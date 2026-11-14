import json

import run_batch


def write_results(run_dir, rows):
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def harness_row(message: str) -> dict:
    return {
        "result": {"judge_layer": "harness", "judge_reason": "harness", "evidence": ""},
        "trace": {"is_success": False, "error_message": message},
    }


def test_extract_infra_hint_surfaces_gateway_error(tmp_path):
    """整批 429 这类环境故障要在批次摘要里直接露出来，而不是只留一个低分。"""
    run_dir = tmp_path / "run-1"
    write_results(
        run_dir,
        [harness_row("Error code: 429 - {'error': {'message': 'user access to this plan has been disabled'}}")] * 3
        + [{"result": {"judge_layer": "llm"}, "trace": {"is_success": True, "error_message": None}}],
    )
    hint = run_batch.extract_infra_hint(run_dir)
    assert "Error code: 429" in hint
    assert "×3" in hint


def test_extract_infra_hint_ignores_valid_samples(tmp_path):
    run_dir = tmp_path / "run-2"
    write_results(run_dir, [{"result": {"judge_layer": "rule"}, "trace": {"is_success": True}}])
    assert run_batch.extract_infra_hint(run_dir) == ""


def test_extract_infra_hint_tolerates_missing_and_broken_files(tmp_path):
    assert run_batch.extract_infra_hint(tmp_path / "not-here") == ""
    run_dir = tmp_path / "run-3"
    run_dir.mkdir()
    (run_dir / "results.jsonl").write_text("{ not json\n\n", encoding="utf-8")
    assert run_batch.extract_infra_hint(run_dir) == ""


def test_min_valid_rate_is_passed_to_run_eval(tmp_path):
    spec = {"name": "m", "model": "m", "base_url": "u", "suite": "smoke", "limit": 10, "min_valid_rate": 85}
    command = run_batch.build_command(spec, "run-1", tmp_path, False, False)
    assert "--min-valid-rate" in command
    assert command[command.index("--min-valid-rate") + 1] == "85"


def test_dataset_identity_follows_dataset_not_suite():
    """指定了自定义数据集时，报告里的数据集标识必须跟着数据集走。

    以前 `spec.setdefault("suite", "core")` 盖不掉显式配置，于是跑 4 条探针集的
    批次在汇总表里被写成 `core(0)`——看的人以为跑的是全量 core。
    """
    spec = run_batch.normalize_spec(
        {"name": "m", "model": "m", "base_url": "u"},
        {"dataset": "cases/attack_probe.jsonl", "limit": 0},
        0,
    )
    assert spec["suite"] == "cases/attack_probe.jsonl"
    # 没给 dataset 时才回落到 core。
    fallback = run_batch.normalize_spec({"name": "m", "model": "m", "base_url": "u"}, {}, 0)
    assert fallback["suite"] == "core"


def test_dataset_label_renders_dataset_stem():
    """汇总表的“数据集”列：路径取文件名主干，内置名原样保留。"""
    assert run_batch.dataset_label("cases/attack_probe.jsonl", 0) == "attack_probe(0)"
    assert run_batch.dataset_label("core", 120) == "core(120)"
    assert run_batch.dataset_label("", 0) == "-"


def test_build_command_passes_dataset_generation(tmp_path):
    """子集视图要能声明自己属于哪一代语料，否则会被当成新一代数据集。"""
    spec = {
        "name": "m",
        "model": "m",
        "base_url": "u",
        "dataset": "cases/attack_probe.jsonl",
        "dataset_generation": "deff7501007cff61",
        "limit": 0,
    }
    command = run_batch.build_command(spec, "run-1", tmp_path, False, False)
    assert "--dataset-generation" in command
    assert command[command.index("--dataset-generation") + 1] == "deff7501007cff61"


def test_build_command_supports_no_publish(tmp_path):
    """探索性批次可以不改共享状态（主台账、reports/latest.md）。"""
    spec = {"name": "m", "model": "m", "base_url": "u", "publish": False, "limit": 0}
    command = run_batch.build_command(spec, "run-1", tmp_path, False, False)
    assert "--no-publish" in command
    # 默认仍然发布。
    default = run_batch.build_command(
        {"name": "m", "model": "m", "base_url": "u", "limit": 0}, "run-1", tmp_path, False, False
    )
    assert "--no-publish" not in default


def test_build_command_passes_judge_kappa(tmp_path):
    """κ 权限表必须能从清单透传到子进程，否则批跑会静默退回“不传 κ”的旧行为。"""
    spec = {"name": "m", "model": "m", "base_url": "u", "limit": 0, "judge_kappa": 0.8412}
    command = run_batch.build_command(spec, "run-1", tmp_path, False, False)
    assert "--judge-kappa" in command
    assert command[command.index("--judge-kappa") + 1] == "0.8412"
    # 未配置时不传，保持旧行为。
    default = run_batch.build_command(
        {"name": "m", "model": "m", "base_url": "u", "limit": 0}, "run-1", tmp_path, False, False
    )
    assert "--judge-kappa" not in default


def test_summary_marks_invalid_rows_as_uncomparable(tmp_path):
    rows = [
        {
            "name": "broken-model",
            "status": "invalid",
            "suite": "core",
            "limit": 120,
            "duration_s": 64.1,
            "note": "仅 1/120 条样本可信",
            "metrics": {
                "total_cases": 120,
                "valid_cases": 1,
                "valid_rate": 0.83,
                "std_pass_rate": 0.83,
                "strict_pass_rate": 0.83,
                "strict_pass_rate_valid_only": 100.0,
                "critical_count": 0,
                "high_count": 119,
                "gate": {"status": "invalid"},
            },
        }
    ]
    meta = {"on_error": "continue", "on_gate": "stop", "update_regression": False}
    text = run_batch.render_summary("batch-test", meta, rows)
    assert "可信样本" in text
    assert "Strict(仅可信)" in text
    assert "不能与其他模型横向对比" in text
    assert "仅 1/120 条样本可信" in text
