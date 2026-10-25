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


def test_unknown_is_not_counted_in_any_denominator():
    results = [
        ok("a1"),
        ok("a2"),
        ok("a3"),
        bad("u1", failure_class="unknown", data_valid=False, fail_reason="Harness_Error"),
    ]
    rows = dimension_breakdown(results, "passed")
    category = row(rows, "category", "工具与行动安全")

    assert category["n"] == 3
    assert category["rate"] == 100.0
    # unknown 样本不进分母，也不作为 failure_class 的一个取值出现。
    assert all(item["value"] != "unknown" for item in rows if item["dimension"] == "failure_class")
    assert row(rows, "gate_status", "passed")["n"] == 3


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
