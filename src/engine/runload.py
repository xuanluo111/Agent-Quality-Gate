"""run 产物的统一读取口：把 `reports/runs/<run_id>/` 的结果行解析成可比对的结论位。

为什么单独建这个模块：`results.jsonl` 的解析过去散在 4 处（`scripts/rejudge.py`、
`scripts/build_label_template.py`、`scripts/judge_token_monitor.py`、
`scripts/run_batch.extract_infra_hint`），每处各自 `json.loads` 再各自挑键。行格式一变
——例如 T49 取证时撞见的「`{result, trace}` 嵌套」与「扁平 result」并存——就要 4 处
同时改对，漏一处就得到一个静默的错误答案。跨窗口协议与噪声标定都要逐条读结论位，
不能再添第 5 份散落实现。

边界（故意的）：本模块只服务新代码，**不改动上述 4 个既有调用点**。收敛它们是独立
任务，混进 T32 会把一次协议改动扩成 4 处回归风险。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterator, List, Optional

RESULTS_NAME = "results.jsonl"
CONFIG_NAME = "config.json"
# 一次尝试的结论位取这个字段：它是逐次落盘的原始判定，不是合并后的最终结论。
# 合并结论（`result.strict_passed`）已被“按最差一次”规则压过，拿它算重复采样抖动会低估噪声。
_ATTEMPT_OUTCOME_KEY = "strict_passed"


def iter_result_rows(path: Path) -> Iterator[tuple[Optional[dict], bool]]:
    """逐行产出 (记录, 是否为坏行)。坏行不抛异常：历史批存在截断尾行，标定必须容忍。

    坏行不静默丢弃——它以 `None` 交给调用方计数，因为「样本量比看上去少」正是
    通过率类指标最容易被低估的偏差来源。
    """
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line), False
        except json.JSONDecodeError:
            yield None, True


def _coerce_bit(value: object) -> Optional[bool]:
    """把结论值压成三态：True/False/None（缺键＝不可用，不当作失败）。

    把「缺字段」当成 False 会凭空造出失败，把缺字段当成 True 会凭空造出通过；
    两者都会把实测噪声带推向错误的方向，所以这里保留 None 让调用方剔除。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "pass", "passed"}:
            return True
        if lowered in {"false", "fail", "failed"}:
            return False
    return None


def bit_of(attempt: dict) -> Optional[bool]:
    """从一条原始尝试记录取 Strict 结论位（公开入口，供下游不要各自拆字典）。"""
    return _coerce_bit(attempt.get(_ATTEMPT_OUTCOME_KEY))


def seed_of(attempt: dict) -> Optional[int]:
    """从一条原始尝试记录取实际采样 seed；没这个键返回 None（≠ 没变序，只是未落盘）。"""
    value = attempt.get("seed")
    return value if isinstance(value, int) else None


def attempts_of(record: dict) -> List[dict]:
    """原始尝试记录，按尝试序号排列（保留 seed/latency/judge_layer 等全部键）。

    为什么需要单独暴露这个口：结论位序列（下面的 `attempt_bits`）会把 seed 丢掉，
    而「重复采样究竟是不是变序」只能从每次尝试落盘的 seed 里核对。噪声标定既要
    结论位算抖动，也要 seed 自证生效，所以两个视角都得能拿到，且必须同源同序。
    """
    result = record.get("result") or {}
    attempts = result.get("attempts")
    if isinstance(attempts, list) and attempts:
        ordered = [dict(item) for item in attempts if isinstance(item, dict)]
        ordered.sort(key=lambda item: item.get("attempt") or 0)
        # 新产物把逐次判定放在 result.attempts，把 seed/token/latency 等原始
        # trace 放在顶层 attempts；两者按 attempt 编号配对，不能只读其中一层。
        traces = record.get("attempts")
        if isinstance(traces, list):
            by_attempt = {
                item.get("attempt"): item
                for item in traces
                if isinstance(item, dict) and isinstance(item.get("attempt"), int)
            }
            merged = []
            for item in ordered:
                attempt = item.get("attempt")
                trace = by_attempt.get(attempt) if isinstance(attempt, int) else None
                merged.append({**trace, **item} if trace else item)
            ordered = merged
        return ordered
    # 没有 attempts 数组 = `repeat=1` 批次的正常形态：最终结论本身就是那一次尝试。
    return [result]


def attempt_bits(record: dict) -> List[Optional[bool]]:
    """取一条用例每次尝试的 Strict 结论位。缺键返回 None（不可用），不当失败也不当通过。"""
    ordered = attempts_of(record)
    bits = [_coerce_bit(item.get(_ATTEMPT_OUTCOME_KEY)) for item in ordered]
    if ordered and not any(bit is not None for bit in bits):
        # 全 None 时退回 result 顶层：历史行里存在 attempts 只记了非判定字段的情况。
        return [_coerce_bit((record.get("result") or {}).get(_ATTEMPT_OUTCOME_KEY))]
    return bits


def case_id_of(record: dict) -> str:
    """用例主键：数据集与结果行都用 `id`，历史行里也叫 `case_id`。"""
    result = record.get("result") or {}
    trace = record.get("trace") or {}
    for holder in (result, trace):
        for key in ("case_id", "id"):
            value = holder.get(key)
            if value:
                return str(value)
    return ""


