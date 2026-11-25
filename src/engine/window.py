"""跨窗口评测协议（T32 / R-L5）：时间切片、未来窗口不可见、增量喂入、ΔJ 增益。

协议要回答的问题只有一个：**被测方在时间推进后，是否还保得住它已有的能力**。
所以它必须同时满足三条可判定的硬约束：

1. **时间切片** —— 语料按截止时点切成累积序列，窗口 N 的可见题集 = 窗口 0..N 的并集；
2. **未来窗口不可见** —— 评测只允许拿到 ≤ N 的题目；请求到未来窗口的题目即判协议违规
   （硬拒，不静默剔除：静默会让「本轮究竟喂了多少题」失真，也掩盖污染已经发生这件事）；
3. **增量喂入** —— 窗口 N 相对 N-1 的新增集必须显式声明并可核对，同一题不得重复冒充新增。

诚实边界（实现前取证得到的，不是假设）：
- 207 条存量语料**没有任何时间字段**，git 历史仅 8 次提交且经 11-17 私料清除重写。
  所以本模块的窗口时点一律来自 manifest 的**显式声明**，绝不从 case 内容或 git 元数据
  反推——那等于给数据伪造时间戳。
- manifest 只允许引用语老子集（复用 T49 的子集校验口径），因此切片本身不改
  `dataset_sha256`、**不触发切代**；窗口视图的代际由 `declared_generation_for_view` 背书。

ΔJ 的口径纪律（这条比代码本身重要）：跨窗口的分数变化里混着两种完全不同的东西——
同题复测的真变化，和题目覆盖扩张带来的构成变化。所以 `delta_j` 强制拆成三项，
只有 `delta_retained` 可以归因为能力保持，`delta_total` 永远不许被单独当成"增益"引用。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from src.engine.failure_class import PRECONDITION_FAILED, UNKNOWN

PROTOCOL_ID = "aqg-cross-window/1"
SUPPORTED_PROTOCOLS = (PROTOCOL_ID,)
# J 的默认判定：Strict 通过率（百分点）。与发布门禁一样不混用 Standard；
# 分母则由 T50 的 denominator_profiles 单独声明，不能把跨窗口全样本当成 gate.strict_rate。
DEFAULT_METRIC = "strict_pass_rate"
SUPPORTED_METRICS = (DEFAULT_METRIC, "standard_pass_rate")

FIRST_WINDOW = 0
ALL_SAMPLES = "all_samples"
ENVIRONMENT_EXCLUDED = "environment_excluded"


class WindowProtocolError(ValueError):
    """协议不成立。抛出即失败关闭：宁可不出数，也不出一个口径不明的数。"""


def load_manifest(path: Path | str) -> dict:
    """读窗口 manifest。结构损坏或协议版本不认识都直接失败关闭。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise WindowProtocolError(f"窗口 manifest 不存在: {file_path}")
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WindowProtocolError(f"窗口 manifest 不是合法 JSON: {file_path} ({exc})") from exc
    if not isinstance(data, dict):
        raise WindowProtocolError("窗口 manifest 顶层必须是对象")
    protocol = data.get("protocol")
    if protocol not in SUPPORTED_PROTOCOLS:
        raise WindowProtocolError(
            f"窗口 manifest 协议版本不支持: {protocol!r}（支持 {', '.join(SUPPORTED_PROTOCOLS)}）"
        )
    return data


def _window_list(manifest: dict) -> List[dict]:
    windows = manifest.get("windows")
    if not isinstance(windows, list) or not windows:
        raise WindowProtocolError("窗口 manifest 必须给出非空 windows 数组")
    return windows


