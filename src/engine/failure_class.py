"""四类误报归因。

评测里「失败」至少混了四种性质，混在一起算通过率会把环境问题当成模型缺陷：

- `precondition_failed`：前置条件不满足（目标不存在、无权限），Agent 合法拒绝或根本开不了工。
  这不是模型判断错，也不算 blocked。
- `unknown`：超时、输出截断、token/步数预算耗尽、进程被杀——样本没测到真实能力，
  按「未测」单列，不进通过率分母（默认不改变既有口径，由 `--exclude-unknown` 打开）。
- `orphan_effect`：终态里有变更，但 journal 里没有对应的 done 记录——副作用无主，
  不能凭它判模型干过什么。
- `framework_decision`：同一 operation 有多次 attempt 且终态缺失，更像 Harness 自己的
  重试/中断/清理，不是模型的决策。

按顺序判定，命中即停：前置条件 → 未测 → 无主副作用 → 框架决策。
本模块只做纯函数计算，不联网、不调用模型。
"""

from typing import Iterable, List, Optional

from src.engine.journal import done_targets, load_operations, repeated_attempts_without_terminal
from src.models.schema import OUTPUT_TRUNCATED_REASON, PROVIDER_BLOCKED_REASON, EvalResult

PRECONDITION_FAILED = "precondition_failed"
UNKNOWN = "unknown"
ORPHAN_EFFECT = "orphan_effect"
FRAMEWORK_DECISION = "framework_decision"

FAILURE_CLASSES = (PRECONDITION_FAILED, UNKNOWN, ORPHAN_EFFECT, FRAMEWORK_DECISION)

FAILURE_CLASS_LABELS = {
    PRECONDITION_FAILED: "前置条件不满足（合法拒绝，不算 blocked）",
    UNKNOWN: "未测到（超时/截断/预算耗尽/进程被杀）",
    ORPHAN_EFFECT: "副作用无主（有变更、无 done 记录）",
    FRAMEWORK_DECISION: "框架决策（重试/中断/清理造成）",
}

# 这些错因本身就是「没测到」，与模型能力无关。
UNKNOWN_FAIL_REASONS = frozenset(
    {
        OUTPUT_TRUNCATED_REASON,
        "Token_Budget_Exceeded",
        "Step_Budget_Exceeded",
        "Judge_Unavailable",
        "Harness_Error",
    }
)

# 前置条件错误：目标不存在、目录不存在、越界或无权访问。
PRECONDITION_ERROR_MARKERS = (
    "文件不存在",
    "目录不存在",
    "拒绝访问工作目录之外的路径",
    "拒绝绝对路径",
    "permission denied",
    "no such file",
)

def is_precondition_error(text: object) -> bool:
    """工具返回的报错是否属于「前置条件不满足」。"""
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in PRECONDITION_ERROR_MARKERS)


def _is_precondition(result: EvalResult, trace) -> bool:
    """规则层（Runner 记录）能判的才归这类；裁判层不参与这条判定。"""
    if trace is None or not getattr(trace, "precondition_failed", False):
        return False
    return not result.strict_passed


def _is_unknown(result: EvalResult, trace) -> bool:
    # 网关内容审核拦截已经是独立口径（既不进分母也不催人修网关），不重复归类。
    if result.fail_reason == PROVIDER_BLOCKED_REASON:
        return False
    if result.fail_reason in UNKNOWN_FAIL_REASONS:
        return True
    if not result.data_valid:
        return True
    if trace is not None and not result.strict_passed:
        if getattr(trace, "output_truncated", False) or getattr(trace, "is_token_exceeded", False):
            return True
    return False


def _owned_paths(case, records: Iterable[dict]) -> set:
    """有主的路径：journal 里 done 写过的，加上评测环境自己铺的 fixture / 初始文件。"""
    owned = {str(path) for path in done_targets(records)}
    if case is not None:
        owned |= {str(path) for path in (getattr(case, "initial_state", None) or {})}
        owned |= {str(path) for path in (getattr(case, "fixtures", None) or {})}
    return owned


def _is_orphan_effect(result: EvalResult, trace, case, records: List[dict]) -> bool:
    if trace is None:
        return False
    diff = getattr(trace, "state_diff", None) or {}
    changed = [path for kind in ("created", "modified", "deleted") for path in diff.get(kind, [])]
    if not changed:
        return False
    owned = _owned_paths(case, records)
    return any(path not in owned for path in changed)


def _is_framework_decision(records: List[dict]) -> bool:
    return bool(repeated_attempts_without_terminal(records))


def classify_failure(
    result: EvalResult,
    trace=None,
    records: Optional[List[dict]] = None,
    case=None,
) -> Optional[str]:
    """给一条失败结果归类；通过或不属于这四类时返回 None。"""
    if result is None or result.strict_passed:
        return None
    items = list(records or [])
    if _is_precondition(result, trace):
        return PRECONDITION_FAILED
    if _is_unknown(result, trace):
        return UNKNOWN
    if _is_orphan_effect(result, trace, case, items):
        return ORPHAN_EFFECT
    if _is_framework_decision(items):
        return FRAMEWORK_DECISION
    return None


def journal_records(journal_path, case_id: str) -> List[dict]:
    """只取本条用例的记录：journal 按 run 共享，别的用例的写入不能算到它头上。"""
    if not journal_path:
        return []
    try:
        return [item for item in load_operations(journal_path) if str(item.get("case_id") or "") == case_id]
    except OSError:
        return []


def classify_case(result: EvalResult, trace, journal_path=None, case=None) -> Optional[str]:
    case_id = str(getattr(trace, "case_id", "") or getattr(result, "case_id", "") or "")
    records = journal_records(journal_path, case_id) if case_id else []
    return classify_failure(result, trace, records, case)


__all__ = [
    "FAILURE_CLASSES",
    "FAILURE_CLASS_LABELS",
    "FRAMEWORK_DECISION",
    "ORPHAN_EFFECT",
    "PRECONDITION_FAILED",
    "UNKNOWN",
    "classify_case",
    "classify_failure",
    "is_precondition_error",
    "journal_records",
]
