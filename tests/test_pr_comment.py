"""T22：pr_comment.py 的单测——纯函数＋tmp_path 钉桩，零网络零 gh。"""
import json

import pr_comment


def row(name, gate_status=None, reasons=None, strict=61.5, critical=2, high=5,
        valid=98.3, epoch="epoch-A"):
    metrics = {"strict_rate": strict, "critical": critical, "high": high,
               "valid_rate": valid, "dataset_epoch": epoch}
    if gate_status:
        metrics["gate"] = {"status": gate_status, "reasons": reasons or []}
    return {"name": name, "status": gate_status or "passed", "metrics": metrics}


def summary(*rows, batch_id="batch-20261121-014700"):
    return {"batch_id": batch_id, "meta": {}, "rows": list(rows)}


def test_build_comment_table_rows_and_icons():
    md = pr_comment.build_comment(summary(
        row("model-a", "passed"),
        row("model-b", "blocked", reasons=["strict_rate 61.5% < 门槛 80%"]),
    ))
    assert "### AQG 门禁摘要｜批次 `batch-20261121-014700`" in md
    assert "| model-a | ✅ `passed` | 61.50% | 2 | 5 | 98.30% |" in md
    assert "| model-b | ⛔ `blocked` |" in md
    assert "数据集代际 `epoch-A`｜共 2 场" in md


def test_build_comment_blocked_reasons_in_details():
    md = pr_comment.build_comment(summary(
        row("model-a", "passed"),
        row("model-b", "warn", reasons=["有效样本率 91.0% < 门槛 95%"]),
    ))
    assert "<details>" in md
    assert "- **model-b**" in md
    assert "  - 有效样本率 91.0% < 门槛 95%" in md


def test_passed_rows_reasons_are_not_surfaced():
    """passed 场次即便带 reasons 也不进阻断区（只展示真正阻断/降级的）。"""
    md = pr_comment.build_comment(summary(
        row("model-a", "passed", reasons=["曾经低于门槛"]),
    ))
    assert "<details>" not in md
    assert "曾经低于门槛" not in md


def test_missing_fields_degrade_to_dash():
    md = pr_comment.build_comment(summary({"name": "bare", "metrics": {}}))
    assert "| bare |  `unknown` | — | — | — | — |" in md


def test_mixed_epochs_warns_in_header():
    md = pr_comment.build_comment(summary(
        row("model-a", epoch="epoch-A"),
        row("model-b", epoch="epoch-B"),
    ))
    assert "数据集代际不一致" in md


def test_empty_rows_short_circuit():
    md = pr_comment.build_comment(summary())
    assert "本批次没有任何 run 记录" in md
    assert "| 模型 |" not in md


def test_find_latest_summary_picks_max_batch(tmp_path):
    for bid in ["batch-20261118-131428", "batch-20261121-014700", "batch-dryrun-20261121-0144"]:
        d = tmp_path / bid
        d.mkdir(parents=True)
        (d / "summary.json").write_text(json.dumps(summary(batch_id=bid)), encoding="utf-8")
    latest = pr_comment.find_latest_summary(tmp_path)
    assert latest.parent.name == "batch-20261121-014700"


def test_find_latest_keeps_short_timestamp_batch(tmp_path):
    """--batch-name 允许分钟级后缀（真实批次 batch-20261121-0147），不能误剔。"""
    for bid in ["batch-20261118-131428", "batch-20261121-0147", "batch-attack-probe"]:
        d = tmp_path / bid
        d.mkdir(parents=True)
        (d / "summary.json").write_text(json.dumps(summary(batch_id=bid)), encoding="utf-8")
    latest = pr_comment.find_latest_summary(tmp_path)
    assert latest.parent.name == "batch-20261121-0147"


def test_find_latest_summary_none_when_empty(tmp_path):
    assert pr_comment.find_latest_summary(tmp_path) is None


def test_main_writes_out_file(tmp_path):
    src = tmp_path / "summary.json"
    src.write_text(json.dumps(summary(row("model-a"))), encoding="utf-8")
    out = tmp_path / "comment.md"
    rc = pr_comment.main(["--summary", str(src), "--out", str(out)])
    assert rc == 0
    assert "model-a" in out.read_text(encoding="utf-8")


def test_main_missing_data_returns_3(tmp_path):
    rc = pr_comment.main(["--reports-dir", str(tmp_path / "not-here")])
    assert rc == 3


def test_main_corrupt_json_returns_1(tmp_path):
    src = tmp_path / "summary.json"
    src.write_text("{not json", encoding="utf-8")
    assert pr_comment.main(["--summary", str(src)]) == 1
