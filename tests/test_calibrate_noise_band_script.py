"""噪声带标定 CLI（scripts/calibrate_noise_band.py）的判据测试。

脚本层只测三件事：目录发现、指纹可读化、以及**没有可用样本时必须拒绝给结论**。
统计口径本身在 tests/test_noise_calibration.py 里测。
"""

import json
import sys
from pathlib import Path

import calibrate_noise_band as cli


BASE = {
    "model": "m1",
    "model_version": "v1",
    "base_url": "https://example.test/v1",
    "dataset": "cases/core_suite.jsonl",
    "dataset_generation": "deff7501007cff61",
    "seed": 42,
    "temperature": 0.1,
    "max_rounds": 4,
    "token_budget": 24000,
    "max_tokens": 4096,
    "case_time_budget": 480,
    "judge_model": "j1",
    "judge_samples": 2,
    "repeat": 2,
    "jcv_threshold": 0.6,
    "collateral_risk": "high",
}


def _make_run(root: Path, name: str, bits_by_case: dict, config: dict) -> Path:
    run = root / name
    run.mkdir(parents=True, exist_ok=True)
    (run / "config.json").write_text(json.dumps(config), encoding="utf-8")
    lines = []
    for case_id, bits in bits_by_case.items():
        lines.append(
            json.dumps(
                {
                    "result": {
                        "case_id": case_id,
                        "strict_passed": all(bit is True for bit in bits),
                        "attempts": [
                            {"attempt": index + 1, "strict_passed": bit}
                            for index, bit in enumerate(bits)
                        ],
                    }
                },
                ensure_ascii=False,
            )
        )
    (run / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run


def test_label_of_is_readable_and_keeps_budget():
    """明细行要靠预算区分组：指纹压完还得认得出是哪一档。"""
    from src.engine.noise_calibration import config_fingerprint

    label = cli.label_of(config_fingerprint(BASE))
    assert "m1" in label
    assert "budget=24000/4096" in label
    assert "judge=2×j1" in label


def test_collect_run_dirs_only_keeps_directories_with_results(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_GLOBS", ("runs/*",))
    good = _make_run(tmp_path, "runs/20261106-120000-m1", {"a": [True, False]}, BASE)
    (tmp_path / "runs" / "empty-run").mkdir()
    found = cli.collect_run_dirs([])
    assert found == [str(good)]


def test_collect_run_dirs_accepts_plain_directory_without_glob(tmp_path, monkeypatch):
    """传目录（不带通配）也要能用：历史归档里就是一片平铺目录。"""
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_GLOBS", ())
    run = _make_run(tmp_path, "archive/20261022-142756-m1", {"a": [True, True]}, BASE)
    assert cli.collect_run_dirs([str(run)]) == [str(run)]


def _invoke(monkeypatch, tmp_path, argv) -> int:
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    saved = sys.argv
    sys.argv = ["calibrate_noise_band.py", *argv]
    try:
        return cli.main()
    finally:
        sys.argv = saved


def test_no_repeat_data_refuses_to_emit_a_band(tmp_path, monkeypatch, capsys):
    """repeat=1 的批次里没有"重跑"可言：此时必须拒绝给数，而不是给个 0pp。"""
    monkeypatch.setattr(cli, "DEFAULT_GLOBS", ("runs/*",))
    _make_run(tmp_path, "runs/20261106-120000-m1", {"a": [True]}, {**BASE, "repeat": 1})
    code = _invoke(monkeypatch, tmp_path, [])
    out = capsys.readouterr().out
    assert code == 1
    assert "没有任何可用样本，不给结论" in out


def test_lower_bound_wording_is_mandatory_without_seed(tmp_path, monkeypatch, capsys):
    """没落盘 seed 就不能宣布"已标定"：脚本必须把"保守下界"打在输出上。"""
    monkeypatch.setattr(cli, "DEFAULT_GLOBS", ("runs/*",))
    _make_run(
        tmp_path, "runs/20261106-120000-m1",
        {f"c{i}": [True, i % 7 == 0] for i in range(40)}, BASE,
    )
    code = _invoke(monkeypatch, tmp_path, [])
    out = capsys.readouterr().out
    assert code == 0
    assert "可宣布已标定: 0" in out
    assert "保守下界" in out
    assert "不替换" in out           # 口径纪律随数一起输出，不让下游顺手换带宽
    assert "（未写任何文件" in out


def test_json_output_is_opt_in_only(tmp_path, monkeypatch, capsys):
    """默认零写入：标定是一次性读取动作，不该在仓库里留下未经审阅的产物。"""
    monkeypatch.setattr(cli, "DEFAULT_GLOBS", ("runs/*",))
    _make_run(
        tmp_path, "runs/20261106-120000-m1",
        {f"c{i}": [True, i % 7 == 0] for i in range(40)}, BASE,
    )
    target = tmp_path / "band.json"
    _invoke(monkeypatch, tmp_path, ["--json", str(target)])
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["summary_status"] == "lower_bound_only"
    assert payload["detail"]