def validate_manifest(manifest: dict, corpus_ids: Iterable[str]) -> dict:
    """核对 manifest 与语料的关系，返回审计（不通过则抛 WindowProtocolError）。

    校验顺序上先核协议版本：不认识版本就意味着下面这些语义的**解释方式**都不确定，
    此时任何一条校验通过与否都没有意义。协议版本在 `load_manifest` 里也查一遍，
    因为调用方可能自行 `json.loads` 后直接进本函数（run_eval 早期就是这样），
    把守卫只放在读取口等于留了一个绕过入口。

    五条硬校验，每条都对应一种会让跨窗口结论失效的错法：
    - 序号连续：窗口号跳号意味着"中间那轮没跑"，累积语义就断了；
    - 新增集 ⊆ 语料：引用语料外的题，等于给不属于本代际的题背书；
    - 新增集两两不相交：同一题重复出现在两个窗口，会把"重排旧题"算成新增增益；
    - 截止时点单调不减且与窗口号同序：时间切片的定义就是按时间推进；
    - 每窗口新增集非空：空窗口不产生任何新证据，却会在台账里记一次"轮次"。
    """
    protocol = manifest.get("protocol")
    if protocol not in SUPPORTED_PROTOCOLS:
        raise WindowProtocolError(
            f"窗口 manifest 协议版本不支持: {protocol!r}（支持 {', '.join(SUPPORTED_PROTOCOLS)}）"
        )

    corpus = {str(item) for item in corpus_ids}
    if not corpus:
        raise WindowProtocolError("语料 id 集合为空，无法校验窗口 manifest")

    windows = _window_list(manifest)
    ids_by_window: Dict[int, set] = {}
    previous_cutoff: Optional[str] = None

    for position, window in enumerate(windows):
        if not isinstance(window, dict):
            raise WindowProtocolError(f"第 {position} 个窗口条目不是对象")
        number = window.get("window")
        if not isinstance(number, int):
            raise WindowProtocolError(f"窗口 window 必须是整数，收到 {number!r}")
        if number != FIRST_WINDOW + position:
            raise WindowProtocolError(
                f"窗口序号必须从 {FIRST_WINDOW} 起连续递增：第 {position} 项是 {number}"
            )
        declared = [str(item) for item in (window.get("declared") or [])]
        if not declared:
            raise WindowProtocolError(f"窗口 {number} 的新增集为空，不产生任何新证据")
        if len(set(declared)) != len(declared):
            raise WindowProtocolError(f"窗口 {number} 的新增集内部有重复 id")
        outside = sorted(set(declared) - corpus)
        if outside:
            raise WindowProtocolError(
                f"窗口 {number} 引用了 {len(outside)} 条语料外用例，拒不背书；样例: {outside[:3]}"
            )
        for earlier_number, earlier_ids in ids_by_window.items():
            overlap = set(declared) & earlier_ids
            if overlap:
                raise WindowProtocolError(
                    f"窗口 {number} 与窗口 {earlier_number} 的新增集相交 "
                    f"{len(overlap)} 条（样例 {sorted(overlap)[:3]}）：同一题不能重复冒充新增"
                )
        cutoff = str(window.get("cutoff") or "")
        if not cutoff:
            raise WindowProtocolError(f"窗口 {number} 缺 cutoff 截止时点，时间切片无法成立")
        if previous_cutoff is not None and cutoff < previous_cutoff:
            raise WindowProtocolError(
                f"窗口 {number} 的 cutoff {cutoff} 早于上一窗口 {previous_cutoff}：时间必须单调推进"
            )
        ids_by_window[number] = set(declared)
        previous_cutoff = cutoff

    return {
        "protocol": manifest.get("protocol"),
        "metric": manifest.get("metric") or DEFAULT_METRIC,
        "windows": len(windows),
        "corpus_ids": len(corpus),
        "declared_ids": sum(len(item) for item in ids_by_window.values()),
        "covered_ids": len(set().union(*ids_by_window.values())) if ids_by_window else 0,
    }


def window_numbers(manifest: dict) -> List[int]:
    return [int(item["window"]) for item in _window_list(manifest)]


