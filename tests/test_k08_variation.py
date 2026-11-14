"""K08 变序：重复采样逐次派生种子，并量化「N 次尝试到底分化了多少」。

两个必须钉住的边界：

1. **attempt=0 的种子必须与旧行为一字不差**。否则单跑（`repeat=1`）的样本也被换了
   seed，历史批次与 11/19 基线立刻不可比——这条改动的收益全部在 repeat 场景，
   不该有单跑场景的代价。
2. **`cases_with_single_seed > 0` 时稳定性数字不可引用**。它是"机制没生效"的显式
   信号（变序未接上、或网关吞掉了 seed），不能混进"模型本来就稳"里。
"""
from src.engine.stability import attempt_divergence, divergence_summary
from tests.conftest import FakeClient, make_case, make_trace, text_response
from tests.test_runner import build_runner


# ---------- 种子派生 ----------


def test_first_attempt_keeps_base_seed(tmp_path):
    """单跑与历史批次的语义一个字都不能变。"""
    runner = build_runner(tmp_path, [], client=FakeClient([]), seed=42)

    assert runner.attempt_seed(0) == 42


def test_later_attempts_get_distinct_seeds(tmp_path):
    runner = build_runner(tmp_path, [], client=FakeClient([]), seed=42)
    seeds = [runner.attempt_seed(index) for index in range(1, 5)]

    assert 42 not in seeds
    assert len(set(seeds)) == len(seeds)


def test_attempt_seed_is_deterministic(tmp_path):
    """同 base seed + 同轮次必须复现同一 seed：否则跑批结果无法重放。"""
    first = build_runner(tmp_path / "a", [], client=FakeClient([]), seed=7)
    second = build_runner(tmp_path / "b", [], client=FakeClient([]), seed=7)

    assert [first.attempt_seed(i) for i in range(4)] == [second.attempt_seed(i) for i in range(4)]


def test_attempt_seed_is_none_without_base(tmp_path):
    runner = build_runner(tmp_path, [], client=FakeClient([]))

    assert runner.attempt_seed(0) is None
    assert runner.attempt_seed(3) is None


def test_derived_seed_stays_in_gateway_range(tmp_path):
    """OpenAI 兼容网关的 seed 上界是 2^31-1，越界会被直接拒绝。"""
    runner = build_runner(tmp_path, [], client=FakeClient([]), seed=2**31 - 10)

    for attempt in range(1, 5):
        assert 0 <= runner.attempt_seed(attempt) <= 2**31 - 1


# ---------- 请求与轨迹 ----------


def test_runner_sends_base_seed_then_derived_seed(tmp_path):
    client = FakeClient([text_response("完成。"), text_response("完成。")])
    runner = build_runner(tmp_path, [], client=client, seed=42)
    case = make_case(id="seed-e2e")

    first = runner.run_case(case, attempt=0)
    second = runner.run_case(case, attempt=1)

    assert client.calls[0]["seed"] == 42
    assert client.calls[1]["seed"] == runner.attempt_seed(1)
    assert first.seed == 42
    assert second.seed == runner.attempt_seed(1)
    assert first.seed != second.seed


def test_trace_records_seed_for_audit(tmp_path):
    """变序是否生效必须能事后核对，不能只靠「我们改过代码」这句话。"""
    client = FakeClient([text_response("完成。")])
    runner = build_runner(tmp_path, [], client=client, seed=9)

    trace = runner.run_case(make_case(id="audit"), attempt=2)

    assert trace.seed == client.calls[0]["seed"] == runner.attempt_seed(2)


def test_no_seed_means_no_seed_kwarg(tmp_path):
    """没配 seed 时不传这个键：部分网关对未知参数直接报错。"""
    client = FakeClient([text_response("完成。")])
    runner = build_runner(tmp_path, [], client=client)

    trace = runner.run_case(make_case(id="no-seed"), attempt=1)

    assert "seed" not in client.calls[0]
    assert trace.seed is None


# ---------- 分化度指标 ----------


def test_attempt_divergence_flags_single_seed():
    """三次尝试同种子、同工具、同回话：机制没生效，指标必须诚实地全 1.0。"""
    traces = [
        make_trace(agent_response="平均值是 30.0", tool_call_sequence=["file_read"], seed=7)
        for _ in range(3)
    ]
    info = attempt_divergence(traces)

    assert info["attempts"] == 3
    assert info["seed_unique"] == 1
    assert info["tool_jaccard"] == 1.0
    assert info["reply_jaccard"] == 1.0


def test_attempt_divergence_measures_both_axes():
    traces = [
        make_trace(
            agent_response="平均值是 30.0，已写入 report.txt",
            tool_call_sequence=["file_read", "write_file"],
            seed=1,
        ),
        make_trace(agent_response="平均值是 28.5", tool_call_sequence=["file_read"], seed=2),
    ]
    info = attempt_divergence(traces)

    assert info["seed_unique"] == 2
    # 工具集 {file_read, write_file} vs {file_read} → 交集 1 / 并集 2
    assert info["tool_jaccard"] == 0.5
    assert 0.0 < info["reply_jaccard"] < 1.0


def test_single_trace_has_no_divergence():
    info = attempt_divergence([make_trace(seed=1)])

    assert info["attempts"] == 1
    assert info["tool_jaccard"] is None
    assert info["reply_jaccard"] is None


def test_empty_replies_counted_not_scored():
    """两次都没回答不是“稳定”，但也不该按 1.0 混进平均把分化度拉平。"""
    info = attempt_divergence([make_trace(agent_response="", seed=1), make_trace(agent_response="", seed=2)])

    assert info["empty_reply_pairs"] == 1
    assert info["reply_jaccard"] is None


def test_divergence_summary_counts_single_seed_cases():
    per_case = [
        {"attempts": 3, "seed_unique": 1, "tool_jaccard": 1.0, "reply_jaccard": 1.0, "empty_reply_pairs": 0},
        {"attempts": 3, "seed_unique": 3, "tool_jaccard": 0.5, "reply_jaccard": 0.3, "empty_reply_pairs": 0},
        # repeat=1 的用例不参与：它本来就没有“两次”可分化的对象
        {"attempts": 1, "seed_unique": 1, "tool_jaccard": None, "reply_jaccard": None, "empty_reply_pairs": 0},
    ]
    summary = divergence_summary(per_case)

    assert summary["cases"] == 2
    assert summary["cases_with_single_seed"] == 1
    assert summary["tool_jaccard"] == 0.75
    assert summary["reply_jaccard"] == 0.65


def test_divergence_summary_empty_when_repeat_disabled():
    summary = divergence_summary([])

    assert summary == {
        "cases": 0,
        "cases_with_single_seed": 0,
        "tool_jaccard": None,
        "reply_jaccard": None,
        "empty_reply_pairs": 0,
    }
