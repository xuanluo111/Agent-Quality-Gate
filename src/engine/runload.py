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


def case_final_bits(run_dir: Path) -> tuple[Dict[str, Optional[bool]], dict]:
    """读一个 run 目录，返回 {case_id: 合并后的最终 Strict 结论}。

    与 `case_bits_from_run` 的区别是口径而不是 convenience：逐次 attempts 回答
    「重跑会抖多少」（噪声），`result.strict_passed` 回答「这批合起来合不合格」（门禁）。
    两个口径不能互换：拿 attempts[0] 当最终结论会把“按最差一次合并”的规则抹掉，
    算出的 ΔJ 与发布门禁看的不是同一个数。
    """
    path = Path(run_dir) / RESULTS_NAME
    finals: Dict[str, Optional[bool]] = {}
    audit = {"rows": 0, "bad_lines": 0, "missing_case_id": 0}
    if not path.is_file():
        return finals, audit
    for record, bad in iter_result_rows(path):
        if bad:
            audit["bad_lines"] += 1
            continue
        audit["rows"] += 1
        case_id = case_id_of(record)
        if not case_id:
            audit["missing_case_id"] += 1
            continue
        finals[case_id] = _coerce_bit((record.get("result") or {}).get(_ATTEMPT_OUTCOME_KEY))
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
