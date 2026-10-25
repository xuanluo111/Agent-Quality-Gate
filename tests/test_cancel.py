from types import SimpleNamespace

from src.engine.failure_class import UNKNOWN, classify_failure
from src.engine.report import generate_markdown, summarize
from src.engine.runner import AgentRunner
from src.models.schema import NO_FINAL_RESPONSE_TEXT
from tests.conftest import FakeClient, make_case, make_result, make_trace, text_response


def build_runner(tmp_path, responses, **overrides):
    params = {
        "api_base": "https://example.invalid/v1",
        "api_key": "test-key",
        "model": "fake-model",
        "workspace_root": str(tmp_path / "workspace"),
        "run_id": "test-run",
        "client": FakeClient(responses),
    }
    params.update(overrides)
    return AgentRunner(**params)


class _CancelDuringCall:
    """在模型调用进行中请求取消：模拟“请求来得太晚，循环已经收尾”。"""

    def __init__(self, runner, response):
        self.runner = runner
        self.response = response
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self.runner.request_cancel()
        return self.response


def test_request_cancel_before_start_stops_at_round_boundary(tmp_path):
    case = make_case()
    runner = build_runner(tmp_path, [])
    request_time = runner.request_cancel()

    trace = runner.run_case(case)

    assert trace.cancel_requested_at == request_time
    # 真的停下了：在第一个轮次边界就退出，一次模型调用都没发。
    assert trace.stopped_at
    assert runner.client.calls == []
    assert trace.agent_response == NO_FINAL_RESPONSE_TEXT


def test_cancel_request_is_not_retimestamped(tmp_path):
    runner = build_runner(tmp_path, [])
    first = runner.request_cancel()
    second = runner.request_cancel()

    assert first == second


def test_cancel_arriving_after_last_call_leaves_stopped_at_empty(tmp_path):
    case = make_case()
    runner = build_runner(tmp_path, [])
    completions = _CancelDuringCall(runner, text_response("已完成。"))
    runner.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    trace = runner.run_case(case)

    assert completions.calls == 1
    assert trace.agent_response == "已完成。"
    # 请求登记了，但循环已经自然结束：没有 stopped_at，不能说“停住了”。
    assert trace.cancel_requested_at
    assert trace.stopped_at is None


def test_cancel_without_stop_is_classified_unknown():
    result = make_result(
        passed=False,
        strict_passed=False,
        risk_level="high",
        fail_reason="Fail_Instruction_Following",
    )
    unstopped = make_trace(cancel_requested_at="2026-10-25 10:00:00")
    assert classify_failure(result, unstopped) == UNKNOWN

    # 真的停下属于正常终止口径，不能因为请求过取消就一律归 unknown。
    stopped = make_trace(cancel_requested_at="2026-10-25 10:00:00", stopped_at="2026-10-25 10:00:03")
    assert classify_failure(result, stopped) is None


def test_report_lists_cancel_request_and_stop_separately(tmp_path):
    results = [
        make_result(),
        make_result(
            case_id="cancel-unstopped",
            passed=False,
            strict_passed=False,
            risk_level="high",
            failure_class=UNKNOWN,
            cancel_requested_at="2026-10-25 10:00:00",
        ),
        make_result(
            case_id="cancel-stopped",
            passed=False,
            strict_passed=False,
            risk_level="high",
            cancel_requested_at="2026-10-25 10:00:00",
            stopped_at="2026-10-25 10:00:03",
        ),
    ]
    summary = summarize(results)
    assert summary["cancel_requested_count"] == 2
    assert summary["cancel_stopped_count"] == 1
    assert summary["cancel_unstopped_count"] == 1

    generate_markdown(results, str(tmp_path / "report.md"), write_ledger=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "| 取消请求未生效 | 1 |" in report
    assert "未停住 `1` 条" in report
