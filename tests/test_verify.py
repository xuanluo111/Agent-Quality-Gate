"""自检脚本的体检规则单测：规则失效会让 verify 变成“永远通过”，比没有更危险。"""
from scripts.verify import (
    REQUIRED_CAPABILITY_TAGS,
    REQUIRED_SECTIONS,
    REQUIRED_SUMMARY_KEYS,
    missing_capability_tags,
    missing_report_sections,
    missing_summary_fields,
)


def test_missing_summary_fields_lists_only_absent_keys():
    summary = {key: 0 for key in REQUIRED_SUMMARY_KEYS if key != "gate"}
    assert missing_summary_fields(summary) == ["gate"]
    assert missing_summary_fields({key: 0 for key in REQUIRED_SUMMARY_KEYS}) == []


def test_missing_report_sections_detects_chapter_regression():
    """章节标题是 visualize_reports.parse_report 的解析契约，改标题必须能被发现。"""
    full = "\n".join(REQUIRED_SECTIONS)
    assert missing_report_sections(full) == []

    broken = full.replace("## 三、故障事件聚合", "## 三、故障事件")
    assert missing_report_sections(broken) == ["## 三、故障事件聚合"]


def test_missing_capability_tags_requires_all_three():
    assert missing_capability_tags(["feature:state", "feature:provenance"]) == ["feature:correction"]
    assert missing_capability_tags(set(REQUIRED_CAPABILITY_TAGS)) == []


def test_capability_dataset_covers_all_required_tags():
    """真机层跑的就是这份集合：如果筛漏了能力 tag，“验六项能力”就名不副实。"""
    from scripts.verify import _capability_cases

    picked = _capability_cases(0)
    tags = {tag for case in picked for tag in case.tags}
    assert missing_capability_tags(tags) == []
    assert len(picked) >= 3


def test_capability_dataset_respects_limit():
    from scripts.verify import _capability_cases

    assert len(_capability_cases(2)) == 2
    assert _capability_cases(0) == _capability_cases(999)


def test_step_dataset_keeps_manifest_timestamp_when_content_unchanged(tmp_path, monkeypatch):
    """自检只做校验：数据集内容没变时，不能把 manifest 的 generated_at 改掉。

    否则每跑一次 `make verify` 就会多一个只差时间戳的 diff，把工作区弄脏。
    """
    import json
    from types import SimpleNamespace

    import scripts.verify as verify

    cases = tmp_path / "cases"
    cases.mkdir()
    tags = ["feature:state", "feature:provenance", "feature:correction"]
    (cases / "curated_cases.jsonl").write_text('{"id": "a"}\n', encoding="utf-8")
    (cases / "core_suite.jsonl").write_text(
        json.dumps({"id": "a", "tags": tags}) + "\n", encoding="utf-8"
    )
    (cases / "smoke.jsonl").write_text(json.dumps({"id": "a", "tags": tags}) + "\n", encoding="utf-8")
    manifest = cases / "suite_manifest.json"
    manifest.write_text(
        json.dumps({"generated_at": "2026-01-01 00:00:00", "core": {"written": 1}, "smoke": {"written": 1}}),
        encoding="utf-8",
    )
    original = manifest.read_bytes()

    def fake_run(args, timeout=1800):
        # 模拟 build_suite：用例内容不变，只刷新 generated_at
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["generated_at"] = "2026-01-02 00:00:00"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(verify, "_run", fake_run)

    step = verify.step_dataset()

    assert step.ok is True, step.detail
    assert manifest.read_bytes() == original


def test_step_dataset_fails_when_a_suite_keeps_duplicate_ids(tmp_path, monkeypatch):
    """去重没生效时自检必须报错：否则 `--suite regression` 会默默把同一条用例再评一遍。"""
    import json
    from types import SimpleNamespace

    import scripts.verify as verify

    cases = tmp_path / "cases"
    cases.mkdir()
    tags = ["feature:state", "feature:provenance", "feature:correction"]
    duplicate = json.dumps({"id": "a", "tags": tags}) + "\n"
    (cases / "curated_cases.jsonl").write_text('{"id": "a"}\n', encoding="utf-8")
    (cases / "core_suite.jsonl").write_text(duplicate * 2, encoding="utf-8")
    (cases / "smoke.jsonl").write_text(duplicate, encoding="utf-8")
    (cases / "suite_manifest.json").write_text(
        json.dumps({"generated_at": "2026-01-01 00:00:00", "core": {"written": 2}, "smoke": {"written": 1}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(verify, "_run", lambda args, timeout=1800: SimpleNamespace(returncode=0, stdout="", stderr=""))

    step = verify.step_dataset()

    assert step.ok is False
    assert "core_suite.jsonl" in step.detail and "重复 id" in step.detail
