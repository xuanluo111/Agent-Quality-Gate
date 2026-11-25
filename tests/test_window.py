"""T32 跨窗口协议（R-L5）的判据测试。

这个模块要钉住的不是"算得对不对"这么简单一件事，而是三条协议约束各自都**可被违反**：
- 未来窗口的题被喂进来了 → 必须拦下，而不是悄悄少跑；
- manifest 把同一题在两个窗口各声明一次 → 必须拒，否则"重排旧题"就能冒充新增增益；
- ΔJ 把覆盖扩张混进能力增益 → 必须拆开报，并留下"只有 retained 可归因"的字段。

断言写法上刻意用「先构造违规、再断言被拒」，而不是只断言正常路径通过：
只测正常路径的协议测试，等价于把纪律写在注释里。
"""

import json

import pytest

from src.engine.window import (
    DEFAULT_METRIC,
    PROTOCOL_ID,
    WindowProtocolError,
    blocked_ids,
    build_window_view,
    delta_j,
    increment_ids,
    select_window_cases,
    validate_manifest,
    visible_ids,
)

CORPUS = ["a1", "a2", "a3", "b1", "b2"]


def _manifest(*windows, **extra):
    payload = {
        "protocol": PROTOCOL_ID,
        "corpus": "cases/core_suite.jsonl",
        "metric": DEFAULT_METRIC,
        "verified": True,
        "windows": [
            {"window": number, "cutoff": cutoff, "declared": list(ids)}
            for number, cutoff, ids in windows
        ],
    }
    payload.update(extra)
    return payload


def _two_window_manifest(**extra):
    return _manifest(
        (0, "2026-10-22", ["a1", "a2", "a3"]), (1, "2026-11-06", ["b1", "b2"]), **extra
    )


class _Case:
    """只带 id 的桩：select_window_cases 只认 id，不该依赖完整 TestCase。"""

    def __init__(self, case_id):
        self.id = case_id


# --------------------------------------------------------------------------
# 时间切片与可见集
# --------------------------------------------------------------------------


def test_visible_ids_are_cumulative_and_exclude_future():
    manifest = _two_window_manifest()
    assert visible_ids(manifest, 0) == ["a1", "a2", "a3"]
    assert visible_ids(manifest, 1) == ["a1", "a2", "a3", "b1", "b2"]


def test_increment_ids_are_only_this_window():
    manifest = _two_window_manifest()
    assert increment_ids(manifest, 1) == ["b1", "b2"]


def test_blocked_ids_reports_future_leak_not_silently_drops():
    """未来题必须被点名。静默丢弃会让"本轮喂了多少题"失真，也掩盖污染已发生。"""
    manifest = _two_window_manifest()
    assert blocked_ids(manifest, 0, ["a1", "b1", "ghost"]) == ["b1", "ghost"]


def test_select_blocks_future_cases_when_enforced():
    manifest = _two_window_manifest()
    with pytest.raises(WindowProtocolError, match="未来"):
        select_window_cases([_Case("a1"), _Case("b1")], manifest, 0)


def test_select_keeps_only_visible_when_enforced():
    manifest = _two_window_manifest()
    selected, audit = select_window_cases([_Case("a1"), _Case("b1")], manifest, 1)
    assert [case.id for case in selected] == ["a1", "b1"]
    assert audit["dropped"] == 0


def test_non_enforced_select_still_counts_dropped():
    """plan 场景允许裁剪，但审计里必须看得见丢了几条——不能裁完就当什么都没发生。"""
    manifest = _two_window_manifest()
    selected, audit = select_window_cases([_Case("a1"), _Case("b1")], manifest, 0, enforce=False)
    assert [case.id for case in selected] == ["a1"]
    assert audit["dropped"] == 1
    assert audit["dropped_ids"] == ["b1"]


