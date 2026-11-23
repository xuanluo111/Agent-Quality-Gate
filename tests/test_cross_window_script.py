"""跨窗口 CLI（scripts/cross_window.py）的判据测试。

CLI 层要钉住的是引擎层管不到的两件事：
- **证据来源**：cutoff 只能从落盘批次目录名复算，且必须自称"下界"而不是入库准点；
- **退出码与措辞**：协议违规要非 0，噪声带缺失时只能报数、不许下判定。
"""

import json
from argparse import Namespace
from pathlib import Path

import cross_window
from src.engine.window import PROTOCOL_ID


def _corpus(tmp_path: Path, ids) -> Path:
    path = tmp_path / "core.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"id": case_id, "source": "curated-v1"}, ensure_ascii=False)
            for case_id in ids
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _run_dir(root: Path, stamp: str, bits: dict) -> Path:
    """造一个最小的落盘 run：目录名带批次日期，结果行带逐次 attempts。"""
    run = root / "reports" / "runs" / f"{stamp}-120000-m1"
    run.mkdir(parents=True, exist_ok=True)
    lines = []
    for case_id, attempts in bits.items():
        lines.append(
            json.dumps(
                {
                    "result": {
                        "case_id": case_id,
                        "strict_passed": all(item is True for item in attempts),
                        "attempts": [
                            {"attempt": index + 1, "strict_passed": item}
                            for index, item in enumerate(attempts)
                        ],
                    }
                },
                ensure_ascii=False,
            )
        )
    (run / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run


# --------------------------------------------------------------------------
# 时间证据
# --------------------------------------------------------------------------


def test_day_of_reads_batch_stamp_from_directory_name():
    assert cross_window._day_of("reports/runs/20261106-144519-tokendance-m1") == "20261106"
    assert cross_window._day_of("workspace/verify-blocked") is None


def test_day_of_rejects_digit_runs_that_are_not_valid_dates():
    """模型名里偶然的 8 位数字不能被当成日期——那会造出一个不存在的窗口。"""
    assert cross_window._day_of("runs/20261399-m1") is None   # 13 月 99 日
    assert cross_window._day_of("runs/20260230-m1") is None   # 2 月 30 日
    assert cross_window._day_of("runs/20261106-m1") == "20261106"


def test_scan_first_seen_keeps_earliest_day_and_ignores_foreign_ids(tmp_path):
    corpus = {"a1", "a2"}
    _run_dir(tmp_path, "20261106", {"a1": [True], "ghost": [True]})
    _run_dir(tmp_path, "20261020", {"a1": [False], "a2": [True]})
    first_seen, scanned = cross_window.scan_first_seen(corpus, tmp_path)
    assert first_seen == {"a1": "20261020", "a2": "20261020"}
    assert scanned == 2


def test_earliest_day_wins_over_scan_order(tmp_path):
    """取“最早”而不是“最先扫到”：归档目录名不保证按时间排序，不同父目录也会被扫进来。

    这里刻意把较晚的批次放在先扫的 `reports/runs/`、较早的放在后扫的 `workspace/`，
    使“插入顺序”与“时间顺序”相反——否则这条断言抓不到“记第一次命中”的错法。
    """
    corpus = {"a1"}
    _run_dir(tmp_path, "20261106", {"a1": [True]})
    later_workspace = tmp_path / "workspace" / "20261020-120000-m1"
    later_workspace.mkdir(parents=True, exist_ok=True)
    (later_workspace / "results.jsonl").write_text(
        json.dumps({"result": {"case_id": "a1", "strict_passed": False}}), encoding="utf-8"
    )
    first_seen, scanned = cross_window.scan_first_seen(corpus, tmp_path)
    assert scanned == 2
    assert first_seen == {"a1": "20261020"}


def test_unobservable_case_gets_no_invented_timestamp(tmp_path):
    """从未在任何历史批次出现过的题只能进 unassigned，不给它编时点。"""
    corpus = {"a1", "never-run"}
    _run_dir(tmp_path, "20261106", {"a1": [True]})
    first_seen, _ = cross_window.scan_first_seen(corpus, tmp_path)
    assert "never-run" not in first_seen
    rows = [{"id": "a1"}, {"id": "never-run"}]
    manifest, unassigned = cross_window.build_manifest_from_evidence(rows, first_seen, 7)
    assert unassigned == ["never-run"]
    assert manifest["unassigned"] == ["never-run"]


# --------------------------------------------------------------------------
# 聚簇与 manifest 构造
# --------------------------------------------------------------------------


def test_cluster_windows_splits_on_gap_threshold():
    first_seen = {"a": "20261020", "b": "20261022", "c": "20261106", "d": "20261110"}
    groups = cross_window.cluster_windows(first_seen, min_gap_days=7)
    assert groups == [["20261020", "20261022"], ["20261106", "20261110"]]


def test_cluster_windows_with_small_gap_stays_one_window():
    first_seen = {"a": "20261020", "b": "20261023"}
    assert cross_window.cluster_windows(first_seen, min_gap_days=7) == [["20261020", "20261023"]]


def test_cluster_windows_empty_evidence_yields_no_windows():
    assert cross_window.cluster_windows({}, min_gap_days=7) == []


def test_cutoff_is_the_latest_day_in_the_cluster():
    """cutoff 必须晚于簇内每题的首次观测日，否则窗口里含"当时还不存在"的题。"""
    first_seen = {"a": "20261020", "b": "20261022", "c": "20261106"}
    rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    manifest, _ = cross_window.build_manifest_from_evidence(rows, first_seen, 7)
    assert [window["cutoff"] for window in manifest["windows"]] == ["2026-10-22", "2026-11-06"]
    assert manifest["windows"][0]["declared"] == ["a", "b"]


def test_manifest_declares_its_lower_bound_nature():
    """措辞红线：下界不是准点，这句话必须落进产物，而不是只写在脚本注释里。"""
    first_seen = {"a": "20261020"}
    manifest, _ = cross_window.build_manifest_from_evidence([{"id": "a"}], first_seen, 7)
    assert "下界" in manifest["basis"]
    assert manifest["protocol"] == PROTOCOL_ID


def test_generated_manifest_passes_engine_validation(tmp_path):
    """脚本产出的 manifest 必须过引擎的硬校验——两边口径不能各说一套。"""
    first_seen = {"a": "20261020", "b": "20261022", "c": "20261106"}
    rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    manifest, _ = cross_window.build_manifest_from_evidence(rows, first_seen, 7)
    audit = cross_window.validate_manifest(manifest, {"a", "b", "c"})
    assert audit["covered_ids"] == 3


# --------------------------------------------------------------------------
# 子命令的退出码与措辞
# --------------------------------------------------------------------------


def test_plan_trims_future_cases_and_exits_zero(tmp_path, capsys):
    corpus = _corpus(tmp_path, ["a1", "a2", "b1"])
    manifest = {
        "protocol": PROTOCOL_ID,
        "corpus": str(corpus),
        "windows": [
            {"window": 0, "cutoff": "2026-10-22", "declared": ["a1", "a2"]},
            {"window": 1, "cutoff": "2026-11-06", "declared": ["b1"]},
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    code = cross_window.cmd_plan(
        Namespace(manifest=str(path), corpus=str(corpus), window=0, ids=True)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "被隔离的未来题 1 条" in out
    assert "+ b1" not in out


def test_validate_surfaces_unverified_manifest(tmp_path, capsys):
    corpus = _corpus(tmp_path, ["a1"])
    manifest = {
        "protocol": PROTOCOL_ID,
        "verified": False,
        "windows": [{"window": 0, "cutoff": "2026-10-22", "declared": ["a1"]}],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    code = cross_window.cmd_validate(Namespace(manifest=str(path), corpus=str(corpus)))
    out = capsys.readouterr().out
    assert code == 0
    assert "不得用于对外结论" in out


def _two_window_fixture(tmp_path):
    corpus = _corpus(tmp_path, ["a1", "a2", "b1"])
    manifest = {
        "protocol": PROTOCOL_ID,
        "metric": "strict_pass_rate",
        "windows": [
            {"window": 0, "cutoff": "2026-10-22", "declared": ["a1", "a2"]},
            {"window": 1, "cutoff": "2026-11-06", "declared": ["b1"]},
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return corpus, path


def test_delta_reports_retained_as_the_only_attributable_number(tmp_path, capsys):
    corpus, manifest_path = _two_window_fixture(tmp_path)
    baseline = _run_dir(tmp_path, "20261022", {"a1": [True, True], "a2": [True, True]})
    current = _run_dir(
        tmp_path, "20261106",
        {"a1": [True, True], "a2": [True, False], "b1": [True, True]},
    )
    code = cross_window.cmd_delta(
        Namespace(
            manifest=str(manifest_path), corpus=str(corpus), window=1,
            baseline_run=str(baseline), current_run=str(current), band_pp=50.0,
        )
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "差异未证实" in out           # 带内：不得宣称劣化
    assert "不得**引用为能力增益" in out  # delta_total 的措辞护栏
    assert "归因字段只有 delta_retained" in out


def test_delta_without_band_or_repeat_declines_to_judge(tmp_path, capsys):
    """没有带宽就没有"超出/未超出"可言：只报数，不下判定。"""
    corpus, manifest_path = _two_window_fixture(tmp_path)
    baseline = tmp_path / "reports" / "runs" / "20261022-120000-m1"
    baseline.mkdir(parents=True, exist_ok=True)
    (baseline / "results.jsonl").write_text(
        json.dumps({"result": {"case_id": "a1", "strict_passed": True}}) + "\n"
        + json.dumps({"result": {"case_id": "a2", "strict_passed": True}}) + "\n",
        encoding="utf-8",
    )
    current = tmp_path / "reports" / "runs" / "20261106-120000-m1"
    current.mkdir(parents=True, exist_ok=True)
    (current / "results.jsonl").write_text(
        json.dumps({"result": {"case_id": "a1", "strict_passed": False}}) + "\n"
        + json.dumps({"result": {"case_id": "a2", "strict_passed": True}}) + "\n"
        + json.dumps({"result": {"case_id": "b1", "strict_passed": True}}) + "\n",
        encoding="utf-8",
    )
    code = cross_window.cmd_delta(
        Namespace(
            manifest=str(manifest_path), corpus=str(corpus), window=1,
            baseline_run=str(baseline), current_run=str(current), band_pp=None,
        )
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "不作判定" in out
    assert "超出噪声带" not in out


def test_delta_fails_closed_when_a_run_has_no_results(tmp_path, capsys):
    corpus, manifest_path = _two_window_fixture(tmp_path)
    empty = tmp_path / "reports" / "runs" / "20261106-120000-m1"
    empty.mkdir(parents=True)
    (empty / "results.jsonl").write_text("", encoding="utf-8")
    code = cross_window.cmd_delta(
        Namespace(
            manifest=str(manifest_path), corpus=str(corpus), window=1,
            baseline_run=str(tmp_path / "missing"), current_run=str(empty), band_pp=5.0,
        )
    )
    assert code == 1
    assert "不予计算" in capsys.readouterr().out


def test_flips_of_excludes_cases_with_no_usable_pair():
    """两期都是环境故障的题不得进分母：那会把带宽往"很稳定"的方向推。"""
    flips, total = cross_window._flips_of(
        {"a": [True, False], "b": [None, None], "c": [True, None], "d": [True, True]}
    )
    assert (flips, total) == (1, 2)


def test_main_maps_protocol_violation_to_nonzero_exit(tmp_path, monkeypatch, capsys):
    corpus = _corpus(tmp_path, ["a1"])
    bad = tmp_path / "manifest.json"
    bad.write_text(
        json.dumps({"protocol": "aqg-cross-window/99", "windows": [{"window": 0}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        ["cross_window.py", "validate", "--manifest", str(bad), "--corpus", str(corpus)],
    )
    assert cross_window.main() == 1
    assert "协议违规" in capsys.readouterr().out