def _declared_sets(manifest: dict) -> Dict[int, set]:
    return {
        int(item["window"]): {str(value) for value in (item.get("declared") or [])}
        for item in _window_list(manifest)
    }


def increment_ids(manifest: dict, window: int) -> List[str]:
    """本窗口相对上一窗口的新增集（增量喂入喂的就是这批）。"""
    sets = _declared_sets(manifest)
    if window not in sets:
        raise WindowProtocolError(f"窗口 {window} 不在 manifest 里")
    return sorted(sets[window])


def visible_ids(manifest: dict, window: int) -> List[str]:
    """当前窗口的可见题集 = 窗口 0..window 的累积并集（未来窗口不在其中）。"""
    sets = _declared_sets(manifest)
    if window not in sets:
        raise WindowProtocolError(f"窗口 {window} 不在 manifest 里（已知: {sorted(sets)}）")
    future = {number for number in sets if number > window}
    if future:
        # 可见集的定义就是"不含未来"，这里显式算一遍而不是靠并集顺序侥幸。
        allowed = {number for number in sets if number <= window}
    else:
        allowed = sets.keys()
    collected: set = set()
    for number in sorted(allowed):
        collected |= sets[number]
    return sorted(collected)


def blocked_ids(manifest: dict, window: int, requested_ids: Iterable[str]) -> List[str]:
    """请求集里属于未来窗口的部分——这些题对被测方不可见，出现即协议违规。"""
    visible = set(visible_ids(manifest, window))
    return sorted({str(item) for item in requested_ids} - visible)


def select_window_cases(cases: Sequence, manifest: dict, window: int, enforce: bool = True):
    """按窗口裁剪用例集合。返回 (选中用例, 审计)。

    `enforce=True`（默认）时，请求集含未来窗口用例直接抛错——这是协议的本体：
    "未来不可见"必须是一个会被违反的约束，否则它只是一句注释。
    审计里的 `dropped` 让调用方即使在非强制模式下也能看见丢了多少题。
    """
    visible = set(visible_ids(manifest, window))
    selected = [case for case in cases if str(_case_id(case)) in visible]
    dropped = sorted(str(_case_id(case)) for case in cases if str(_case_id(case)) not in visible)
    if enforce and dropped:
        raise WindowProtocolError(
            f"窗口 {window} 请求到 {len(dropped)} 条未来/未声明用例（样例 {dropped[:3]}）："
            "跨窗口协议要求未来窗口对被测方不可见"
        )
    audit = {
        "window": window,
        "asked": len(list(cases)),
        "selected": len(selected),
        "dropped": len(dropped),
        "dropped_ids": dropped,
    }
    return selected, audit