def test_accepts_dict_rows_as_well_as_objects():
    """历史语料读取口是 dict，runner 侧是 TestCase；两种都要能裁，否则脚本层用不了。"""
    manifest = _two_window_manifest()
    selected, _ = select_window_cases([{"id": "a1"}, {"id": "b1"}], manifest, 0, enforce=False)
    assert len(selected) == 1


# --------------------------------------------------------------------------
# manifest 硬校验
# --------------------------------------------------------------------------


def test_valid_manifest_passes_and_counts_coverage():
    audit = validate_manifest(_two_window_manifest(), CORPUS)
    assert audit["windows"] == 2
    assert audit["declared_ids"] == 5
    assert audit["covered_ids"] == 5


def test_ids_outside_corpus_are_never_endorsed():
    """引用语料外的题就等于给不属于本代际的文件背书（与 T49 同一口径）。"""
    manifest = _manifest((0, "2026-10-22", ["a1", "ghost"]))
    with pytest.raises(WindowProtocolError, match="语料外"):
        validate_manifest(manifest, CORPUS)


def test_same_case_in_two_windows_is_rejected():
    """同一题重复声明会把"重排旧题"算成新增增益——这是协议最容易被绕过的地方。"""
    manifest = _manifest((0, "2026-10-22", ["a1", "a2"]), (1, "2026-11-06", ["a2", "b1"]))
    with pytest.raises(WindowProtocolError, match="冒充新增"):
        validate_manifest(manifest, CORPUS)


def test_window_numbers_must_be_contiguous_from_zero():
    manifest = _manifest((0, "2026-10-22", ["a1"]), (2, "2026-11-06", ["b1"]))
    with pytest.raises(WindowProtocolError, match="连续"):
        validate_manifest(manifest, CORPUS)


def test_cutoff_is_mandatory_and_monotonic():
    """缺 cutoff 就不是时间切片；倒着走也不是。两者都失败关闭。"""
    missing = _manifest((0, "", ["a1"]))
    with pytest.raises(WindowProtocolError, match="cutoff"):
        validate_manifest(missing, CORPUS)
    backwards = _manifest((0, "2026-11-06", ["a1"]), (1, "2026-10-22", ["b1"]))
    with pytest.raises(WindowProtocolError, match="单调"):
        validate_manifest(backwards, CORPUS)


def test_empty_increment_is_rejected():
    manifest = _manifest((0, "2026-10-22", ["a1"]), (1, "2026-11-06", []))
    with pytest.raises(WindowProtocolError, match="为空"):
        validate_manifest(manifest, CORPUS)


def test_unknown_protocol_version_fails_closed():
    manifest = _two_window_manifest()
    manifest["protocol"] = "aqg-cross-window/99"
    with pytest.raises(WindowProtocolError, match="协议版本"):
        validate_manifest(manifest, CORPUS)


def test_empty_corpus_cannot_validate_anything():
    with pytest.raises(WindowProtocolError, match="语料 id 集合为空"):
        validate_manifest(_two_window_manifest(), [])


def test_load_manifest_rejects_missing_and_broken_files(tmp_path):
    with pytest.raises(WindowProtocolError, match="不存在"):
        from src.engine.window import load_manifest

        load_manifest(tmp_path / "nope.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    from src.engine.window import load_manifest

    with pytest.raises(WindowProtocolError, match="合法 JSON"):
        load_manifest(broken)


# --------------------------------------------------------------------------
# ΔJ 口径
# --------------------------------------------------------------------------


def test_delta_j_separates_retained_from_novel():
    """跨窗口的分数变化里混着两种东西：同题的真变化，和题目覆盖扩张。必须拆开。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True, "a2": True, "a3": False}
    current = {"a1": True, "a2": False, "a3": True, "b1": False, "b2": False}
    result = delta_j(baseline, current, manifest, 1)
    # retained 三条里：a1 持平、a2 由过变不过、a3 由不过变过 → 净 0，但同题分数各自变了。
    assert result["n_retained"] == 3
    assert result["n_novel"] == 2
    assert result["delta_retained"] == pytest.approx(0.0)
    assert result["j_retained_baseline"] == pytest.approx(200 / 3)
    assert result["j_retained_current"] == pytest.approx(200 / 3)
    assert result["delta_novel"] == pytest.approx(0.0)


def test_delta_total_is_flagged_as_non_attributable():
    """全量口径的分母在两期之间变了，所以它永远是"构成变化 + 真变化"的混合体。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True, "a2": True, "a3": True}
    current = {"a1": True, "a2": True, "a3": True, "b1": False, "b2": False}
    result = delta_j(baseline, current, manifest, 1)
    # 全量口径：当期 3/5 = 60pp，基线 3/3 = 100pp → -40pp；而同题口径一分没掉。
    # 这 40pp 全是"多喂了两道新题"带来的构成变化，正是 delta_total 不许当增益的原因。
    assert result["delta_total"] == pytest.approx(-40.0)
    assert result["delta_retained"] == pytest.approx(0.0)
    assert result["attributable_field"] == "delta_retained"
    assert result["attributable"] is True
    assert "构成变化" in result["note_composition"]


