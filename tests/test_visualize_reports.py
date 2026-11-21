"""visualize_reports.load_reports 的 --latest-per-model 口径单测。

动机（08 待拍板第 3 行）：T18 修复批重跑后 runs 里同名模型有两场
（旧 invalid 场审计保留＋新修复场），默认渲染会把它双计成两票。
本参数只让最新一场进统计图，旧场进 skipped 清单可审计。
判据全部用钉桩 summary.json，不依赖真实 reports/ 内容。
"""
import json

import visualize_reports as vr


def write_summary(reports_dir, run_name, model, started_at=None, extra=None):
    data = {
        "model": model,
        "strict_pass_rate": 70.0,
        "gate": {"status": "passed", "metrics": {}},
        "total_cases": 207,
    }
    if started_at is not None:
        data["started_at"] = started_at
    if extra:
        data.update(extra)
    run = reports_dir / "runs" / run_name
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps(data), encoding="utf-8")


def test_latest_per_model_keeps_only_newest_run_of_each_model(tmp_path):
    write_summary(tmp_path, "20261118-131428-spark", "spark-4b", "2026-11-18T13:14:28")
    write_summary(tmp_path, "20261121-0147-spark", "spark-4b", "2026-11-21T01:47:00")
    write_summary(tmp_path, "20261118-131428-glm", "glm-4-flash", "2026-11-18T13:14:30")
    reports, skipped = vr.load_reports(tmp_path, latest_per_model=True)
    assert [(r["model"], r["file"].split("/")[1]) for r in reports] == [
        ("glm-4-flash", "20261118-131428-glm"),
        ("spark-4b", "20261121-0147-spark"),
    ]
    assert len(skipped) == 1 and skipped[0]["kind"] == "superseded"
    assert "20261118-131428-spark" in skipped[0]["file"] and "旧场" in skipped[0]["reason"]


def test_default_rendering_stores_both_runs_untouched(tmp_path):
    """不带参数时必须维持旧行为：多场并存、display 带时间戳后缀——防止「顺手改默认」。"""
    write_summary(tmp_path, "20261118-131428-spark", "spark-4b", "2026-11-18T13:14:28")
    write_summary(tmp_path, "20261121-0147-spark", "spark-4b", "2026-11-21T01:47:00")
    reports, skipped = vr.load_reports(tmp_path)
    assert len(reports) == 2 and not skipped
    assert all("·" in r["display"] for r in reports)


def test_latest_per_model_uses_started_at_not_generated_at(tmp_path):
    """排序键取 started_at：旧场即使后生成（重判/重出报告），也不能反过来顶掉新场。"""
    write_summary(tmp_path, "20261121-0147-spark", "spark-4b", "2026-11-21T01:47:00",
                  {"generated_at": "2026-11-19T00:00:00"})
    write_summary(tmp_path, "20261118-131428-spark", "spark-4b", "2026-11-18T13:14:28",
                  {"generated_at": "2026-11-25T00:00:00"})
    reports, skipped = vr.load_reports(tmp_path, latest_per_model=True)
    assert [r["file"] for r in reports] == ["runs/20261121-0147-spark/summary.json"]
    assert len(skipped) == 1


def test_latest_per_model_single_run_model_display_stays_plain(tmp_path):
    """过滤后每模型只剩一场：display 不应残留时间戳后缀（counts 必须在过滤后算）。"""
    write_summary(tmp_path, "20261118-131428-spark", "spark-4b", "2026-11-18T13:14:28")
    write_summary(tmp_path, "20261121-0147-spark", "spark-4b", "2026-11-21T01:47:00")
    reports, _ = vr.load_reports(tmp_path, latest_per_model=True)
    assert reports[0]["display"] == "spark-4b"


def test_latest_per_model_missing_stamp_falls_back_to_last_occurrence(tmp_path):
    """无 started_at/generated_at 时 sort_stamp 为空串，排序稳定按路径序——
    必须取最后一次出现（路径序最大），且结果可复现。"""
    write_summary(tmp_path, "20261118-000000-spark", "spark-4b")
    write_summary(tmp_path, "20261121-000000-spark", "spark-4b")
    reports, skipped = vr.load_reports(tmp_path, latest_per_model=True)
    assert [r["file"] for r in reports] == ["runs/20261121-000000-spark/summary.json"]
    assert len(skipped) == 1


def test_selfcheck_exclusion_composes_with_latest(tmp_path):
    """verify-* 自检场在 latest 过滤前就被排除，不得因「时间最新」顶掉正式场。"""
    write_summary(tmp_path, "20261110-000000-spark", "spark-4b", "2026-11-10T00:00:00")
    write_summary(tmp_path, "verify-20261122-000000", "spark-4b", "2026-11-22T00:00:00")
    reports, skipped = vr.load_reports(tmp_path, latest_per_model=True)
    assert [r["file"] for r in reports] == ["runs/20261110-000000-spark/summary.json"]
    kinds = {item.get("kind") for item in skipped}
    assert kinds == {"selfcheck"}  # verify 场走原有排除通道，不混入 superseded