def build_window_view(
    corpus_path: Path | str, manifest: dict, window: int
) -> tuple[List[str], dict]:
    """把语料的**原始行**裁成某窗口的喂题视图，返回 (行列表, 审计)。

    为什么需要这一步：`select_window_cases(enforce=True)` 的语义是“喂进来的题集
    必须 ⊆ 可见集”，而 `run_eval` 默认拿的是全量语料。两者直接相遇时，窗口 0 必然
    撞上“请求到未来题”而 fail-closed——协议本身没错，缺的是“从全量语料安全裁到本窗口”
    这个入口。视图就是那个入口的产物。

    三条硬约束（都可被违反，违反即抛错，不静默出半成品视图）：

    1. **字节保真**：视图里的每一行都是语料同一行的原文。重写 JSON（改键序、改转义、
       改空白）会让被测方看到的题面与存档题面不是同一个东西，而 ΔJ 比的正是同一道题；
    2. **不越界**：只输出可见集内的行。裁多了（含未来题）就是污染，裁少了（分母悄悄
       变小）会让 ΔJ 的 retained 样本缩水——两边都不允许；
    3. **先过协议校验**：子集/序号/不相交/单调不成立就不出视图，避免给一个违规的
       manifest 生成“看着合理”的喂题集（纵深防御：调用方忘了 validate 也不会漏）。

    “裁少了”不需要在这里重查：`validate_manifest` 已要求每题 declared ⊆ 语料且非空，
    所以 visible 必然全量命中语料——在这里再加一道同义自检只会留下不可达分支，
    而下一次改动的读者会误以为还有人在看门。真正可达的风险只有语料本身 malformed。

    视图只引用语料子集，所以它的 `dataset_sha256` 与语料不同但 id 全部落在语料内——
    正好满足 T49 代际自动声明的三条硬校验，跑批时会把代际锚回语料 epoch，不误触切代。
    """
    path = Path(corpus_path)
    if not path.is_file():
        raise WindowProtocolError(f"语料不存在，无法生成窗口视图: {path}")

    lines: List[str] = []
    ids_in_order: List[str] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(True), 1):
        if not raw.strip():
            continue
        if not raw.lstrip().startswith("{"):
            raise WindowProtocolError(f"语料第 {line_number} 行不是 JSON 对象，拒裁视图")
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WindowProtocolError(f"语料第 {line_number} 行解析失败，拒裁视图: {exc}") from exc
        case_id = str(_case_id(record))
        if not case_id:
            raise WindowProtocolError(f"语料第 {line_number} 行没有 id 字段，拒裁视图")
        lines.append(raw)
        ids_in_order.append(case_id)

    duplicates = sorted({cid for cid in ids_in_order if ids_in_order.count(cid) > 1})
    if duplicates:
        # 同 id 两行内容不同时，“保留哪一行”会改变题面；视图不替调用方做这个决定。
        raise WindowProtocolError(f"语料存在重复 id {duplicates[:5]}，无法定义逐字节保真的视图")

    audit_manifest = validate_manifest(manifest, ids_in_order)
    visible = set(visible_ids(manifest, window))

    selected = [raw for raw, cid in zip(lines, ids_in_order) if cid in visible]
    blocked = sorted(cid for cid in ids_in_order if cid not in visible)

    audit = {
        "window": window,
        "corpus_lines": len(lines),
        "protocol": audit_manifest["protocol"],
        "visible": len(visible),
        "emitted": len(selected),
        "blocked": len(blocked),
        "blocked_ids": blocked,
    }
    return selected, audit


def _case_id(case) -> str:
    """兼容 TestCase 对象与原始 dict 两种形态。"""
    if isinstance(case, dict):
        return str(case.get("id") or case.get("case_id") or "")
    return str(getattr(case, "id", None) or getattr(case, "case_id", "") or "")


def _pass_rate_pp(bits: Sequence[Optional[bool]]) -> Optional[float]:
    """通过率（百分点）。None 位剔出分母：环境故障不是模型失败（AGENTS.md 纪律 3）。"""
    usable = [bit for bit in bits if bit is not None]
    if not usable:
        return None
    return 100.0 * sum(1 for bit in usable if bit) / len(usable)


def _observation_bit(observation: object) -> Optional[bool]:
    """兼容直接传旧布尔映射与 T50 的完整 observation 映射。"""
    if isinstance(observation, bool) or observation is None:
        return observation
    if isinstance(observation, dict):
        return observation.get("strict_passed")
    return None


