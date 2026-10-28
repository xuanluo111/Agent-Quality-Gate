"""run_eval 落盘契约：逐次轨迹与逐次结论的 attempt 必须一一对应。

历史问题：`AgentTrace.attempt` 是 0 起、`EvalResult.attempts` 是 1 起，而且合并后
“最差的一次”被挪到列表末尾。外部脚本按 attempt 关联轨迹和结论会错位一对。
"""
import argparse

from run_eval import _attempts_payload, _skipped_result, _trip_reason

from src.config import add_run_args, config_from_args
from src.engine.stability import merge_attempts
from tests.conftest import make_result, make_trace


def test_attempts_payload_sorts_and_renumbers_from_one():
    traces = [
        make_trace(case_id="c1", attempt=1, agent_response="第二次"),
        make_trace(case_id="c1", attempt=0, agent_response="第一次"),
    ]
    payload = _attempts_payload(traces)
    assert [item["attempt"] for item in payload] == [1, 2]
    # 顺序也要纠正回来：落盘顺序 = 尝试顺序，而不是“最差在最后”的排列。
    assert [item["agent_response"] for item in payload] == ["第一次", "第二次"]


def test_attempts_payload_matches_merged_result_attempts():
    """第一次失败、第二次通过时，轨迹会被挪成 [第二次, 第一次]，重编号后仍要对齐。"""
    first = (
        make_trace(case_id="c1", attempt=0, agent_response="第一次"),
        make_result(
            case_id="c1",
            passed=False,
            strict_passed=False,
            risk_level="critical",
            fail_reason="Policy_Violation",
        ),
    )
    second = (make_trace(case_id="c1", attempt=1, agent_response="第二次"), make_result(case_id="c1"))

    worst_trace, merged = merge_attempts([first, second], run_id="r", model="m")
    traces = [trace for trace in (first[0], second[0]) if trace is not worst_trace] + [worst_trace]
    assert [trace.attempt for trace in traces] == [1, 0], "前置条件：合并后最差的一次在末尾"

    payload = _attempts_payload(traces)
    assert [item["attempt"] for item in payload] == [item.attempt for item in merged.attempts]
    assert [item["agent_response"] for item in payload] == ["第一次", "第二次"]


def _config(*argv: str):
    parser = argparse.ArgumentParser()
    add_run_args(parser)
    return config_from_args(parser.parse_args(["--suite", "smoke", *argv]))


def test_publish_is_on_by_default():
    """默认行为不变：正式评测仍然更新 latest.md / observability.json / 台账。"""
    assert _config().publish is True


def test_no_publish_keeps_selfcheck_runs_out_of_shared_artifacts():
    """自检跑 6 条冒烟，不能把对外报告和台账改成自检结果。"""
    assert _config("--no-publish").publish is False


def test_regression_only_takes_real_model_failures():
    from run_eval import regression_candidates
    from tests.conftest import make_case

    case = make_case()
    case_map = {case.id: case}
    # 环境故障（429）在 _harness_failure 里被记成 high，但它根本不是模型答错。
    harness = make_result(
        passed=False, strict_passed=False, risk_level="high",
        judge_layer="harness", fail_reason="Harness_Error", failure_class="unknown",
    )
    precondition = make_result(
        passed=False, strict_passed=False, risk_level="critical", failure_class="precondition_failed",
    )
    model_error = make_result(passed=False, strict_passed=False, risk_level="critical", failure_class=None)
    low = make_result(passed=False, strict_passed=False, risk_level="medium", failure_class=None)
    passed = make_result()

    picked = regression_candidates([harness, precondition, model_error, low, passed], case_map)
    assert picked == [case]
    assert regression_candidates([harness, precondition], case_map) == []


def test_regression_append_never_writes_the_same_id_twice(tmp_path):
    from run_eval import _append_regression

    from tests.conftest import make_case

    path = tmp_path / "regression.jsonl"
    case = make_case()

    assert _append_regression(path, [case]) == 1
    # 同一个失败用例再跑一轮不能再追加：文件每多一行，`--suite regression` 就多烧一次调用。
    assert _append_regression(path, [case]) == 0
    # 同 id 但内容是另一套场景也不行：id 是去重的唯一依据，得先去改 id 再入库。
    assert _append_regression(path, [make_case(input="另一个场景")]) == 0

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1


def test_resilience_args_wire_into_config():
    """C11 韧性参数走 CLI→config：默认开启退避+熔断，急停默认关（0）。"""
    default = _config()
    assert default.max_retries == 3
    assert default.breaker_threshold == 5
    assert default.abort_on_critical == 0  # 急停需显式开启，默认不改变既有行为
    assert default.abort_file == ""
    tuned = _config(
        "--max-retries", "0",
        "--retry-base-delay", "1.5",
        "--breaker-threshold", "3",
        "--abort-on-critical", "2",
        "--abort-file", "/tmp/aqg-abort",
    )
    assert tuned.max_retries == 0
    assert tuned.retry_base_delay == 1.5
    assert tuned.breaker_threshold == 3
    assert tuned.abort_on_critical == 2
    assert tuned.abort_file == "/tmp/aqg-abort"


def test_trip_reason_breaker_beats_critical_and_signal():
    """三条护栏都命中时，熔断优先（网关整体挂了，继续只白烧额度）。"""
    reason = _trip_reason(
        consecutive_harness=5, critical_strict=9, breaker_threshold=5,
        abort_on_critical=3, abort_file_present=True,
    )
    assert reason is not None and reason.startswith("熔断")


def test_trip_reason_thresholds_gate_each_guard():
    """阈值 0=关闭对应护栏；未达阈不触发；达阈才停。"""
    assert _trip_reason(consecutive_harness=9, critical_strict=0, breaker_threshold=0,
                        abort_on_critical=0, abort_file_present=False) is None
    assert _trip_reason(consecutive_harness=4, critical_strict=0, breaker_threshold=5,
                        abort_on_critical=0, abort_file_present=False) is None
    assert _trip_reason(consecutive_harness=0, critical_strict=2, breaker_threshold=0,
                        abort_on_critical=3, abort_file_present=False) is None
    crit = _trip_reason(consecutive_harness=0, critical_strict=3, breaker_threshold=0,
                        abort_on_critical=3, abort_file_present=False)
    assert crit is not None and crit.startswith("急停") and "Critical" in crit
    external = _trip_reason(consecutive_harness=0, critical_strict=0, breaker_threshold=0,
                            abort_on_critical=0, abort_file_present=True)
    assert external is not None and "外部信号" in external


def test_skipped_result_is_unrated_environment_failure():
    """停摆未评的用例：统一 Harness_Error、低风险、不当模型失分。"""
    from tests.conftest import make_case

    case = make_case(id="skipped-one")
    result = _skipped_result(case, "run-1", "m", "熔断：连续 5 条 Harness_Error")
    assert result.fail_reason == "Harness_Error"
    assert result.passed is False and result.strict_passed is False
    assert result.risk_level == "low"  # 不能把环境停摆标成高危，否则污染高危统计
    assert result.judge_layer == "harness"
