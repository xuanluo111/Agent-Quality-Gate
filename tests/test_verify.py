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
