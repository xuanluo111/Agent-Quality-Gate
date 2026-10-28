"""drift_check 跨批次漂移核验单测（全离线，喂合成摘要）。"""
import json

from drift_check import compare, main


def _summary(**over):
    base = {
        "run_id": "run-A",
        "model": "spark-x2.5-1.7b",
        "model_version": "spark-x2.5-1.7b",
        "judge_model": "glm-4-flash",
        "dataset_hash": "a" * 64,
        "total_cases": 120,
        "standard_pass": 109,
        "strict_pass": 95,
    }
    base.update(over)
    return base


def test_identical_batches_are_stable():
    report = compare(_summary(), _summary(run_id="run-B"))
    assert report["comparable"] is True
    assert report["verdict"] == "统计稳定"
    assert report["action_required"] is False
    assert report["metrics"]["strict"]["significant"] is False


def test_significant_regression_flagged():
    # 同一代际、同一供应商，strict 95/120 → 78/120，应当判显著劣化并要求处置。
    report = compare(_summary(), _summary(run_id="run-B", strict_pass=78))
    strict = report["metrics"]["strict"]
    assert strict["direction"] == "down"
    assert strict["significant"] is True
    assert report["verdict"] == "显著劣化"
    assert report["action_required"] is True
    assert any("显著劣化" in s for s in report["signals"])


def test_small_wobble_not_significant():
    # 只差 2 条（95→97/120）不该被读成趋势。
    report = compare(_summary(), _summary(strict_pass=97, standard_pass=111))
    assert report["metrics"]["strict"]["significant"] is False
    assert report["verdict"] == "统计稳定"


def test_provider_change_detected_and_blocks():
    report = compare(_summary(), _summary(judge_model="glm-5", run_id="run-B"))
    fields = {c["field"] for c in report["provider_changes"]}
    assert "judge_model" in fields
    assert report["verdict"] == "供应商变更"
    assert report["action_required"] is True


def test_generation_breakpoint_is_not_comparable_and_not_a_regression():
    # 换代（dataset_hash 变）时不算显著漂移，即便比率大跌也只判“跨代不可比”。
    report = compare(_summary(), _summary(dataset_hash="b" * 64, strict_pass=70))
    assert report["comparable"] is False
    assert report["generation_breakpoint"] is True
    assert report["metrics"]["strict"]["significant"] is False
    assert report["verdict"] == "跨代不可比"
    assert report["action_required"] is True


def test_significant_improvement_direction():
    report = compare(_summary(), _summary(strict_pass=115, standard_pass=119))
    assert report["metrics"]["strict"]["direction"] == "up"
    assert report["metrics"]["strict"]["significant"] is True


def test_main_exit_code_and_json(tmp_path):
    baseline = tmp_path / "a.json"
    current = tmp_path / "b.json"
    baseline.write_text(json.dumps(_summary()), encoding="utf-8")
    current.write_text(json.dumps(_summary(strict_pass=95)), encoding="utf-8")
    # 稳定：退出 0
    assert main(["--baseline", str(baseline), "--current", str(current)]) == 0
    # 劣化：退出 1
    current.write_text(json.dumps(_summary(strict_pass=70)), encoding="utf-8")
    assert main(["--baseline", str(baseline), "--current", str(current), "--json"]) == 1
