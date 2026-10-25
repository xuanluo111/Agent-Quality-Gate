from src.engine.report import dimension_breakdown, generate_markdown, summarize
from tests.conftest import make_result


def ok(case_id, category="工具与行动安全"):
    return make_result(case_id=case_id, category=category)


def bad(case_id, category="工具与行动安全", **overrides):
    payload = {
        "passed": False,
        "strict_passed": False,
        "risk_level": "high",
        "judge_reason": "failed",
        "fail_reason": "Fail_Instruction_Following",
    }
    payload.update(overrides)
    return make_result(case_id=case_id, category=category, **payload)


def row(rows, dimension, value):
    return next(item for item in rows if item["dimension"] == dimension and item["value"] == value)


def test_unknown_follows_the_gate_exclude_switch():
    results = [
        ok("a1"),
        ok("a2"),
        ok("a3"),
        bad("u1", failure_class="unknown", data_valid=False, fail_reason="Harness_Error"),
    ]
    # 默认与门禁同口径：unknown 留在分母（门禁默认也不摘），4 条里 3 条通过。
    default_rows = dimension_breakdown(results, "passed")
    category = row(default_rows, "category", "工具与行动安全")
    assert category["n"] == 4
    assert category["rate"] == 75.0

    # 打开开关才摘除，且不再作为 failure_class 的一个取值出现。
    strict_rows = dimension_breakdown(results, "passed", exclude_unknown=True)
    assert row(strict_rows, "category", "工具与行动安全")["n"] == 3
    assert row(strict_rows, "category", "工具与行动安全")["rate"] == 100.0
    assert all(item["value"] != "unknown" for item in strict_rows if item["dimension"] == "failure_class")


def test_precondition_failed_never_counts_even_without_the_switch():
    # 前置条件不满足不是模型行为，门禁的 scored 恒排除它，维度表也必须一样。
    results = [
        ok("a1"),
        ok("a2"),
        bad("p1", failure_class="precondition_failed", data_valid=False, fail_reason="Harness_Error"),
    ]
    for exclude in (False, True):
        rows = dimension_breakdown(results, "passed", exclude_unknown=exclude)
        assert row(rows, "category", "工具与行动安全")["n"] == 2
        assert all(
            item["value"] != "precondition_failed" for item in rows if item["dimension"] == "failure_class"
        )


def test_gate_status_row_reuses_gate_metrics_instead_of_recomputing():
    # 9 通过 + 1 未测：门禁默认把它们放进分母，得 90%；维度表必须给同一个数，
    # 否则同一报告里会出现两个通过率，还会把 90% 错标成“饱和、无区分度”。
    results = [ok(f"a{i}") for i in range(9)] + [
        bad("u1", failure_class="unknown", data_valid=False, fail_reason="Harness_Error")
    ]
    summary = summarize(results)
    metrics = summary["gate"]["metrics"]
    gate_row = row(summary["dimension_breakdown"], "gate_status", summary["gate"]["status"])

    assert metrics["strict_rate"] == 90.0
    assert (gate_row["n"], gate_row["passed"], gate_row["rate"]) == (
        metrics["scored_cases"],
        metrics["strict_pass"],
        metrics["strict_rate"],
    )
    assert gate_row["note"] == ""


def test_saturation_and_floor_are_labelled_without_discrimination():
    saturated = dimension_breakdown([ok("a1"), ok("a2")], "passed")
    assert row(saturated, "category", "工具与行动安全")["note"] == "饱和（100%，该维度无区分度）"

    floor = dimension_breakdown([bad("b1"), bad("b2")], "blocked")
    assert row(floor, "category", "工具与行动安全")["note"] == "地板（0%，该维度无区分度）"


def test_mixed_dimension_reports_wilson_interval():
    results = [ok("a1"), ok("a2"), bad("a3"), bad("a4")]
    rows = dimension_breakdown(results, "blocked")
    mixed = row(rows, "category", "工具与行动安全")

    assert mixed["n"] == 4
    assert mixed["passed"] == 2
    assert mixed["rate"] == 50.0
    assert mixed["ci_low"] < 50.0 < mixed["ci_high"]
    assert mixed["note"] == ""


def test_report_renders_dimension_table_without_weighted_score(tmp_path):
    results = [ok("a1"), ok("a2"), bad("b1", category="prompt_injection")]
    summary = summarize(results)
    assert summary["gate"]["status"] == "blocked"
    rows = summary["dimension_breakdown"]
    assert row(rows, "gate_status", summary["gate"]["status"])["n"] == 3
    assert row(rows, "category", "prompt_injection")["note"].startswith("地板")

    generate_markdown(results, str(tmp_path / "report.md"), write_ledger=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "### 维度分层（n / 通过率 / Wilson 95%）" in report
    assert "| 用例类别 | `工具与行动安全` | 2 | 100.00% |" in report
    assert "该维度无区分度" in report
    assert "不提供加权总分" in report
    # 分母里有什么必须写清：可逐行复算，也说明 unknown 默认没被摘。
    assert "口径与门禁一致" in report
    assert "可逐行复算" in report
    assert "`unknown`（未测到）0 条仍在分母里" in report


def test_report_note_switches_to_exclude_wording(tmp_path):
    results = [ok("a1"), bad("u1", failure_class="unknown", data_valid=False, fail_reason="Harness_Error")]
    generate_markdown(results, str(tmp_path / "r.md"), write_ledger=False, baseline={"exclude_unknown": True})
    report = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "`unknown`（未测到）1 条也已排除（与 `--exclude-unknown` 同口径）" in report
    assert "仍在分母里" not in report
