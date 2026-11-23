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
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

PROTOCOL_ID = "aqg-cross-window/1"
SUPPORTED_PROTOCOLS = (PROTOCOL_ID,)
# J 的默认口径：Strict 通过率（百分点）。与发布门禁同口径，避免"门禁看 Strict、
# 跨窗口结论看 Standard"这种两套数并存。
DEFAULT_METRIC = "strict_pass_rate"
SUPPORTED_METRICS = (DEFAULT_METRIC, "standard_pass_rate")

FIRST_WINDOW = 0


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


def delta_j(
    baseline_bits: Dict[str, Optional[bool]],
    current_bits: Dict[str, Optional[bool]],
    manifest: dict,
    window: int,
    metric: Optional[str] = None,
) -> dict:
    """按 ΔJ 口径计算跨窗口增益，强制拆成 retained / novel / total 三项。

    - `delta_retained`：**同题复测**的分数差。只有这一项能归因为"被测方跨窗口保没保住能力"。
    - `delta_novel`：新增题的绝对分，没有基线可比，单独报，不参与任何"增益"表述。
    - `delta_total`：全量口径差。分母构成在两期之间变了，所以它是**构成变化与真变化的
      混合体**，只能作为账面对总数，禁止当成能力增益引用。

    基线只允许来自上一窗口（`window - 1`）：跳过一期去比，得到的"增益"里会混进
    被测方在中间窗口已经退化又恢复的过程，读者看不出来。
    """
    declared_metric = metric or manifest.get("metric") or DEFAULT_METRIC
    if declared_metric not in SUPPORTED_METRICS:
        raise WindowProtocolError(f"未知的 J 口径: {declared_metric}")
    if declared_metric != DEFAULT_METRIC:
        # 非 Strict 口径目前没有任何门禁消费它；允许声明但必须落盘标记，
        # 否则报告里会出现一个和发布门禁不同源的"增益"。
        pass

    sets = _declared_sets(manifest)
    if window not in sets:
        raise WindowProtocolError(f"窗口 {window} 不在 manifest 里")
    if window > FIRST_WINDOW and (window - 1) not in sets:
        raise WindowProtocolError(f"窗口 {window} 缺少上一窗口 {window - 1}，ΔJ 基线无处可取")

    novel = set(sets[window]) if window > FIRST_WINDOW else set()
    if window == FIRST_WINDOW:
        # 窗口 0 是基线本身：没有"上一期"，任何 ΔJ 都是无中生有。
        retained: set = set()
    else:
        visible_now = set(visible_ids(manifest, window))
        retained = visible_now - novel

    def _keys(bits: Dict[str, Optional[bool]]) -> set:
        return set(bits)

    common_retained = sorted(retained & _keys(baseline_bits) & _keys(current_bits))
    current_novel = sorted(novel & _keys(current_bits))
    missing_retained = sorted(retained & _keys(current_bits) - _keys(baseline_bits))

    j_current_all = _pass_rate_pp([current_bits[key] for key in sorted(current_bits)])
    j_baseline_all = _pass_rate_pp([baseline_bits[key] for key in sorted(baseline_bits)])
    j_retained_base = _pass_rate_pp([baseline_bits[key] for key in common_retained])
    j_retained_curr = _pass_rate_pp([current_bits[key] for key in common_retained])
    j_novel = _pass_rate_pp([current_bits[key] for key in current_novel])

    result = {
        "protocol": manifest.get("protocol"),
        "metric": declared_metric,
        "window": window,
        "j_baseline": j_baseline_all,
        "j_current": j_current_all,
        "delta_total": (
            None if j_current_all is None or j_baseline_all is None else j_current_all - j_baseline_all
        ),
        "delta_retained": (
            None if j_retained_base is None or j_retained_curr is None
            else j_retained_curr - j_retained_base
        ),
        "j_retained_baseline": j_retained_base,
        "j_retained_current": j_retained_curr,
        "delta_novel": j_novel,
        "n_retained": len(common_retained),
        "n_novel": len(current_novel),
        "n_missing_baseline": len(missing_retained),
        # 归因边界：写进产物，让下游无法只挑 delta_total 说话。
        "attributable_field": "delta_retained",
        "attributable": window > FIRST_WINDOW and bool(common_retained),
    }
    if window == FIRST_WINDOW:
        result["note"] = "窗口 0 是基线轮，不产生 ΔJ；只有 n≥1 的窗口才有增益口径"
    if len(current_bits) != len(baseline_bits):
        result["note_composition"] = (
            f"两期样本集不同（基线 {len(baseline_bits)} 条 / 当期 {len(current_bits)} 条），"
            "delta_total 含构成变化，不可当能力增益"
        )
    return result
