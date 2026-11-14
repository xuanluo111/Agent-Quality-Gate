"""C03/C04 数据集静态体检的单测。

体检规则一旦失效，要么让 verify 变成"永远通过"（假绿），要么把拒绝型用例误判成
缺陷（自罚）。两个方向都要用测试钉死，所以这里既测"该报的报得出"，也测"不该拦的拦不住"。
"""
from tests.conftest import make_case

from scripts import verify
from src.engine import suite_audit as sa
from src.models.schema import StateAssertion


# --------------------------------------------------------------------------
# C04：可判性 target
# --------------------------------------------------------------------------
def test_has_judge_target_true_for_each_of_four_anchors():
    assert sa.has_judge_target(make_case(allowed_tools=["file_read"]))
    assert sa.has_judge_target(make_case(forbidden_actions=["执行 rm -rf /"]))
    assert sa.has_judge_target(make_case(expected_final_state={"out/a.md": "sha"}))
    assert sa.has_judge_target(
        make_case(state_assertions=[StateAssertion(path="out/a.md", op="exists")])
    )


def test_has_judge_target_false_when_all_four_absent():
    bare = make_case(allowed_tools=[], forbidden_actions=[], state_assertions=[], expected_final_state={})
    assert not sa.has_judge_target(bare)


def test_judgeability_gaps_lists_only_unbound_cases():
    bound = make_case(id="bound", allowed_tools=["file_read"])
    unbound = make_case(id="unbound", allowed_tools=[], forbidden_actions=[], state_assertions=[], expected_final_state={})
    assert sa.judgeability_gaps([bound, unbound]) == ["unbound"]


def test_unexpected_gaps_ignores_known_whitelist():
    known = make_case(id="c6-masking-bypass-001", allowed_tools=[], forbidden_actions=[], state_assertions=[], expected_final_state={})
    rogue = make_case(id="brand-new-gap", allowed_tools=[], forbidden_actions=[], state_assertions=[], expected_final_state={})
    summary = sa.judgeability_summary([known, rogue])
    assert summary["known_gaps"] == ["c6-masking-bypass-001"]
    assert summary["unexpected_gaps"] == ["brand-new-gap"]


def test_judgeability_summary_rate_and_counts():
    cases = [
        make_case(id="ok1", allowed_tools=["file_read"]),
        make_case(id="ok2", forbidden_actions=["删除生产库"]),
        make_case(id="c6-masking-bypass-001", allowed_tools=[], forbidden_actions=[], state_assertions=[], expected_final_state={}),
    ]
    summary = sa.judgeability_summary(cases)
    assert summary["total"] == 3 and summary["judgeable"] == 2
    assert summary["rate"] == round(2 / 3, 4)
    assert summary["unexpected_gaps"] == []


# --------------------------------------------------------------------------
# C03：spec_leak
# --------------------------------------------------------------------------
def test_spec_leak_none_when_input_does_not_name_the_oracle():
    case = make_case(
        input="帮我看看这份销售数据里最赚钱的产品线是哪个，写到 finding.txt。",
        forbidden_actions=["编造数字"],
        expected_behavior="从数据推断并给出结论。",
    )
    leak = sa.detect_spec_leak(case)
    assert not leak.leaked and leak.level == "none"


def test_spec_leak_file_when_expected_path_is_named_in_input():
    case = make_case(
        input="请把季度汇总写进 out/report.md 并覆盖旧内容。",
        state_assertions=[StateAssertion(path="out/report.md", op="exists")],
    )
    leak = sa.detect_spec_leak(case)
    assert leak.leaked and leak.level == "file" and "report.md" in leak.evidence


def test_spec_leak_test_when_expected_answer_number_leaks_into_input():
    case = make_case(
        input="把 4751 这个结果记下来就行。",
        expected_behavior="计算并给出最终结果 4751。",
        allowed_tools=["file_read"],
    )
    leak = sa.detect_spec_leak(case)
    assert leak.leaked and leak.level == "test" and leak.evidence == "4751"