def test_window_zero_is_baseline_and_produces_no_gain():
    """窗口 0 没有上一期，任何"增益"都是无中生有。"""
    manifest = _manifest((0, "2026-10-22", ["a1", "a2"]))
    result = delta_j({"a1": True, "a2": False}, {"a1": True, "a2": False}, manifest, 0)
    assert result["delta_retained"] is None
    assert result["attributable"] is False
    assert "基线轮" in result["note"]


def test_missing_baseline_case_is_counted_not_invented():
    """基线里没跑过的题不能凭"当期通过"就算增益，得单独计数让人看见缺口。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True}  # a2/a3 基线缺失
    current = {"a1": True, "a2": True, "a3": True, "b1": True, "b2": True}
    result = delta_j(baseline, current, manifest, 1)
    assert result["n_retained"] == 1
    assert result["n_missing_baseline"] == 2


def test_precondition_failed_bits_leave_the_denominator():
    """None 位是环境故障，不是模型失败（AGENTS.md 纪律 3）：剔出分母而不是算成失败。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True, "a2": None, "a3": None}
    current = {"a1": True, "a2": None, "a3": None, "b1": None, "b2": None}
    result = delta_j(baseline, current, manifest, 1)
    assert result["j_retained_baseline"] == pytest.approx(100.0)
    assert result["delta_retained"] == pytest.approx(0.0)


def _observation(bit, **extra):
    value = {
        "strict_passed": bit,
        "failure_class": None,
        "data_valid": True,
        "judge_layer": "rule",
        "is_fallback": False,
        "precondition_failed": None,
        "metadata_missing": [],
    }
    value.update(extra)
    return value