def case_attempts_from_run(run_dir: Path) -> tuple[Dict[str, List[dict]], dict]:
    """读一个 run 目录，返回 {case_id: [每次尝试的原始记录]} 与读取审计。"""
    path = Path(run_dir) / RESULTS_NAME
    attempts: Dict[str, List[dict]] = {}
    audit = {"rows": 0, "bad_lines": 0, "missing_case_id": 0}
    if not path.is_file():
        return attempts, audit
    for record, bad in iter_result_rows(path):
        if bad:
            audit["bad_lines"] += 1
            continue
        audit["rows"] += 1
        case_id = case_id_of(record)
        if not case_id:
            audit["missing_case_id"] += 1
            continue
        attempts[case_id] = attempts_of(record)
    return attempts, audit


def case_bits_from_run(run_dir: Path) -> tuple[Dict[str, List[Optional[bool]]], dict]:
    """读一个 run 目录，返回 {case_id: [每次尝试的 Strict 结论位]} 与读取审计。

    审计里必须留 `bad_lines`：同一批的样本量若因坏行缩水，噪声带的分母就变了，
    只看结论位看不出来。
    """
    attempts, audit = case_attempts_from_run(run_dir)
    bits = {
        case_id: [_coerce_bit(item.get(_ATTEMPT_OUTCOME_KEY)) for item in records]
        for case_id, records in attempts.items()
    }
    return bits, audit


def _trace_precondition_failed(record: dict) -> Optional[bool]:
    """读取 Runner 的前置条件证据，兼容旧 `trace` 与 repeat 批的顶层 `attempts`。

    新结果的 `result.failure_class` 已包含这个判断；旧行可能只有 trace 标记。
    两者要同时保留，否则历史批次里的环境故障会以 False 混进能力归因分母。
    """
    trace = record.get("trace") or {}
    direct = _coerce_bit(trace.get("precondition_failed"))
    attempts = record.get("attempts")
    flags = [direct]
    if isinstance(attempts, list):
        flags.extend(
            _coerce_bit(item.get("precondition_failed"))
            for item in attempts
            if isinstance(item, dict)
        )
    if any(flag is True for flag in flags):
        return True
    known = [flag for flag in flags if flag is not None]
    return False if known and all(flag is False for flag in known) else None


def final_observation_of(record: dict) -> dict:
    """把最终结论与判错因元数据一起取出，供双口径指标共用同一份证据。

    只返回 `strict_passed` 会丢掉 `precondition_failed` / `unknown`：真实环境故障
    经常是 `strict_passed=False, data_valid=True`，下游无法凭结论位识别。缺失的
    `data_valid` / `is_fallback` 按 `EvalResult` 的既有默认值补 True / False，
    但 `metadata_missing` 会原样点名，不把历史缺键伪装成完整取证。
    """
    result = record.get("result") or {}
    failure_class = result.get("failure_class")
    data_valid = _coerce_bit(result.get("data_valid"))
    is_fallback = _coerce_bit(result.get("is_fallback"))
    # 缺键按 EvalResult 默认值兼容；键存在却不是布尔则失败关闭，不能把脏证据当可信。
    if "data_valid" in result and data_valid is None:
        data_valid = False
    if "is_fallback" in result and is_fallback is None:
        is_fallback = True
    tracked = ("failure_class", "data_valid", "judge_layer", "is_fallback")
    return {
        "strict_passed": _coerce_bit(result.get(_ATTEMPT_OUTCOME_KEY)),
        "failure_class": str(failure_class).strip() if failure_class else None,
        "data_valid": True if data_valid is None else data_valid,
        "judge_layer": str(result.get("judge_layer") or "") or None,
        "is_fallback": False if is_fallback is None else is_fallback,
        "precondition_failed": _trace_precondition_failed(record),
        "metadata_missing": [key for key in tracked if key not in result],
    }


def case_final_observations(run_dir: Path) -> tuple[Dict[str, dict], dict]:
    """读一个 run 目录，返回 {case_id: 最终结论及环境分类元数据} 与读取审计。

    这是 `case_final_bits` 的完整证据版；前者保持原有三态返回类型，后者服务 T50
    的「全样本 / 剔环境类」双口径。两者必须同源解析，不能一份读 result、另一份猜 trace。
    """
    path = Path(run_dir) / RESULTS_NAME
    observations: Dict[str, dict] = {}
    audit = {"rows": 0, "bad_lines": 0, "missing_case_id": 0}
    if not path.is_file():
        return observations, audit
    for record, bad in iter_result_rows(path):
        if bad:
            audit["bad_lines"] += 1
            continue
        audit["rows"] += 1
        case_id = case_id_of(record)
        if not case_id:
            audit["missing_case_id"] += 1
            continue
        observations[case_id] = final_observation_of(record)
    return observations, audit


def case_final_bits(run_dir: Path) -> tuple[Dict[str, Optional[bool]], dict]:
    """读一个 run 目录，返回 {case_id: 合并后的最终 Strict 结论}。

    与 `case_bits_from_run` 的区别是口径而不是 convenience：逐次 attempts 回答
    「重跑会抖多少」（噪声），`result.strict_passed` 回答「这批合起来合不合格」（门禁）。
    两个口径不能互换：拿 attempts[0] 当最终结论会把“按最差一次合并”的规则抹掉，
    算出的 ΔJ 与发布门禁看的不是同一个数。
    """
    observations, audit = case_final_observations(run_dir)
    finals = {
        case_id: observation["strict_passed"]
        for case_id, observation in observations.items()
    }
    return finals, audit


def read_run_config(run_dir: Path) -> dict:
    """读 run 的配置快照；缺失或损坏返回空 dict，由调用方按“缺证据”处理。"""
    path = Path(run_dir) / CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