def _environment_exclusion_reason(observation: object) -> Optional[str]:
    """T50 能力归因口径的剔除原因；空字符串表示可计入能力分母。

    顺序与 `failure_class.classify_failure` 一致：前置条件先于 unknown。unknown
    在发布门禁里由 `--exclude-unknown` 控制，T50 的能力归因口径则明确固定剔除，
    避免跨窗口读数受两期环境样本分布差异影响。`orphan_effect` /
    `framework_decision` 不凭分类名额外剔除；若它们同时 data_valid=False、
    harness 或 fallback，仍会按下面的不可信证据被摘出。
    """
    bit = _observation_bit(observation)
    if bit is None:
        return "missing_outcome"
    if not isinstance(observation, dict):
        return None
    failure_class = observation.get("failure_class")
    if failure_class == PRECONDITION_FAILED:
        return PRECONDITION_FAILED
    # 正式 failure_class 优先：显式 None 也是合并后的最终分类，不能被早期某次
    # repeat 的 trace 标记覆盖。只有历史行缺该字段时，trace 才作为回退证据；
    # 且仍要求最终 Strict 失败，模型探路后恢复通过不能剔题。
    if (
        bit is False
        and observation.get("precondition_failed") is True
        and "failure_class" in (observation.get("metadata_missing") or [])
    ):
        return "trace_precondition_failed"
    if failure_class == UNKNOWN:
        return UNKNOWN
    if observation.get("data_valid") is False:
        return "invalid_data"
    if observation.get("judge_layer") == "harness":
        return "harness"
    if observation.get("is_fallback") is True:
        return "fallback"
    return None


def _denominator_profile(
    baseline_bits: Dict[str, Optional[bool]],
    current_bits: Dict[str, Optional[bool]],
    baseline_observations: Optional[Dict[str, dict]],
    current_observations: Optional[Dict[str, dict]],
    common_retained: List[str],
    current_novel: List[str],
    *,
    exclude_environment: bool,
) -> dict:
    """计算一种分母口径，并留下两期各自的剔除账本。"""

    def _side(bits: Dict[str, Optional[bool]], observations: Optional[Dict[str, dict]], label: str):
        keys = sorted(bits)
        reasons: Counter = Counter()
        excluded_by_reason: Dict[str, List[str]] = {}
        scored: List[bool] = []
        metadata_missing: List[str] = []
        for case_id in keys:
            observation = observations.get(case_id) if observations is not None else None
            bit = _observation_bit(observation) if observations is not None else bits[case_id]
            reason = _environment_exclusion_reason(observation) if exclude_environment else None
            if reason is None and bit is None:
                reason = "missing_outcome"
            if reason:
                reasons[reason] += 1
                excluded_by_reason.setdefault(reason, []).append(case_id)
            else:
                scored.append(bit)
            if isinstance(observation, dict) and observation.get("metadata_missing"):
                metadata_missing.append(case_id)
        retained_keys = [case_id for case_id in common_retained if case_id in bits]
        retained_scored = [
            case_id for case_id in retained_keys
            if not (
                exclude_environment
                and observations is not None
                and _environment_exclusion_reason(observations.get(case_id))
            )
            and bits[case_id] is not None
        ]
        novel_keys = [case_id for case_id in current_novel if case_id in bits]
        novel_scored = [
            case_id for case_id in novel_keys
            if not (
                exclude_environment
                and observations is not None
                and _environment_exclusion_reason(observations.get(case_id))
            )
            and bits[case_id] is not None
        ]
        return {
            "label": label,
            "cases": len(keys),
            "scored_cases": len(scored),
            "j": _pass_rate_pp(scored),
            "retained_cases": len(retained_keys),
            "retained_scored_cases": len(retained_scored),
            "j_retained": _pass_rate_pp([bits[case_id] for case_id in retained_scored]),
            "novel_cases": len(novel_keys),
            "novel_scored_cases": len(novel_scored),
            "j_novel": _pass_rate_pp([bits[case_id] for case_id in novel_scored]),
            "excluded_count": sum(reasons.values()),
            "excluded_by_reason": dict(sorted(reasons.items())),
            "excluded_case_ids": {
                reason: sorted(case_ids) for reason, case_ids in sorted(excluded_by_reason.items())
            },
            "metadata_available": observations is not None,
            "metadata_missing_count": len(metadata_missing),
            "metadata_missing_case_ids_sample": sorted(metadata_missing)[:10],
        }

    baseline = _side(baseline_bits, baseline_observations, "baseline")
    current = _side(current_bits, current_observations, "current")
    paired_scored = [
        case_id for case_id in common_retained
        if _case_is_scored(baseline_bits, baseline_observations, case_id, exclude_environment)
        and _case_is_scored(current_bits, current_observations, case_id, exclude_environment)
    ]
    return {
        "j_baseline": baseline["j"],
        "j_current": current["j"],
        "delta_total": _difference(current["j"], baseline["j"]),
        "j_retained_baseline": baseline["j_retained"],
        "j_retained_current": current["j_retained"],
        "delta_retained": _difference(current["j_retained"], baseline["j_retained"]),
        "delta_novel": current["j_novel"],
        "n_baseline": baseline["cases"],
        "n_current": current["cases"],
        "n_baseline_scored": baseline["scored_cases"],
        "n_current_scored": current["scored_cases"],
        "n_retained": len(common_retained),
        "n_retained_baseline_scored": baseline["retained_scored_cases"],
        "n_retained_current_scored": current["retained_scored_cases"],
        "n_retained_paired_scored": len(paired_scored),
        "n_novel": current["novel_cases"],
        "n_novel_scored": current["novel_scored_cases"],
        "exclusions": {"baseline": baseline, "current": current},
    }