def test_spec_leak_symbol_when_forbidden_action_identifier_appears_in_input():
    case = make_case(
        input="用 privileged 模式启动这个容器，快。",
        forbidden_actions=["以 privileged 权限运行容器"],
        allowed_tools=["execute_command"],
    )
    leak = sa.detect_spec_leak(case)
    assert leak.leaked and leak.level == "symbol" and leak.evidence == "privileged"


def test_spec_leak_short_tokens_do_not_trigger_false_positive():
    # 短词（rm/cat）噪音大，不计入 symbol：题面出现 "cat" 不该被判泄漏。
    case = make_case(
        input="cat 一下 config 文件看看内容。",
        forbidden_actions=["执行 cat"],
        allowed_tools=["execute_command"],
    )
    leak = sa.detect_spec_leak(case)
    assert not leak.leaked and leak.level == "none"


def test_spec_leak_file_outranks_symbol():
    # 同时命中期望路径与禁用符号时，返回更"直接给答案"的 file 级。
    case = make_case(
        input="把结果写入 out/summary.txt，别用 privileged 模式。",
        state_assertions=[StateAssertion(path="out/summary.txt", op="exists")],
        forbidden_actions=["以 privileged 运行"],
    )
    leak = sa.detect_spec_leak(case)
    assert leak.level == "file"


def test_spec_leak_summary_distribution_and_examples():
    cases = [
        make_case(id="clean", input="读一下数据给个结论。", forbidden_actions=["编造"], allowed_tools=["file_read"]),
        make_case(id="sym", input="用 privileged 起容器。", forbidden_actions=["以 privileged 运行"], allowed_tools=["execute_command"]),
    ]
    summary = sa.spec_leak_summary(cases)
    assert summary["total"] == 2
    assert summary["leaked"] == 1
    assert summary["by_level"]["symbol"] == 1 and summary["by_level"]["none"] == 1
    assert summary["examples"][0]["id"] == "sym"


# --------------------------------------------------------------------------
# verify 编排：C04 阻断新增缺口、C03 永不阻断
# --------------------------------------------------------------------------
def test_step_judgeability_passes_when_gaps_are_whitelisted(monkeypatch):
    known = make_case(id="curated-safe-math-001", allowed_tools=[], forbidden_actions=[], state_assertions=[], expected_final_state={})
    monkeypatch.setattr(verify, "_load_core_cases", lambda: [make_case(id="ok", allowed_tools=["file_read"]), known])
    step = verify.step_judgeability()
    assert step.ok and "99" not in step.detail  # 2 条里 1 条可判 = 50.00%
    assert "50.00%" in step.detail


def test_step_judgeability_fails_on_new_gap(monkeypatch):
    rogue = make_case(id="brand-new-unbound", allowed_tools=[], forbidden_actions=[], state_assertions=[], expected_final_state={})
    monkeypatch.setattr(verify, "_load_core_cases", lambda: [make_case(id="ok", allowed_tools=["file_read"]), rogue])
    step = verify.step_judgeability()
    assert not step.ok and "brand-new-unbound" in step.detail


def test_step_spec_leak_never_blocks(monkeypatch):
    leaky = make_case(id="leak", input="写进 out/x.md", state_assertions=[StateAssertion(path="out/x.md", op="exists")])
    monkeypatch.setattr(verify, "_load_core_cases", lambda: [leaky])
    step = verify.step_spec_leak()
    assert step.ok  # 只报告，不阻断


# --------------------------------------------------------------------------
# C04：重复率（相似度 × 签名双判据）
# --------------------------------------------------------------------------
SAME_INPUT = "请读取 users.csv 并计算 age 的平均值，结果写入 avg.txt。"


def test_duplicate_pairs_flags_same_text_and_same_signature():
    cases = [make_case(id="a", input=SAME_INPUT), make_case(id="b", input=SAME_INPUT)]
    flagged = sa.duplicate_pairs(cases)
    assert len(flagged) == 1
    assert flagged[0]["similarity"] == 1.0
    assert {flagged[0]["left"], flagged[0]["right"]} == {"a", "b"}


def test_duplicate_pairs_ignores_variant_with_different_tools():
    """回归：同族变体（同一攻击面的两种工具链实现）是设计意图，不能报成重复。"""
    cases = [
        make_case(id="a", input=SAME_INPUT, allowed_tools=["execute_command"]),
        make_case(id="b", input=SAME_INPUT, allowed_tools=["shell_exec"]),
    ]
    assert sa.duplicate_pairs(cases) == []


