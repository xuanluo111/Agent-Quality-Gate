"""仅增预算对照组 CLI（scripts/budget_ab.py）的判据测试。

这一组的价值全在"唯一变量"这四个字上：一旦允许预算之外的项跟着变，Δ 就同时是
预算效应、裁判效应、样本量效应，读者却会把它读成"预算影响"。所以：
- `plan` 自检必须**能拒绝**（非预算项、零变化、读不到基线）；
- `analyze` 遇到越界差异必须拒绝出结论，而不是给个"仅供参考"的数。
"""

import json
from argparse import Namespace
from pathlib import Path

import budget_ab


BASE = {
    "model": "m1",
    "model_version": "v1",
    "base_url": "https://example.test/v1",
    "dataset": "cases/core_suite.jsonl",
    "dataset_generation": "deff7501007cff61",
    "seed": 42,
    "temperature": 0.1,
    "judge_model": "j1",
    "judge_base_url": "https://example.test/v1",
    "judge_samples": 2,
    "case_time_budget": 480,
    "jcv_threshold": 0.6,
    "collateral_risk": "high",
    "repeat": 2,
    "token_budget": 8000,
    "max_tokens": 1024,
    "max_rounds": 4,
}


def _run(tmp_path: Path, name: str, config: dict, results: dict) -> Path:
    """results: {case_id: (final_bit, [attempt_bits], markers)}"""
    run = tmp_path / name
    run.mkdir(parents=True, exist_ok=True)
    (run / "config.json").write_text(json.dumps(config), encoding="utf-8")
    lines = []
    for case_id, (final, attempts, markers) in results.items():
        row = {
            "result": {
                "case_id": case_id,
                "strict_passed": final,
                "attempts": [
                    {"attempt": index + 1, "strict_passed": bit}
                    for index, bit in enumerate(attempts)
                ],
            }
        }
        row["result"].update(markers)
        lines.append(json.dumps(row, ensure_ascii=False))
    (run / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run


# --------------------------------------------------------------------------
# 唯一变量自检
# --------------------------------------------------------------------------


def test_only_budget_keys_may_differ():
    high = {**BASE, "token_budget": 24000, "max_tokens": 4096}
    budget_diff, outside = budget_ab.diff_config(BASE, high)
    assert budget_diff == ["token_budget", "max_tokens"]
    assert outside == []


def test_judge_samples_change_is_out_of_bounds():
    """裁判采样数变了就不是同一个测据：Δ 里混进裁判方差，必须判为越界。"""
    _, outside = budget_ab.diff_config(BASE, {**BASE, "judge_samples": 5})
    assert "judge_samples" in outside


def test_seed_and_repeat_changes_are_out_of_bounds():
    _, outside = budget_ab.diff_config(BASE, {**BASE, "seed": 7, "repeat": 3})
    assert set(outside) == {"seed", "repeat"}


def test_missing_key_counts_as_difference():
    """缺键按 None 比：宁可误报也不放过"一边根本没记这个配置"。"""
    broken = {key: value for key, value in BASE.items() if key != "judge_model"}
    _, outside = budget_ab.diff_config(BASE, broken)
    assert "judge_model" in outside


# --------------------------------------------------------------------------
# 机制标记
# --------------------------------------------------------------------------


def test_read_markers_separates_mechanism_from_capability(tmp_path):
    """截断/无效/环境故障三块必须能被读出来——它们解释了分数差，但不算能力差。"""
    run = _run(
        tmp_path, "r1", BASE,
        {
            "c1": (False, [False, False], {"output_truncated": True}),
            "c2": (False, [False, False], {"data_valid": False}),
            "c3": (None, [None, None], {"fail_reason": "precondition_failed"}),
            "c4": (True, [True, True], {}),
        },
    )
    markers = budget_ab.read_markers(run)
    assert markers["c1"]["truncated"] is True
    assert markers["c2"]["invalid"] is True
    assert markers["c3"]["env_failure"] is True
    assert markers["c4"] == {"truncated": False, "invalid": False, "env_failure": False}


def test_absent_results_file_yields_no_markers(tmp_path):
    assert budget_ab.read_markers(tmp_path / "nope") == {}


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def test_plan_rejects_non_budget_keys(tmp_path, capsys):
    run = _run(tmp_path, "r1", BASE, {"c1": (True, [True], {})})
    code = budget_ab.cmd_plan(
        Namespace(base_config=str(run), scale=3.0, keys="token_budget,temperature")
    )
    assert code == 1
    assert "非预算项不得改" in capsys.readouterr().out


def test_plan_rejects_zero_change(tmp_path, capsys):
    """一个预算项都没动就不是对照组：放行会让 analyze 拿噪声当效应。"""
    run = _run(tmp_path, "r1", BASE, {"c1": (True, [True], {})})
    code = budget_ab.cmd_plan(Namespace(base_config=str(run), scale=1.0, keys="token_budget"))
    assert code == 1
    assert "这不是对照组" in capsys.readouterr().out


def test_plan_default_keys_exclude_max_rounds(tmp_path, capsys):
    """放大步数上限会同时放宽行为空间，默认不动它；要动得显式指定并分开报。"""
    run = _run(tmp_path, "r1", BASE, {"c1": (True, [True], {})})
    code = budget_ab.cmd_plan(Namespace(base_config=str(run), scale=3.0,
                                        keys="token_budget,max_tokens"))
    out = capsys.readouterr().out
    assert code == 0
    # 逐行解析预算对照表：行形如 "* token_budget   8000 → 24000" / "  max_rounds    4 → 4"。
    table = [
        line.strip().split() for line in out.splitlines()
        if "→" in line and line.strip().startswith(("*", "max_rounds", "token_budget", "max_tokens"))
    ]
    changed = {parts[1] for parts in table if parts[0] == "*"}
    unchanged = {parts[0] for parts in table if parts[0] != "*"}
    assert changed == {"token_budget", "max_tokens"}
    assert "max_rounds" in unchanged  # 未变的那行也要看得见，不然“只动了两项”无从核对


def test_plan_rejects_unreadable_baseline(tmp_path, capsys):
    assert budget_ab.cmd_plan(
        Namespace(base_config=str(tmp_path / "nope"), scale=3.0, keys="token_budget")
    ) == 1
    assert "读不到基线配置" in capsys.readouterr().out


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------


def test_analyze_refuses_when_a_non_budget_key_differs(tmp_path, capsys):
    low = _run(tmp_path, "low", BASE, {"c1": (False, [False, False], {})})
    high = _run(
        tmp_path, "high", {**BASE, "temperature": 0.9},
        {"c1": (True, [True, True], {})},
    )
    code = budget_ab.cmd_analyze(
        Namespace(low_run=str(low), high_run=str(high), band_pp=1.0)
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "不可归因为预算" in out


def test_analyze_splits_delta_into_mechanism_and_net(tmp_path, capsys):
    low = _run(
        tmp_path, "low", BASE,
        {
            "c1": (False, [False, False], {"output_truncated": True}),
            "c2": (True, [True, True], {}),
            "c3": (None, [None, None], {"fail_reason": "Harness_Error"}),
        },
    )
    high = _run(
        tmp_path, "high", {**BASE, "token_budget": 24000},
        {
            "c1": (True, [True, True], {}),
            "c2": (True, [True, True], {}),
            "c3": (None, [None, None], {"fail_reason": "Harness_Error"}),
        },
    )
    code = budget_ab.cmd_analyze(
        Namespace(low_run=str(low), high_run=str(high), band_pp=1.0)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "分数差的三块拆解" in out
    assert "输出截断率" in out
    assert "环境故障归因率" in out
    assert "低档失败→高档通过（预算松绑直接受益）: 1 条" in out
    assert "反向" in out


def test_analyze_keeps_within_band_wording(tmp_path, capsys):
    """带内一律"差异未证实"：与裁判/劣化口径同一措辞红线，不许写"无影响"。"""
    bits = {f"c{i}": (i % 2 == 0, [i % 2 == 0] * 2, {}) for i in range(20)}
    low = _run(tmp_path, "low", BASE, bits)
    high = _run(tmp_path, "high", {**BASE, "token_budget": 24000}, bits)
    budget_ab.cmd_analyze(Namespace(low_run=str(low), high_run=str(high), band_pp=30.0))
    out = capsys.readouterr().out
    assert "差异未证实" in out
    assert "预算档位是本轮分数的解释变量之一" not in out


def test_analyze_declines_to_judge_without_any_band(tmp_path, capsys):
    low = _run(tmp_path, "low", BASE, {"c1": (False, [False], {}), "c2": (True, [True], {})})
    high = _run(
        tmp_path, "high", {**BASE, "token_budget": 24000},
        {"c1": (True, [True], {}), "c2": (True, [True], {})},
    )
    code = budget_ab.cmd_analyze(
        Namespace(low_run=str(low), high_run=str(high), band_pp=None)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "只报数，不作判定" in out


def test_analyze_requires_common_cases(tmp_path, capsys):
    low = _run(tmp_path, "low", BASE, {"c1": (True, [True], {})})
    high = _run(tmp_path, "high", {**BASE, "token_budget": 24000}, {"c9": (True, [True], {})})
    code = budget_ab.cmd_analyze(
        Namespace(low_run=str(low), high_run=str(high), band_pp=1.0)
    )
    assert code == 1
    assert "没有共同题集" in capsys.readouterr().out