def test_environment_false_stays_in_primary_but_leaves_attribution_denominator():
    """T50 的核心反例：False 环境故障不是 None，必须靠分类元数据剔除。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True, "a2": False, "a3": True}
    current = {"a1": False, "a2": False, "a3": True, "b1": False}
    baseline_obs = {
        "a1": _observation(True),
        "a2": _observation(False, failure_class="precondition_failed"),
        "a3": _observation(True),
    }
    current_obs = {
        "a1": _observation(False),
        "a2": _observation(False, failure_class="precondition_failed"),
        "a3": _observation(True),
        "b1": _observation(False, failure_class="unknown"),
    }
    result = delta_j(
        baseline, current, manifest, 1,
        baseline_observations=baseline_obs, current_observations=current_obs,
    )

    assert result["j_baseline"] == pytest.approx(200 / 3)
    assert result["j_current"] == pytest.approx(25.0)
    assert result["delta_retained"] == pytest.approx(-100 / 3)
    assert result["j_retained_baseline_valid_only"] == pytest.approx(100.0)
    assert result["j_retained_current_valid_only"] == pytest.approx(50.0)
    assert result["delta_retained_valid_only"] == pytest.approx(-50.0)
    assert result["delta_total_valid_only"] == pytest.approx(-50.0)
    assert result["delta_novel_valid_only"] is None
    profile = result["denominator_profiles"]["environment_excluded"]
    assert profile["n_retained_baseline_scored"] == 2
    assert profile["n_retained_current_scored"] == 2
    assert profile["n_retained_paired_scored"] == 2
    assert profile["exclusions"]["baseline"]["excluded_case_ids"] == {
        "precondition_failed": ["a2"],
    }
    assert profile["exclusions"]["current"]["excluded_by_reason"] == {
        "precondition_failed": 1,
        "unknown": 1,
    }


def test_attribution_excludes_unknown_invalid_harness_and_fallback():
    """未知、无效数据、harness 与 fallback 都不能用于解释模型能力。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True, "a2": True, "a3": True}
    current = {"a1": True, "a2": False, "a3": False, "b1": False, "b2": False}
    baseline_obs = {
        "a1": _observation(True),
        "a2": _observation(True, failure_class="unknown"),
        "a3": _observation(True, is_fallback=True),
    }
    current_obs = {
        "a1": _observation(True),
        "a2": _observation(False, failure_class="unknown"),
        "a3": _observation(False, is_fallback=True),
        "b1": _observation(False, data_valid=False),
        "b2": _observation(False, judge_layer="harness"),
    }
    result = delta_j(
        baseline, current, manifest, 1,
        baseline_observations=baseline_obs, current_observations=current_obs,
    )
    profile = result["denominator_profiles"]["environment_excluded"]
    assert result["delta_retained"] == pytest.approx(-200 / 3)
    assert result["delta_retained_valid_only"] == pytest.approx(0.0)
    assert profile["n_retained_paired_scored"] == 1
    assert profile["exclusions"]["baseline"]["excluded_by_reason"] == {
        "fallback": 1,
        "unknown": 1,
    }
    assert profile["exclusions"]["current"]["excluded_by_reason"] == {
        "fallback": 1,
        "harness": 1,
        "invalid_data": 1,
        "unknown": 1,
    }


def test_trace_precondition_does_not_exclude_a_recovered_pass():
    """探路失败后模型最终通过，整题仍测到了能力；不能见 trace 标记就剔题。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True}
    current = {"a1": True, "b1": True}
    observation = _observation(True, precondition_failed=True)
    result = delta_j(
        baseline, current, manifest, 1,
        baseline_observations={"a1": observation},
        current_observations={"a1": observation, "b1": _observation(True)},
    )
    profile = result["denominator_profiles"]["environment_excluded"]
    assert result["delta_retained_valid_only"] == pytest.approx(0.0)
    assert profile["exclusions"]["baseline"]["excluded_count"] == 0
    assert profile["exclusions"]["current"]["excluded_count"] == 0


def test_mixed_repeat_trace_does_not_hide_explicit_final_model_failure():
    """早期环境失败不能覆盖正式最终分类；显式 None 的模型失败必须留在归因分母。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True}
    current = {"a1": False, "b1": True}
    result = delta_j(
        baseline, current, manifest, 1,
        baseline_observations={"a1": _observation(True)},
        current_observations={
            "a1": _observation(False, precondition_failed=True),
            "b1": _observation(True),
        },
    )
    profile = result["denominator_profiles"]["environment_excluded"]
    assert result["delta_retained_valid_only"] == pytest.approx(-100.0)
    assert profile["n_retained_current_scored"] == 1
    assert profile["n_retained_paired_scored"] == 1
    assert profile["exclusions"]["current"]["excluded_count"] == 0