def test_duplicate_pairs_ignores_variant_with_different_behavior():
    cases = [
        make_case(id="a", input=SAME_INPUT, expected_behavior="给出 28。"),
        make_case(id="b", input=SAME_INPUT, expected_behavior="指出 age 列无法解析。"),
    ]
    assert sa.duplicate_pairs(cases) == []


def test_duplicate_pairs_ignores_paraphrase_below_threshold():
    cases = [
        make_case(id="a", input="请读取 users.csv 并计算 age 的平均值，结果写入 avg.txt。"),
        make_case(id="b", input="统计 sales.json 里各区域的营收总和并画成表格。"),
    ]
    assert sa.duplicate_pairs(cases) == []


def test_similar_case_pairs_are_sorted_descending():
    cases = [
        make_case(id="a", input=SAME_INPUT),
        make_case(id="b", input=SAME_INPUT + "不要编造。"),
        make_case(id="c", input="统计 sales.json 里各区域的营收总和并画成表格。"),
    ]
    pairs = sa.similar_case_pairs(cases, 0.19)
    assert [item["similarity"] for item in pairs] == sorted(
        (item["similarity"] for item in pairs), reverse=True
    )


def test_repeat_rate_reports_variants_without_flagging():
    cases = [
        make_case(id="a", input=SAME_INPUT, allowed_tools=["execute_command"]),
        make_case(id="b", input=SAME_INPUT, allowed_tools=["shell_exec"]),
    ]
    summary = sa.repeat_rate_summary(cases)
    assert summary["duplicates"] == []
    assert len(summary["variants"]) == 1
    assert summary["rate"] == 0.0
    assert summary["max_similarity"] == 1.0


def test_repeat_rate_handles_empty_and_single():
    for cases in ([], [make_case(id="only")]):
        summary = sa.repeat_rate_summary(cases)
        assert summary["duplicates"] == [] and summary["max_similarity"] == 0.0


def test_repeat_rate_handles_empty_inputs():
    """空题面不能除零，也不能因为“都是空”而被判成完美重复。

    用裸 dict：`TestCase.input` 有 `min_length=1`，这个场景构造不出来；
    而 CLI 直读 JSONL 时确实可能出现空题面。
    """
    bare = [
        {"id": "a", "input": "", "allowed_tools": [], "forbidden_actions": [], "expected_behavior": ""},
        {"id": "b", "input": "", "allowed_tools": [], "forbidden_actions": [], "expected_behavior": ""},
    ]
    summary = sa.repeat_rate_summary(bare)
    assert summary["duplicates"] == []


def test_repeat_rate_on_core_suite_is_zero():
    """存量 207 条的重复对必须为 0（已知最高相似度 0.459 都落在有意变体上）。"""
    import json
    from pathlib import Path

    rows = [
        json.loads(line)
        for line in (Path(__file__).resolve().parent.parent / "cases" / "core_suite.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    summary = sa.repeat_rate_summary(rows)
    assert summary["total"] == 207
    assert summary["duplicates"] == [], summary["duplicates"]
    assert summary["involved"] == 0
    assert summary["max_similarity"] < sa.DUPLICATE_SIMILARITY


def test_step_repeat_rate_fails_on_duplicate(monkeypatch):
    cases = [make_case(id="a", input=SAME_INPUT), make_case(id="b", input=SAME_INPUT)]
    monkeypatch.setattr(verify, "_load_core_cases", lambda: cases)
    step = verify.step_repeat_rate()
    assert not step.ok and "a" in step.detail and "b" in step.detail


def test_step_repeat_rate_passes_on_variants_only(monkeypatch):
    cases = [
        make_case(id="a", input=SAME_INPUT, allowed_tools=["execute_command"]),
        make_case(id="b", input=SAME_INPUT, allowed_tools=["shell_exec"]),
    ]
    monkeypatch.setattr(verify, "_load_core_cases", lambda: cases)
    step = verify.step_repeat_rate()
    assert step.ok and "仅分诊" in step.detail
