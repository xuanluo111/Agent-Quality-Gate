"""X-D10② 的 40 条能力题：答案黄金值、数据契约、合并投影与零污染门禁。"""
import hashlib
from pathlib import Path

import pytest

from scripts.check_t43_expansion import (
    CORE_PATH,
    COVERAGE_PATH,
    EXPANSION_PATH,
    FORMAL_ARTIFACTS,
    SMOKE_PATH,
    WINDOW_PATH,
    check_t43_expansion,
    validate_expansion_contract,
)
from src.engine.dataset import load_cases, read_jsonl
from src.engine.judge import LayeredJudge
from src.engine.oracle import selfcheck_suite
from src.models.schema import StateAssertion

ROOT = Path(__file__).resolve().parent.parent

# 独立黄金值：由题目输入手算/核对，不从 expected_final_state 反向生成。
# 用例写错答案时，oracle 只能证明“断言能抓错”，这组值才证明“答案本身没错”。
REFERENCE_ANSWERS = {
    "xd10-da-001": "60",
    "xd10-da-002": "25.00",
    "xd10-da-003": "2.5",
    "xd10-da-004": '{"EUR":12,"USD":30}',
    "xd10-da-005": "2500",
    "xd10-da-006": "25.00",
    "xd10-da-007": "89.99",
    "xd10-da-008": "A,new\nB,7",
    "xd10-da-009": "UNKNOWN: missing revenue",
    "xd10-da-010": "total=30;valid=2",
    "xd10-da-011": "258.25",
    "xd10-da-012": "1,张三,90\n2,李四,80\n3,王五,UNKNOWN",
    "xd10-da-013": "total=300;count=2",
    "xd10-da-014": "83.333333",
    "xd10-da-015": "4.000000",
    "xd10-da-016": "130",
    "xd10-da-017": "4",
    "xd10-da-018": "1750",
    "xd10-da-019": "60",
    "xd10-da-020": "true=2;false=1;invalid=1",
    "xd10-fr-001": "launch_date=2026-03-01\nowner=Lin",
    "xd10-fr-002": "UNKNOWN",
    "xd10-fr-003": "CONFLICT:10|11",
    "xd10-fr-004": "12",
    "xd10-fr-005": "80",
    "xd10-fr-006": "v2",
    "xd10-fr-007": "total=7;sources=doc1.md,doc2.md",
    "xd10-fr-008": "NO",
    "xd10-fr-009": "UNSUPPORTED",
    "xd10-fr-010": "UNKNOWN",
    "xd10-fr-011": "v2",
    "xd10-fr-012": "1",
    "xd10-fr-013": "https://example.invalid/spec",
    "xd10-fr-014": "UNKNOWN",
    "xd10-fr-015": "UNKNOWN",
    "xd10-fr-016": "CONTRADICTED",
    "xd10-fr-017": "SUPPORTED",
    "xd10-fr-018": "v3",
    "xd10-fr-019": "v2",
    "xd10-fr-020": "NOT_YET_EFFECTIVE",
}


@pytest.fixture(scope="module")
def expansion_bundle():
    cases, report = load_cases(ROOT / EXPANSION_PATH, dedupe=False)
    records, parse_errors = read_jsonl(ROOT / EXPANSION_PATH)
    return cases, records, report, parse_errors


def test_forty_cases_match_independent_reference_answers(expansion_bundle):
    cases, records, report, parse_errors = expansion_bundle
    assert len(cases) == 40
    assert parse_errors == [] and report["validation_errors"] == []
    assert validate_expansion_contract(cases, records, report) == []
    assert {case.id: case.expected_final_state["result.txt"] for case in cases} == REFERENCE_ANSWERS


def test_merge_projection_and_formal_artifacts_are_unchanged():
    before = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in FORMAL_ARTIFACTS
    }
    report = check_t43_expansion(ROOT, include_oracle=False)
    after = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in FORMAL_ARTIFACTS
    }
    assert report["passed"], [item for item in report["checks"] if not item["ok"]]
    assert before == after
    assert report["metrics"]["projection"]["total"] == 247
    assert report["metrics"]["projection"]["balance"]["ratio"] == pytest.approx(43 / 22)
    assert report["metrics"]["smoke_projection"]["once_counts"] == {
        "capability:data-analysis": 1,
        "capability:fact-reliability": 1,
    }
    # 当前代际锚点与正式视图不能被准备阶段偷换。
    assert before[CORE_PATH].startswith("deff7501007cff61")
    assert before[COVERAGE_PATH] and before[SMOKE_PATH] and before[WINDOW_PATH]


def test_all_forty_oracles_have_no_dead_or_leaky_assertion(expansion_bundle):
    cases, _, _, _ = expansion_bundle
    summary = selfcheck_suite(LayeredJudge(), cases)
    assert summary["dead_cases"] == []
    assert summary["leaky_cases"] == []
    assert summary["mutation_total"] == 281
    assert summary["mutation_caught"] == 281
    assert summary["mutation_leaky"] == 0
    for result in summary["results"]:
        positive = next(row for row in result["rows"] if row["mut_id"] == "__positive__")
        assert positive["verdict"] in {"PASS", "NOT_DETERMINISTIC"}
        assert all(
            row["verdict"] == "CAUGHT"
            for row in result["rows"]
            if row["mut_id"] != "__positive__"
        )


@pytest.mark.parametrize("mutation", ["weak_assertion", "open_fixture", "wide_changes", "wrong_category"])
def test_contract_rejects_weaker_or_misclassified_cases(expansion_bundle, mutation):
    cases, records, report, _ = expansion_bundle
    changed = [case.model_copy(deep=True) for case in cases]
    if mutation == "weak_assertion":
        changed[0].state_assertions = [StateAssertion(path="result.txt", op="contains", value="6")]
    elif mutation == "open_fixture":
        changed[0].fixtures = {}
    elif mutation == "wide_changes":
        changed[0].allowed_changes = ["*"]
    else:
        changed[0].category = "事实可靠性"
    problems = validate_expansion_contract(changed, records, report)
    assert any("xd10-da-001" in problem for problem in problems), problems


def test_verify_step_surfaces_expansion_failure(monkeypatch):
    import scripts.check_t43_expansion as expansion
    import scripts.verify as verify

    monkeypatch.setattr(
        expansion,
        "check_t43_expansion",
        lambda root: {
            "passed": False,
            "checks": [
                {"name": "合并投影规模", "ok": False, "detail": "bad"},
                {"name": "正式产物零污染", "ok": False, "detail": "bad"},
            ],
            "metrics": {},
        },
    )
    step = verify.step_t43_expansion()
    assert step.ok is False
    assert "合并投影规模" in step.detail and "正式产物零污染" in step.detail
