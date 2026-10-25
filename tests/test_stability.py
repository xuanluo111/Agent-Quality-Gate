from run_eval import run_single_case

from src.engine.gate import evaluate_gate
from src.engine.report import generate_markdown, summarize
from src.engine.stability import attempt_outcomes, merge_attempts, stability_metrics
from src.models.schema import AttemptResult
from tests.conftest import make_case, make_result, make_trace


def flaky_result(case_id: str = "c1"):
    return make_result(
        case_id=case_id,
        attempts=[
            AttemptResult(attempt=1, passed=True, strict_passed=True, risk_level="low"),
            AttemptResult(attempt=2, passed=False, strict_passed=False, risk_level="high", fail_reason="Policy_Violation"),
        ],
    )


def stable_result(case_id: str = "c2"):
    return make_result(
        case_id=case_id,
        attempts=[
            AttemptResult(attempt=1, passed=True, strict_passed=True, risk_level="low"),
            AttemptResult(attempt=2, passed=True, strict_passed=True, risk_level="low"),
        ],
    )


def test_merge_attempts_takes_worst_and_keeps_every_outcome():
    first = (make_trace(latency_ms=10.0), make_result(case_id="c1"))
    second = (
        make_trace(latency_ms=20.0),
        make_result(
            case_id="c1",
            passed=False,
            strict_passed=False,
            risk_level="critical",
            fail_reason="Policy_Violation",
            judge_reason="越权写入",
            token_cost=5,
        ),
    )
    trace, merged = merge_attempts([first, second], run_id="run-1", model="fake")

    assert merged.strict_passed is False
    assert merged.risk_level == "critical"
    assert merged.token_cost == 5
    assert trace is second[0]
    assert [item.attempt for item in merged.attempts] == [1, 2]
    assert [item.strict_passed for item in merged.attempts] == [True, False]
    assert merged.attempts[0].latency_ms == 10.0
    assert merged.judge_reason.startswith("[不稳定] 1/2 次通过")
    assert (merged.run_id, merged.model) == ("run-1", "fake")


def test_merge_attempts_all_pass_keeps_original_reason():
    _, merged = merge_attempts([(make_trace(), make_result(case_id="c1")), (make_trace(), make_result(case_id="c1"))])
    assert merged.strict_passed is True
    assert merged.judge_reason == "ok"


def test_merge_attempts_requires_at_least_one_attempt():
    try:
        merge_attempts([])
    except ValueError as exc:
        assert "至少一次" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空尝试必须报错")


def test_attempt_outcomes_falls_back_to_single_result():
    assert attempt_outcomes(make_result(strict_passed=False)) == [False]
    assert attempt_outcomes(flaky_result()) == [True, False]


def test_stability_metrics_separates_capability_from_reliability():
    metrics = stability_metrics([flaky_result(), stable_result()])
    assert metrics["repeat"] == 2
    assert metrics["pass_at_k"] == 100.0
    assert metrics["pass_pow_k"] == 50.0
    assert metrics["stability_rate"] == 50.0
    assert metrics["flaky_count"] == 1
    assert metrics["flaky_cases"] == ["c1"]


def test_stability_metrics_without_attempts_reports_single_run():
    metrics = stability_metrics([make_result(strict_passed=False, passed=False)])
    assert metrics["repeat"] == 1
    assert metrics["pass_pow_k"] == 0.0
    assert metrics["flaky_count"] == 0


def test_gate_require_stable_blocks_flaky_run():
    decision = evaluate_gate([flaky_result()], strict_baseline=80.0, require_stable=True)
    assert decision.status == "blocked"
    assert any("pass^2" in reason for reason in decision.reasons)
    assert decision.metrics["pass_pow_k"] == 0.0
    assert decision.metrics["require_stable"] is True


def test_gate_without_require_stable_ignores_flakiness():
    decision = evaluate_gate([flaky_result()], strict_baseline=80.0)
    assert decision.status == "passed"
    assert decision.metrics["pass_at_k"] == 100.0


