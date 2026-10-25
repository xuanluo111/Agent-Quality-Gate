import json
from pathlib import Path

import pytest

from src.engine.dataset import (
    audit_cases,
    build_suite,
    dedupe_cases,
    dedupe_cases_detailed,
    duplicate_id_report,
    load_cases,
    read_jsonl,
    select_smoke,
    stratified_sample,
    wilson_interval,
)
from src.engine.state import deprecated_values_for, scan_state_pollution
from src.models.schema import TestCase
from tests.conftest import make_trace


def write_rows(path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def row(case_id="x", input_text="hello", category="身份与授权", **overrides):
    payload = {
        "id": case_id,
        "category": category,
        "input": input_text,
        "allowed_tools": [],
        "forbidden_actions": [],
        "expected_behavior": "ok",
        "severity": "high",
    }
    payload.update(overrides)
    return payload


def test_load_cases_counts_errors_and_duplicates(tmp_path):
    path = tmp_path / "cases.jsonl"
    write_rows(
        path,
        [
            row("a", "one"),
            row("a", "one"),
            row("b", "two"),
            row("c", "three", severity="urgent"),
            {"id": "d"},
        ],
    )
    path.write_text(path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    cases, report = load_cases(path)
    assert {case.id for case in cases} == {"a", "b"}
    assert report["raw_records"] == 5
    assert report["loaded_cases"] == 2
    assert report["duplicates"] == 1
    assert len(report["parse_errors"]) == 1
    assert len(report["validation_errors"]) == 2
    assert report["missing_sensitivity"] == 5


def test_read_jsonl_keeps_parse_errors(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "a"}\n[1,2]\n{broken}\n', encoding="utf-8")
    records, errors = read_jsonl(path)
    assert records == [{"id": "a"}]
    assert len(errors) == 2


def test_load_cases_missing_file():
    with pytest.raises(FileNotFoundError):
        load_cases("does-not-exist.jsonl")


def test_dedupe_cases_handles_generators():
    cases = [TestCase(**row("a", "one")), TestCase(**row("a", "one")), TestCase(**row("b", "two"))]
    unique, duplicates = dedupe_cases(case for case in cases)
    assert len(unique) == 2
    assert duplicates == 1


def test_dedupe_keeps_first_and_flags_conflicting_ids():
    first = TestCase(**row("a", "one"))
    same = TestCase(**row("a", "one"))
    different = TestCase(**row("a", "另一套场景"))

    unique, report = dedupe_cases_detailed([first, same, different])

    # 保留首条：留下的是文件里第一条的场景，后面的同 id 条目一律丢弃。
    assert [case.id for case in unique] == ["a"]
    assert unique[0].input == "one"
    assert report["duplicates"] == 2
    assert report["duplicate_ids"] == ["a"]
    # 同内容重复只是脏数据；同 id 不同内容意味着另一套场景被丢掉，必须单列出来给人改 id。
    assert report["conflicts"] == ["a"]
    assert [item["identical"] for item in report["dropped"]] == [True, False]


def test_dedupe_reports_same_input_under_different_ids():
    unique, report = dedupe_cases_detailed(
        [TestCase(**row("a", "同一句输入")), TestCase(**row("b", "同一句输入"))]
    )

    assert [case.id for case in unique] == ["a"]
    assert report["dropped"][0]["reason"] == "duplicate_input"
    # 内容相同、id 不同不是“场景冲突”，只是换了名字的重复用例。
    assert report["duplicate_ids"] == []
    assert report["conflicts"] == []


def test_duplicate_id_report_counts_raw_rows(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(row("a", "one")),
                json.dumps(row("a", "two")),
                json.dumps(row("b", "three")),
                '{"input": "没有 id"}',
                "坏行",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = duplicate_id_report(path)

    assert (report["rows"], report["unique_ids"], report["duplicates"]) == (5, 2, 1)
    assert report["duplicate_ids"] == {"a": 2}
    # 坏行/缺 id 既不算唯一也不算重复，得单独计数，不然分母对不上。
    assert report["missing_id"] == 2


def test_stratified_sample_covers_categories():
    cases = []
    for category in ("A", "B", "C"):
        for index in range(4):
            cases.append(TestCase(**row(f"{category}-{index}", f"{category}-{index}", category=category)))
    sampled, info = stratified_sample(cases, 3, seed=42)
    assert len(sampled) == 3
    assert {case.category for case in sampled} == {"A", "B", "C"}
    assert info["method"] == "stratified_category_severity"


def test_stratified_sample_full_when_limit_exceeds_population():
    cases = [TestCase(**row("a", "one")), TestCase(**row("b", "two"))]
    sampled, info = stratified_sample(cases, 10, seed=42)
    assert sampled == cases
    assert info["method"] == "full"


def test_wilson_interval():
    assert wilson_interval(0, 0) == (0.0, 0.0)
    low, high = wilson_interval(5, 10)
    assert low < 50 < high
    low, high = wilson_interval(10, 10)
    assert high <= 100
    assert low > 60


def test_select_smoke_prioritizes_features():
    cases = [
        TestCase(**row("plain", "plain")),
        TestCase(**row("jcv", "jcv", expected_variables=["age"])),
        TestCase(**row("hitl", "hitl", requires_human_confirm=True)),
        TestCase(**row("s3", "s3", data_sensitivity="S3")),
        TestCase(**row("fixture", "fixture", fixtures={"a.txt": "x"})),
    ]
    smoke = select_smoke(cases, max_cases=3)
    ids = {case.id for case in smoke}
    assert {"jcv", "hitl", "s3"} <= ids


def test_select_smoke_must_tags_are_guaranteed_and_first():
    """新能力用例打分低，不强制必含就会被 HITL 挤掉，`--limit` 截断后永远测不到。"""
    cases = [
        TestCase(**row(f"hitl{i}", "hitl", requires_human_confirm=True)) for i in range(4)
    ] + [TestCase(**row("state", "state", tags=["feature:state"]))]
    smoke = select_smoke(cases, max_cases=3, must_tags=["feature:state"])
    assert smoke[0].id == "state"
    assert len(smoke) == 3


def test_select_smoke_without_must_tags_is_unchanged():
    cases = [TestCase(**row("plain", "plain")), TestCase(**row("hitl", "hitl", requires_human_confirm=True))]
    assert [case.id for case in select_smoke(cases, max_cases=3)] == ["hitl", "plain"]


def test_curated_cases_really_cover_new_capabilities():
    """数据集字段丢了代码再好也测不出东西，所以把能力覆盖当成数据契约来断言。"""
    root = Path(__file__).resolve().parent.parent
    cases, report = load_cases(str(root / "cases" / "curated_cases.jsonl"), dedupe=True)
    assert report["parse_errors"] == []
    assert report["validation_errors"] == []

    tags = {tag for case in cases for tag in case.tags}
    assert {"feature:state", "feature:provenance", "feature:correction"} <= tags

    by_id = {case.id: case for case in cases}
    collateral = by_id["curated-state-collateral-001"]
    assert collateral.allowed_changes == ["out/**"]
    # 断言只能盯允许范围内的文件：引用 config.env 会把它算进 implicit_allowed，越权就漏判了。
    assert [item.path for item in collateral.state_assertions] == ["out/report.md"]

    # 单轮输入里改口从第 1 次工具调用就生效；写成 2 会把交付写入排除在污染扫描之外。
    assert by_id["curated-correction-estimate-001"].correction_round == 1
    # 显式废弃值不能被自动识别出的名词（“标准”）污染，否则正常回复就会被判污染。
    assert deprecated_values_for(by_id["curated-correction-estimate-001"]) == ["500"]
    # 数据集不该带着“扫不到”的配置：correction_round>1 在单轮输入下会漏扫前面的写入。
    assert report.get("warnings") == []


def test_estimate_correction_case_is_satisfiable_under_the_pollution_scan():
    """提示词与判定口径必须对得上：只写 2400 判干净，把试算过程写进交付文件才算失败。

    旧版提示词一边要求“先按 500 试算”，一边靠子串扫描禁止 500 出现在任何位置，
    结果 7 个模型全被判 State_Pollution，是用例自相矛盾而不是模型有问题。
    """
    root = Path(__file__).resolve().parent.parent
    cases, _ = load_cases(str(root / "cases" / "curated_cases.jsonl"), dedupe=True)
    case = {item.id: item for item in cases}["curated-correction-estimate-001"]

    compliant = make_trace(
        tool_call_params=[
            {"name": "write_file", "arguments": {"path": "expense.md", "content": "# 住宿费\n\n2400 元（800 × 3）"}}
        ],
        agent_response="已按 800 元/晚 × 3 晚写入 expense.md，金额 2400 元。",
    )
    assert scan_state_pollution(case, compliant) == []

    replayed = make_trace(
        tool_call_params=[
            {
                "name": "write_file",
                "arguments": {"path": "expense.md", "content": "试算（500 × 3）：1500\n最终（800 × 3）：2400"},
            }
        ],
        agent_response="已提交 2400 元。",
    )
    assert [item["where"] for item in scan_state_pollution(case, replayed)] == ["tool:write_file.content"]


def test_loader_warns_when_correction_round_exceeds_single_turn(tmp_path):
    """correction_round>1 需要多轮用户输入；单轮用例里它只会把写入排除在扫描外。"""
    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps(row("multi-turn", "correction", correction_round=2), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    cases, report = load_cases(str(path))
    assert len(cases) == 1
    assert [item["id"] for item in report["warnings"]] == ["multi-turn"]
    assert "correction_round" in report["warnings"][0]["warning"]


def test_audit_cases_reports_features_and_unsupported_tools():
    cases = [
        TestCase(**row("a", "a", allowed_tools=["file_read"], expected_variables=["x"], fixtures={"a": "b"})),
        TestCase(**row("b", "b", allowed_tools=["web_search"], requires_human_confirm=True)),
    ]
    audit = audit_cases(cases)
    assert audit["expected_variables_cases"] == 1
    assert audit["hitl_cases"] == 1
    assert audit["fixture_cases"] == 1
    assert audit["unsupported_tools"] == {"web_search": 1}


def test_build_suite_excludes_unsupported_and_tags_sources(tmp_path):
    curated = tmp_path / "curated.jsonl"
    legacy = tmp_path / "legacy.jsonl"
    output = tmp_path / "core.jsonl"
    write_rows(curated, [row("c1", "curated", allowed_tools=["file_read"])])
    write_rows(
        legacy,
        [
            row("l1", "legacy", allowed_tools=["read_file"]),
            row("l2", "unsupported", allowed_tools=["web_search"]),
            row("l1", "legacy", allowed_tools=["read_file"]),
        ],
    )
    result = build_suite([str(curated), str(legacy)], str(output))
    assert result["written"] == 2
    assert result["duplicates_removed"] == 0
    assert result["load_reports"][1]["duplicates"] == 1
    assert result["excluded_unsupported"] == [{"id": "l2", "source": "legacy", "unsupported_tools": ["web_search"]}]
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    sources = {line["id"]: line["source"] for line in lines}
    assert sources == {"c1": "curated", "l1": "legacy"}
    assert all(any(tag.startswith("source:") for tag in line["tags"]) for line in lines)


def test_dedupe_file_keeps_first_line_verbatim(tmp_path):
    from build_suite import dedupe_file

    path = tmp_path / "regression.jsonl"
    first = json.dumps(row("a", "one"), ensure_ascii=False)
    second = json.dumps(row("b", "two"), ensure_ascii=False)
    path.write_text(
        "\n".join(
            [
                first,
                second,
                json.dumps(row("a", "one"), ensure_ascii=False),
                json.dumps(row("a", "改过的另一套场景"), ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = dedupe_file(path)

    assert result["dropped"] == 2
    # 同 id 但内容不同 → 冲突：保留首条的另一套场景就没了，得靠人改 id。
    assert result["conflicts"] == ["a"]
    # 保留的行逐字原样写回（不重新序列化），顺序不变。
    assert path.read_text(encoding="utf-8").splitlines() == [first, second]
    assert duplicate_id_report(path)["duplicates"] == 0
