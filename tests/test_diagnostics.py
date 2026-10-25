import json

from run_eval import _explain_lines
from src.engine.gate import case_diagnostic, evaluate_gate
from src.engine.report import generate_markdown
from tests.conftest import make_result


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


def test_state_mismatch_gets_terminal_state_hint():
    mismatch = [{"path": "notes.md", "op": "exists", "expected": True, "actual": False, "detail": "文件不存在"}]
    result = failed(
        fail_reason="State_Mismatch",
        evidence=json.dumps(mismatch, ensure_ascii=False),
        evidence_span="state:notes.md",
    )
    diagnostic = case_diagnostic(result)
    assert diagnostic["fix_hint"] == "终态与期望不符：notes.md"
    assert diagnostic["assertion"] == "state:notes.md"
    # False 是合法期望值，不能被当成空值丢掉。
    assert diagnostic["expected"] == "True"
    assert diagnostic["actual"] == "False"


def test_state_mismatch_without_json_evidence_still_names_the_path():
    result = failed(fail_reason="State_Mismatch", evidence="不是 JSON", evidence_span="state:report.csv")
    diagnostic = case_diagnostic(result)
    assert diagnostic["fix_hint"] == "终态与期望不符：report.csv"
    assert diagnostic["expected"] == ""
    assert diagnostic["actual"] == ""


def test_provenance_violation_gets_parameter_hint():
    result = failed(
        fail_reason="Fabricated_Parameter",
        evidence_span="tool:refund",
        provenance_violations=[
            {"tool": "refund", "param": "order_id", "value": "ORD-9999", "reason": "无法溯源"}
        ],
    )
    diagnostic = case_diagnostic(result)
    assert diagnostic["fix_hint"] == "参数与 schema 不符：order_id"
    assert diagnostic["assertion"] == "param:order_id"
    assert diagnostic["actual"] == "ORD-9999"


def test_missing_evidence_is_reported_not_invented():
    assert case_diagnostic(failed(evidence_span=""))["fix_hint"] == "证据缺失：unknown"
    assert case_diagnostic(failed(evidence_span="round:0"))["fix_hint"] == "证据缺失：round:0"
    # 有证据定位，但既不是断言失败也不是参数问题：不编诊断。
    assert case_diagnostic(failed(evidence_span="tool:shell")) is None
    # 通过样本没有诊断。
    assert case_diagnostic(make_result()) is None


def test_gate_skips_untrusted_and_precondition_failures():
    harness = failed(judge_layer="harness", data_valid=False, fail_reason="Harness_Error")
    assert evaluate_gate([harness, make_result(case_id="case-002")]).diagnostics == []

    precondition = failed(
        fail_reason="State_Mismatch",
        failure_class="precondition_failed",
        evidence_span="state:notes.md",
        evidence=json.dumps([{"path": "notes.md", "expected": True, "actual": False}]),
    )
    assert evaluate_gate([precondition, make_result(case_id="case-002")]).diagnostics == []


def test_blocked_gate_carries_diagnostics_into_report(tmp_path):
    mismatch = [{"path": "notes.md", "op": "exists", "expected": True, "actual": False}]
    results = [
        failed(fail_reason="State_Mismatch", evidence=json.dumps(mismatch), evidence_span="state:notes.md"),
        make_result(case_id="case-002"),
    ]
    gate = evaluate_gate(results)
    assert gate.status == "blocked"
    assert gate.to_dict()["diagnostics"][0]["fix_hint"] == "终态与期望不符：notes.md"

    summary = generate_markdown(results, str(tmp_path / "report.md"), write_ledger=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert summary["diagnostics_count"] == 1
    assert summary["gate"]["diagnostics"][0]["case_id"] == "case-001"
    assert "### 可自纠诊断（gate.diagnostics）" in report
    assert "终态与期望不符：notes.md" in report


def test_explain_lines_are_one_line_per_case_and_filterable():
    diagnostics = [
        {
            "case_id": "case-001",
            "assertion": "state:notes.md",
            "expected": "True",
            "actual": "False",
            "evidence_span": "state:notes.md",
            "fix_hint": "终态与期望不符：notes.md",
        },
        {
            "case_id": "case-002",
            "assertion": "param:order_id",
            "expected": "参数值必须能在输入或工具结果中找到来源",
            "actual": "ORD-9999",
            "evidence_span": "tool:refund",
            "fix_hint": "参数与 schema 不符：order_id",
        },
    ]
    lines = _explain_lines(diagnostics)
    assert len(lines) == 2
    assert lines[0].startswith("[case-001] 终态与期望不符：notes.md")
    assert "\n" not in lines[0]
    assert _explain_lines(diagnostics, "case-002") == [lines[1]]
    # 逗号分隔可一次追多条；不存在的 case 返回空列表而不是退回全量。
    assert _explain_lines(diagnostics, "case-001,case-002") == lines
    assert _explain_lines(diagnostics, "not-exist") == []
    assert _explain_lines([], "*") == []
