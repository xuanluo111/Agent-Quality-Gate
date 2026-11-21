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


def test_already_done_matches_run_dir_by_model_slug(tmp_path):
    """T24 断点续跑口径：--skip-existing 只认「历史批次目录尾缀 = 模型 slug 且有 report.md」。

    run_dir 布局是 `{batch_id}-{slug(name)}`；这里钉住三个易错面：
    命中前缀任意批次 ID、未跑过的模型不算完成、有目录没报告（中途崩）不算完成。
    """
    (tmp_path / "20261121-0147-my-model" / "run-a").mkdir(parents=True)
    (tmp_path / "20261121-0147-my-model" / "report.md").write_text("# ok", encoding="utf-8")
    assert run_batch.already_done(tmp_path, "my-model") is True

    # 换一批批次 ID 也能命中（glob 前缀 `*` 覆盖任意 batch_id）
    (tmp_path / "20261122-0930-my-model").mkdir()
    assert run_batch.already_done(tmp_path, "my-model") is True

    assert run_batch.already_done(tmp_path, "other-model") is False  # 没跑过

    (tmp_path / "20261122-0931-half-baked").mkdir()  # 有目录无报告 = 中断过，必须重跑
    assert run_batch.already_done(tmp_path, "half-baked") is False


def test_already_done_rejects_directory_named_report(tmp_path):
    """report.md 是目录这种脏布局不能算完成：is_file() 而非 exists() 是有意选择。"""
    (tmp_path / "20261122-0932-weird" / "report.md").mkdir(parents=True)
    assert run_batch.already_done(tmp_path, "weird") is False


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
    """κ 权限表必须能从清单透传到子进程：没透传就等于未标定，能力判据会静默失去阻断权。"""
    spec = {"name": "m", "model": "m", "base_url": "u", "limit": 0, "judge_kappa": 0.8412}
    command = run_batch.build_command(spec, "run-1", tmp_path, False, False)
    assert "--judge-kappa" in command
    assert command[command.index("--judge-kappa") + 1] == "0.8412"
    # 未配置时不硬造一个值：不传就是未标定（unverified），由门禁自己做权限判定。
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


def test_summary_states_same_source_judge_is_a_red_line():
    """K09：同源场次要说成“分数不成立、需重跑”，不能写成“判分需打折”。"""
    rows = [
        {
            "name": "self-judged",
            "status": "invalid",
            "suite": "core",
            "limit": 0,
            "duration_s": 10.0,
            "metrics": {
                "total_cases": 10,
                "judge_model": "self-judged",
                "judge_same_as_sut": True,
                "gate": {"status": "invalid"},
            },
        }
    ]
    meta = {"on_error": "continue", "on_gate": "stop", "update_regression": False}
    text = run_batch.render_summary("batch-test", meta, rows)
    assert "裁判与被测模型同源" in text and "不构成能力结论" in text
    assert "打折" not in text


# ---------- T14：排名解读的多重比较校正 / 效应量 / 置信等级 ----------

TOTAL = 207


def _matrix_row(name: str, passed: int, **metrics_over) -> dict:
    """按 run_batch 实际消费的 summary 字段造一场（只给排名用到的那几个）。"""
    metrics = {
        "total_cases": TOTAL,
        "strict_pass": passed,
        "strict_pass_rate_valid_only": passed / TOTAL * 100,
        "strict_ci_low": 80.0,
        "strict_ci_high": 95.0,
        "valid_cases": passed,
        "data_reliable": True,
    }
    metrics.update(metrics_over)
    return {"name": name, "status": "ok", "suite": "core", "limit": 0, "metrics": metrics}


def _four_model_rows() -> list[dict]:
    # 190/207 基准；188 差 1 条；177 原始 p=0.044（校正后翻车）；165 校正后仍显著。
    return [
        _matrix_row("ling", 190),
        _matrix_row("dots", 188),
        _matrix_row("mimo", 177),
        _matrix_row("spark", 165),
    ]


def _row_of(text: str, name: str) -> str:
    return next(line for line in text.splitlines() if f"| {name} |" in line)


def test_rank_block_corrects_p_and_shows_effect_size():
    text = run_batch._rank_block(_four_model_rows())

    assert "| 名次 | 模型 | Strict | Strict(仅可信) | 95% CI | 可信样本 | 相对基准 | 原始 p | 校正后 p | 结论 |" in text
    # 「校正」字样必须显式落到读者眼前，而不是藏在实现里。
    assert "Holm–Bonferroni" in text
    assert "未做多重比较校正" not in text

    mimo = _row_of(text, "mimo")
    assert "| -6.28pp（h=0.20） | 0.044 | 0.088 | B｜" in mimo
    dots = _row_of(text, "dots")
    assert "| -0.97pp（h=0.03） | 0.727 | 0.727 | C｜" in dots
    assert "<0.001 | 0.001 | A｜" in _row_of(text, "spark")


def test_rank_block_flips_the_marginal_call_and_says_so():
    """验收「校正前后对照」：原始 p<0.05 而校正后≥0.05 的场次必须被点名。"""
    text = run_batch._rank_block(_four_model_rows())
    assert "翻转场次：mimo" in text

    corrected = run_batch._rank_block([_matrix_row("ling", 190), _matrix_row("mimo", 177)])
    # m=1 时校正不该改变任何结论，对照行要如实说「无翻转」。
    assert "无翻转场次" in corrected
    assert "| 0.044 | 0.044 | A｜" in _row_of(corrected, "mimo")


def test_rank_block_never_writes_that_two_models_are_equal():
    """措辞红线：p≥0.05 只能说「差异未证实」，不能写成「统计上不可区分」。"""
    text = run_batch._rank_block(_four_model_rows())
    assert "统计上不可区分" not in text
    assert "不是「两者相同」" in text
    assert "差异未证实（未超噪声带）" in text


def test_rank_block_marks_the_band_as_a_proxy_not_a_measured_noise_floor():
    text = run_batch._rank_block(_four_model_rows())
    assert "代理值、非实测抖动" in text
    assert "T32" in text
    # 等级不是准出条件，这句必须在场，否则读者会以为 A/B/C 会拦发布。
    assert "任何一档都不是准出条件" in text


def test_rank_block_is_absent_below_two_comparable_rows():
    assert run_batch._rank_block([_matrix_row("ling", 190)]) == ""
    assert run_batch._rank_block([]) == ""


def test_rank_block_correction_is_load_bearing(monkeypatch):
    """反例验证：把校正换成恒等，mimo 必须从 B 变回 A，且对照行不再点名它。"""
    monkeypatch.setattr(run_batch, "holm_adjust", lambda p_values: list(p_values))
    text = run_batch._rank_block(_four_model_rows())
    assert "| 0.044 | 0.044 | A｜" in _row_of(text, "mimo")
    assert "翻转场次：" not in text