def test_gate_require_stable_is_noop_for_single_attempt():
    decision = evaluate_gate([make_result()], strict_baseline=80.0, require_stable=True)
    assert decision.status == "passed"
    assert decision.metrics["repeat"] == 1


def test_summarize_exposes_stability_and_attribution():
    failed = make_result(
        case_id="fab",
        passed=True,
        strict_passed=False,
        risk_level="high",
        fail_reason="Fabricated_Parameter",
        failure_stage="generation",
        evidence_span="tool:refund",
        provenance_violations=[{"tool": "refund", "param": "order_id", "value": "ORD-9999"}],
    )
    summary = summarize(
        [flaky_result("c1"), failed],
        run_meta={"run_id": "run-1", "model": "fake-model"},
        require_stable=True,
    )
    assert summary["repeat"] == 2
    assert summary["flaky_count"] == 1
    assert summary["require_stable"] is True
    assert summary["failure_stage_distribution"] == {"generation": 1}
    assert summary["repair_attribution"][0]["stage"] == "generation"
    assert summary["fabricated_parameter_count"] == 1
    assert summary["provenance_violations"] == 1


def test_markdown_renders_stage_section_and_new_risk_table(tmp_path):
    failed_state = make_result(
        case_id="state-case",
        passed=False,
        strict_passed=False,
        risk_level="high",
        fail_reason="State_Mismatch",
        failure_stage="validation",
        evidence_span="state:summary.md",
        state_diff={"created": ["summary.md"]},
    )
    failed_fab = make_result(
        case_id="fab-case",
        passed=True,
        strict_passed=False,
        risk_level="high",
        fail_reason="Fabricated_Parameter",
        failure_stage="generation",
        evidence_span="tool:refund",
        provenance_violations=[{"tool": "refund", "param": "order_id", "value": "ORD-9999"}],
    )
    report_path = tmp_path / "report.md"
    summary = generate_markdown(
        [failed_state, failed_fab, make_result(case_id="ok-case")],
        str(report_path),
        run_meta={"run_id": "run-1", "model": "fake-model", "repeat": 2, "require_stable": True},
        ledger_path=tmp_path / "risk_ledger.json",
        write_ledger=False,
    )
    text = report_path.read_text(encoding="utf-8")

    assert "## 六、失败环节分布与修复归属" in text
    assert "新增风险类型（iRTE 2026）" in text
    assert "State_Mismatch" in text and "Fabricated_Parameter" in text
    # 旧章节标题保持原样，历史报告解析脚本仍可用。
    assert "## 三、故障事件聚合" in text
    assert "失败用例根因分析（仅可信样本）" in text
    assert summary["state_mismatch_count"] == 1
    assert summary["failure_stage_distribution"] == {"validation": 1, "generation": 1}


class StubRunner:
    def __init__(self, traces):
        self.traces = traces

    def run_case(self, case, attempt=0):
        return self.traces[attempt]


class StubJudge:
    def __init__(self, results):
        self.results = results

    def evaluate(self, case, trace):
        return self.results[trace.attempt].model_copy(deep=True)


def test_run_single_case_pairs_result_with_its_trace():
    """results.jsonl 只存一条 trace，它必须是合并结论对应的那次，否则重判会错配。"""
    traces = [make_trace(attempt=0), make_trace(attempt=1)]
    results = [
        make_result(
            passed=False,
            strict_passed=False,
            risk_level="critical",
            fail_reason="Policy_Violation",
            judge_reason="越权写入",
        ),
        make_result(case_id="c1"),
    ]
    runner, judge = StubRunner(traces), StubJudge(results)

    paired, merged = run_single_case(make_case(), runner, judge, "run-1", "fake-model", repeat=2)

    assert merged.strict_passed is False
    assert merged.risk_level == "critical"
    assert [item.attempt for item in merged.attempts] == [1, 2]
    # 最差的是第 1 次尝试，必须被排到末尾供写盘/重判配对
    assert [trace.attempt for trace in paired] == [1, 0]
    assert paired[-1] is traces[0]

