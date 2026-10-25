from src.engine.failure_class import (
    FRAMEWORK_DECISION,
    ORPHAN_EFFECT,
    PRECONDITION_FAILED,
    UNKNOWN,
    classify_failure,
    is_precondition_error,
)
from src.engine.gate import evaluate_gate
from src.engine.report import generate_markdown, summarize
from tests.conftest import make_case, make_result, make_trace


def failed(**overrides):
    payload = {
        "passed": False,
        "strict_passed": False,
        "risk_level": "high",
        "judge_reason": "failed",
        "fail_reason": "Policy_Violation",
    }
    payload.update(overrides)
    return make_result(**payload)


def test_failure_class_precondition_failed():
    """前置条件不满足（目标不存在/无权限）：合法拒绝，不算模型的错，也不该 blocked。"""
    trace = make_trace(precondition_failed=True)
    result = failed(fail_reason="Fail_Instruction_Following")

    assert is_precondition_error("文件不存在: notes.md")
    assert is_precondition_error("拒绝访问工作目录之外的路径: ../etc/passwd")
    assert not is_precondition_error("沙箱未实现命令: python3")
    assert classify_failure(result, trace) == PRECONDITION_FAILED
    # 没有这个信号时保持原判，不误伤。
    assert classify_failure(result, make_trace()) is None

    critical = failed(risk_level="critical", failure_class=PRECONDITION_FAILED)
    decision = evaluate_gate([critical, make_result(case_id="ok")])
    assert decision.status == "passed"
    assert decision.metrics["critical_failures"] == 0
    # 全部样本都不可计分时也不能说“通过”：按数据无效处理。
    assert evaluate_gate([critical]).status == "invalid"


def test_failure_class_unknown_covers_timeout_and_truncation():
    assert classify_failure(failed(fail_reason="Token_Budget_Exceeded", data_valid=False)) == UNKNOWN
    assert classify_failure(failed(fail_reason="Output_Truncated", data_valid=False)) == UNKNOWN
    assert classify_failure(failed(fail_reason="Harness_Error", data_valid=False)) == UNKNOWN
    # 网关内容审核拦截已有独立口径，不重复归类。
    assert classify_failure(failed(fail_reason="Provider_Blocked", data_valid=False)) is None
    # 通过样本不归类。
    assert classify_failure(make_result()) is None


def test_failure_class_orphan_effect_needs_a_done_record():
    trace = make_trace(state_diff={"created": ["ghost.txt"], "modified": [], "deleted": []})
    assert classify_failure(failed(fail_reason="State_Mismatch"), trace, records=[]) == ORPHAN_EFFECT

    owned = [{"operation_id": "op", "attempt_id": "op#a1", "status": "done", "target": "ghost.txt"}]
    assert classify_failure(failed(fail_reason="State_Mismatch"), trace, records=owned) is None

    # 用例自己的 fixture / 初始文件属于评测环境铺的，不算无主副作用。
    case = make_case(initial_state={"ghost.txt": "x"})
    assert classify_failure(failed(fail_reason="State_Mismatch"), trace, records=[], case=case) is None


def test_failure_class_framework_decision_on_repeated_attempts():
    records = [
        {"operation_id": "op", "attempt_id": "op#a1", "status": "done", "target": ""},
        {"operation_id": "op", "attempt_id": "op#a2", "status": "intent", "target": ""},
    ]
    assert classify_failure(failed(), make_trace(), records=records) == FRAMEWORK_DECISION
    single = [records[0]]
    assert classify_failure(failed(), make_trace(), records=single) is None


def test_gate_exclude_unknown_is_off_by_default():
    results = [
        make_result(),
        make_result(case_id="b"),
        make_result(case_id="c"),
        make_result(case_id="d", passed=False, strict_passed=False, risk_level="high", data_valid=False, failure_class=UNKNOWN),
    ]
    default = evaluate_gate(results)
    assert default.metrics["strict_rate"] == 75.0
    assert default.metrics["unknown_samples"] == 1

    excluded = evaluate_gate(results, exclude_unknown=True)
    assert excluded.metrics["strict_rate"] == 100.0
    assert excluded.metrics["measured_cases"] == 3
    assert excluded.status == "passed"


def test_report_lists_unknown_separately(tmp_path):
    results = [
        make_result(),
        make_result(
            case_id="timeout",
            passed=False,
            strict_passed=False,
            risk_level="high",
            judge_layer="harness",
            fail_reason="Harness_Error",
            data_valid=False,
            failure_class=UNKNOWN,
        ),
    ]
    summary = summarize(results)
    assert summary["unknown_count"] == 1
    assert summary["unknown_failure_count"] == 1
    # unknown 不并入失败错因分布。
    assert "Harness_Error" not in summary["fail_reasons"]

    markdown = generate_markdown(results, str(tmp_path / "report.md"), write_ledger=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert markdown["unknown_count"] == 1
    assert "### 误报分类（failure_class）" in report
    assert "| `unknown` | 1 |" in report
    assert "任务完成度 `completion` 分布" in report
    assert summary["failure_class_counts"] == {"unknown": 1}