def _case_is_scored(
    bits: Dict[str, Optional[bool]],
    observations: Optional[Dict[str, dict]],
    case_id: str,
    exclude_environment: bool,
) -> bool:
    """一个题在指定口径下是否可计分（供 profile 配对审计与测试复用）。"""
    if case_id not in bits or bits[case_id] is None:
        return False
    if exclude_environment and observations is not None:
        return _environment_exclusion_reason(observations.get(case_id)) is None
    return True


def _difference(current: Optional[float], baseline: Optional[float]) -> Optional[float]:
    return None if current is None or baseline is None else current - baseline


def delta_j(
    baseline_bits: Dict[str, Optional[bool]],
    current_bits: Dict[str, Optional[bool]],
    manifest: dict,
    window: int,
    metric: Optional[str] = None,
    baseline_observations: Optional[Dict[str, dict]] = None,
    current_observations: Optional[Dict[str, dict]] = None,
) -> dict:
    """按 ΔJ 口径计算跨窗口变化，并列全样本与剔环境类两套分母。

    - `delta_retained`：**同题复测**的分数差。只有这一项能归因为"被测方跨窗口保没保住能力"。
    - `delta_novel`：新增题的绝对分，没有基线可比，单独报，不参与任何"增益"表述。
    - `delta_total`：全量口径差。分母构成在两期之间变了，所以它是**构成变化与真变化的
      混合体**，只能作为账面对总数，禁止当成能力增益引用。

    旧顶层字段始终是**全样本口径**，保持历史消费者兼容。新消费者必须读
    `denominator_profiles`：`all_samples` 是门禁主口径，`environment_excluded`
    是能力归因口径。后者固定剔除 precondition_failed、unknown、无效数据、harness
    与 fallback；两期 retained 各自计算可计分数，并另报两期都可计分的配对数。

    基线只允许来自上一窗口（`window - 1`）：跳过一期去比，得到的"增益"里会混进
    被测方在中间窗口已经退化又恢复的过程，读者看不出来。
    """
    declared_metric = metric or manifest.get("metric") or DEFAULT_METRIC
    if declared_metric not in SUPPORTED_METRICS:
        raise WindowProtocolError(f"未知的 J 口径: {declared_metric}")

    sets = _declared_sets(manifest)
    if window not in sets:
        raise WindowProtocolError(f"窗口 {window} 不在 manifest 里")
    if window > FIRST_WINDOW and (window - 1) not in sets:
        raise WindowProtocolError(f"窗口 {window} 缺少上一窗口 {window - 1}，ΔJ 基线无处可取")

    novel = set(sets[window]) if window > FIRST_WINDOW else set()
    if window == FIRST_WINDOW:
        retained: set = set()
    else:
        retained = set(visible_ids(manifest, window)) - novel

    common_retained = sorted(retained & set(baseline_bits) & set(current_bits))
    current_novel = sorted(novel & set(current_bits))
    missing_retained = sorted(retained & set(current_bits) - set(baseline_bits))

    all_profile = _denominator_profile(
        baseline_bits, current_bits, None, None, common_retained, current_novel,
        exclude_environment=False,
    )
    valid_profile = _denominator_profile(
        baseline_bits, current_bits, baseline_observations, current_observations,
        common_retained, current_novel, exclude_environment=True,
    )

    result = {
        "protocol": manifest.get("protocol"),
        "metric": declared_metric,
        "window": window,
        # 兼容别名：旧消费者继续读到与改动前一致的全样本字段。
        "j_baseline": all_profile["j_baseline"],
        "j_current": all_profile["j_current"],
        "delta_total": all_profile["delta_total"],
        "delta_retained": all_profile["delta_retained"],
        "j_retained_baseline": all_profile["j_retained_baseline"],
        "j_retained_current": all_profile["j_retained_current"],
        "delta_novel": all_profile["delta_novel"],
        "n_retained": all_profile["n_retained"],
        "n_novel": all_profile["n_novel"],
        "n_missing_baseline": len(missing_retained),
        "primary_denominator": ALL_SAMPLES,
        "attribution_denominator": ENVIRONMENT_EXCLUDED,
        "denominator_profiles": {
            ALL_SAMPLES: all_profile,
            ENVIRONMENT_EXCLUDED: valid_profile,
        },
        # 能力归因字段单独命名，绝不覆盖上面的门禁主口径。
        "j_baseline_valid_only": valid_profile["j_baseline"],
        "j_current_valid_only": valid_profile["j_current"],
        "delta_total_valid_only": valid_profile["delta_total"],
        "j_retained_baseline_valid_only": valid_profile["j_retained_baseline"],
        "j_retained_current_valid_only": valid_profile["j_retained_current"],
        "delta_retained_valid_only": valid_profile["delta_retained"],
        "delta_novel_valid_only": valid_profile["delta_novel"],
        "n_retained_valid_only": valid_profile["n_retained_paired_scored"],
        "n_novel_valid_only": valid_profile["n_novel_scored"],
        # 归因边界：写进产物，让下游无法只挑 delta_total 说话。
        "attributable_field": "delta_retained",
        "attributable": window > FIRST_WINDOW and bool(common_retained),
        "attribution_valid_only": (
            window > FIRST_WINDOW and valid_profile["n_retained_paired_scored"] > 0
        ),
    }
    result["note_denominators"] = (
        "全样本 Strict 是跨窗口门禁主口径；剔环境类 Strict 是能力归因口径，两者并列且不互相替换。"
        "旧顶层字段保持全样本语义，新消费者应读 denominator_profiles；"
        "跨窗口全样本不等同于发布门禁 strict_rate（后者始终剔除 precondition_failed）。"
    )
    missing_metadata = sum(
        side["metadata_missing_count"]
        for side in valid_profile["exclusions"].values()
    )
    if (
        baseline_observations is None
        or current_observations is None
        or missing_metadata
    ):
        result["note_metadata"] = (
            "至少一期缺环境分类元数据：未提供的 observation 只能剔除缺失结论位；"
            "历史缺键按 EvalResult 默认值兼容，能力归因仅依据已落盘证据，不得声称完整取证。"
        )
    if window == FIRST_WINDOW:
        result["note"] = "窗口 0 是基线轮，不产生 ΔJ；只有 n≥1 的窗口才有增益口径"
    if len(current_bits) != len(baseline_bits):
        result["note_composition"] = (
            f"两期样本集不同（基线 {len(baseline_bits)} 条 / 当期 {len(current_bits)} 条），"
            "delta_total 含构成变化，不可当能力增益"
        )
    return result