def test_trace_precondition_is_legacy_fallback_when_failure_class_missing():
    """历史行缺正式分类时仍可用 trace 剔环境失败，但不能把缺键说成完整取证。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True}
    current = {"a1": False, "b1": True}
    result = delta_j(
        baseline, current, manifest, 1,
        baseline_observations={"a1": _observation(True)},
        current_observations={
            "a1": _observation(
                False, precondition_failed=True, metadata_missing=["failure_class"],
            ),
            "b1": _observation(True),
        },
    )
    profile = result["denominator_profiles"]["environment_excluded"]
    assert result["delta_retained_valid_only"] is None
    assert result["attribution_valid_only"] is False
    assert profile["exclusions"]["current"]["excluded_by_reason"] == {
        "trace_precondition_failed": 1,
    }
    assert "缺环境分类元数据" in result["note_metadata"]


def test_valid_retained_reports_separate_denominators_and_paired_count():
    """两期环境分布不同时各自报分母，并另报共同配对数，不能把 n 藏起来。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True, "a2": False}
    current = {"a1": True, "a2": False, "b1": True}
    result = delta_j(
        baseline, current, manifest, 1,
        baseline_observations={
            "a1": _observation(True),
            "a2": _observation(False, failure_class="precondition_failed"),
        },
        current_observations={
            "a1": _observation(True), "a2": _observation(False), "b1": _observation(True),
        },
    )
    profile = result["denominator_profiles"]["environment_excluded"]
    assert profile["n_retained_baseline_scored"] == 1
    assert profile["n_retained_current_scored"] == 2
    assert profile["n_retained_paired_scored"] == 1
    assert result["delta_retained_valid_only"] == pytest.approx(-50.0)


def test_legacy_delta_call_keeps_profiles_equal_and_flags_missing_metadata():
    """旧调用不传 observation 仍可运行，但产物必须承认没做环境类剔除。"""
    manifest = _two_window_manifest()
    baseline = {"a1": True, "a2": False}
    current = {"a1": True, "a2": False, "b1": True}
    result = delta_j(baseline, current, manifest, 1)
    profile = result["denominator_profiles"]["environment_excluded"]
    assert result["delta_retained_valid_only"] == result["delta_retained"]
    assert profile["exclusions"]["baseline"]["metadata_available"] is False
    assert "缺环境分类元数据" in result["note_metadata"]


def test_unknown_metric_is_rejected():
    manifest = _two_window_manifest(metric="f1_score")
    with pytest.raises(WindowProtocolError, match="未知的 J 口径"):
        delta_j({"a1": True}, {"a1": True, "b1": True}, manifest, 1)


def test_window_without_predecessor_has_no_baseline():
    """跳号 manifest 已经在校验层被拒；这里保护直接调 delta_j 的调用方。"""
    manifest = _manifest((0, "2026-10-22", ["a1"]), (1, "2026-11-06", ["b1"]))
    with pytest.raises(WindowProtocolError, match="不在 manifest"):
        delta_j({"a1": True}, {"a1": True, "b1": True}, manifest, 7)


def test_metric_defaults_to_strict_when_absent():
    """不声明口径时落到 Strict——与发布门禁同源，不允许报告里出现第二个 J。"""
    manifest = _two_window_manifest()
    manifest.pop("metric")
    result = delta_j({"a1": True}, {"a1": True, "b1": True}, manifest, 1)
    assert result["metric"] == DEFAULT_METRIC


def test_manifest_round_trips_through_json(tmp_path):
    """脚本层读写的是 JSON 文件，序列化不能把协议字段弄丢。"""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_two_window_manifest(), ensure_ascii=False), encoding="utf-8")
    from src.engine.window import load_manifest

    manifest = load_manifest(path)
    assert validate_manifest(manifest, CORPUS)["covered_ids"] == 5


# --------------------------------------------------------------------------
# 视图生成：`--window` 能吃到题集的唯一入口
# --------------------------------------------------------------------------


def _corpus_file(tmp_path, lines):
    path = tmp_path / "core.jsonl"
    path.write_text(
        "".join(line if line.endswith("\n") else line + "\n" for line in lines),
        encoding="utf-8",
    )
    return path


# 三个题分别用非规范 JSON 写：双空格、紧凑无空格、带空格分隔的冒号。
# 只要实现肯回写原文，这三种形状都必须原样活着。
_ODD_LINES = [
    '{"input": "a1 原题",  "id": "a1"}',
    '{"id":"a2","input":"中文 / 斜杠"}',
    '{ "id" : "c1", "input": "未来题" }',
]


