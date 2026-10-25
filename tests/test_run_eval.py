"""run_eval 落盘契约：逐次轨迹与逐次结论的 attempt 必须一一对应。

历史问题：`AgentTrace.attempt` 是 0 起、`EvalResult.attempts` 是 1 起，而且合并后
“最差的一次”被挪到列表末尾。外部脚本按 attempt 关联轨迹和结论会错位一对。
"""
import argparse

from run_eval import _attempts_payload

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
