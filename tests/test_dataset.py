import json

import pytest

from src.engine.dataset import (
    audit_cases,
    build_suite,
    dedupe_cases,
    load_cases,
    read_jsonl,
    select_smoke,
    stratified_sample,
    wilson_interval,
)
from src.models.schema import TestCase


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