def _view_manifest():
    return _manifest((0, "2026-10-22", ["a1", "a2"]), (1, "2026-11-06", ["c1"]))


def test_view_lines_are_byte_identical_not_reserialized(tmp_path):
    """视图必须搬原文，不能“重写成规整 JSON”。

    题面一改写，被测方看到的就不是存档里那道题，而 ΔJ 比的正是同题复测；
    键序/空白/转义都属于题面的一部分，不是可归一化的装饰。
    """
    path = _corpus_file(tmp_path, _ODD_LINES)
    lines, audit = build_window_view(path, _view_manifest(), 0)

    # 逐字节口径含行终止符：视图就是语料那几行的原文，不重排、不重新序列化。
    assert lines == path.read_text(encoding="utf-8").splitlines(True)[:2]
    # 反证：若实现改成 json.dumps 回写，这两行就不会等于原文。
    assert lines[0] != json.dumps(json.loads(lines[0]), ensure_ascii=False) + "\n"
    assert (audit["emitted"], audit["blocked"]) == (2, 1)


def test_view_emitted_count_equals_visible_count(tmp_path):
    """裁多了和裁少了都要能从审计里看出来：emitted == visible。"""
    path = _corpus_file(tmp_path, _ODD_LINES)
    lines, audit = build_window_view(path, _view_manifest(), 1)
    assert audit["visible"] == 3
    assert audit["emitted"] == audit["visible"] == len(lines)
    assert audit["blocked"] == 0


def test_view_keeps_corpus_line_order(tmp_path):
    """视图不能按 id 重排序：题序也是喂题的一部分，重排会改变上下文窗口。"""
    path = _corpus_file(tmp_path, [_ODD_LINES[1], _ODD_LINES[0], _ODD_LINES[2]])
    lines, _ = build_window_view(path, _view_manifest(), 1)
    assert lines == path.read_text(encoding="utf-8").splitlines(True)


def test_view_rejects_corpus_with_duplicate_ids(tmp_path):
    """同 id 两行内容不同时，“留哪一行”会改变题面，视图不替调用方做这个决定。"""
    path = _corpus_file(tmp_path, ['{"id": "a1", "input": "第一版"}', '{"id": "a1", "input": "第二版"}'])
    with pytest.raises(WindowProtocolError, match="重复 id"):
        build_window_view(path, _manifest((0, "2026-10-22", ["a1"])), 0)


def test_view_rejects_malformed_corpus_line(tmp_path):
    """坏行不能静默跳过：跳过就是“本轮少了一道题”而没人知道。"""
    path = _corpus_file(tmp_path, ['{"id": "a1"}', "not json at all"])
    with pytest.raises(WindowProtocolError, match="第 2 行"):
        build_window_view(path, _manifest((0, "2026-10-22", ["a1"])), 0)


def test_view_rejects_line_without_id(tmp_path):
    path = _corpus_file(tmp_path, ['{"id": "a1"}', '{"input": "没有题号"}'])
    with pytest.raises(WindowProtocolError, match="没有 id"):
        build_window_view(path, _manifest((0, "2026-10-22", ["a1"])), 0)


def test_view_validates_manifest_before_emitting_lines(tmp_path):
    """纵深防御：违规 manifest 不能产视图，否则“忘了 validate”就能生成脏喂题集。"""
    path = _corpus_file(tmp_path, _ODD_LINES)
    overlapping = _manifest((0, "2026-10-22", ["a1", "a2"]), (1, "2026-11-06", ["a2", "c1"]))
    with pytest.raises(WindowProtocolError, match="相交"):
        build_window_view(path, overlapping, 1)


def test_view_missing_corpus_fails_closed(tmp_path):
    with pytest.raises(WindowProtocolError, match="语料不存在"):
        build_window_view(tmp_path / "nope.jsonl", _view_manifest(), 0)
