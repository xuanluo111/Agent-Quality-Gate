"""噪声带实测标定（T32 / M2）的判据测试。

这个模块最容易被做错的三件事，都有对应反例测试：
1. **混池**：不同配置（预算、裁判采样、模型）的抖动不是同一个量，平均成一个"噪声带"
   是统计上的伪工作 → 分组指纹必须抓得住预算变化、忽略得住无关项；
2. **单位**：`wilson_interval` 返回百分点而不是比例，直接当比例用会把带宽算大一个量级
   （纸面上看不出来，只在数字上错得离谱）；
3. **可引用性**：K08 变序落地前的 repeat 批是同种子重复，方差偏小，算出来的只能当下界；
   期数/配对不足时必须显式标出来，不能让一个差值冒充标准差。
"""

import json
import math
from pathlib import Path

import pytest

from src.engine.noise_calibration import (
    MIN_N_FOR_SUMMARY,
    MIN_PAIRS_FOR_SD,
    Z_TWO_SIDED,
    attempt_columns,
    calibrate_group,
    calibrate_runs,
    case_flip_counts,
    config_fingerprint,
    flip_band_pp,
    pairwise_abs_diffs,
    seed_evidence,
)

BASE_CONFIG = {
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
    "repeat": 3,
    "jcv_threshold": 0.6,
    "collateral_risk": "high",
}


def _attempts(bits_by_case):
    """{case: [结论位]} → 引擎内部吃的原始尝试记录形态（带 attempt 序号）。"""
    return {
        case_id: [
            {"attempt": index + 1, "strict_passed": bit}
            for index, bit in enumerate(bits)
        ]
        for case_id, bits in bits_by_case.items()
    }


# --------------------------------------------------------------------------
# 配置指纹：分组即拒绝混池
# --------------------------------------------------------------------------


def test_budget_change_splits_the_group():
    """预算是抖动的来源之一（截断→重判），换预算必须换一组。"""
    a = dict(BASE_CONFIG)
    b = {**BASE_CONFIG, "token_budget": 32000}
    assert config_fingerprint(a) != config_fingerprint(b)


def test_judge_samples_change_splits_the_group():
    b = {**BASE_CONFIG, "judge_samples": 5}
    assert config_fingerprint(BASE_CONFIG) != config_fingerprint(b)


def test_irrelevant_keys_do_not_split_the_group():
    """limit/workers/run_id 不改变单条题的判定路径；把它们算进指纹会让同批切片无法合池。"""
    b = {**BASE_CONFIG, "limit": 10, "workers": 1, "run_id": "20261120-010101-m1",
         "output_root": "reports/runs-archive"}
    assert config_fingerprint(BASE_CONFIG) == config_fingerprint(b)


# --------------------------------------------------------------------------
# 翻转率带的单位回归
# --------------------------------------------------------------------------


def test_flip_band_ci_is_same_order_of_magnitude_as_band():
    """单位回归：CI 与点估计必须同量级。

    曾经把 wilson_interval 返回的百分点当比例用，得到 50~74pp 的 CI（错 100 倍）。
    这个断言就是那条错误的照妖镜——它不看具体值，只看量级关系。
    """
    band = flip_band_pp(flips=40, n=200)
    assert band["available"] is True
    lo, hi = band["band_pp_ci"]
    assert lo <= band["band_pp"] <= hi
    # 点估计 = z·√(q/n)·100；q=0.2, n=200 → 1.96·0.0316·100 ≈ 6.2pp
    assert band["band_pp"] == pytest.approx(Z_TWO_SIDED * math.sqrt(0.2 / 200) * 100)
    assert hi < band["band_pp"] * 3, "CI 上界被算大一个量级说明百分点/比例又混了"


def test_zero_flips_still_yields_a_band_of_zero_but_flagged_small_sample():
    """偶然 0 翻转 ≠ 噪声为 0：样本不足时必须标记为不可引用。"""
    band = flip_band_pp(flips=0, n=6)
    assert band["band_pp"] == 0.0
    assert band["quotable_for_summary"] is False
    big = flip_band_pp(flips=0, n=MIN_N_FOR_SUMMARY)
    assert big["quotable_for_summary"] is True


def test_flip_band_needs_no_data_is_unavailable():
    assert flip_band_pp(0, 0)["available"] is False


def test_flip_band_states_its_falsifiable_assumptions():
    """带宽是推导出来的，假设必须随数落盘，否则读者无法证伪。"""
    assert flip_band_pp(10, 100)["assumptions"]


# --------------------------------------------------------------------------
# 期矩阵与配对
# --------------------------------------------------------------------------


def test_attempt_columns_drops_cases_missing_in_later_periods():
    """各期题集不同就不能算差值：差值里混的是"题不一样"而不是抖动。"""
    records = _attempts({"a": [True, True], "b": [True]})
    columns = attempt_columns(records)
    assert len(columns) == 2
    assert len(columns[0]) == 1  # 只有 a 两期都有


def test_pairwise_diffs_ignore_periods_with_no_usable_bits():
    columns = [[None, None], [None, None]]
    assert pairwise_abs_diffs(columns) == []


def test_none_bits_leave_the_denominator_of_period_rate():
    """环境故障剔出分母（AGENTS.md 纪律 3）：全 None 的那一期不该产生一个 0% 的假分数。"""
    records = _attempts({"a": [True, None], "b": [True, None]})
    result = calibrate_group(records, BASE_CONFIG)
    assert result["period_pass_rates_pp"] == [100.0]  # 第二期全 None → 不产生配对


def test_case_flip_counts_only_lists_flipping_cases():
    counts = case_flip_counts({"a": [True, False], "b": [True, True], "c": [None, True]})
    assert counts == {"a": 1}


# --------------------------------------------------------------------------
# 变序证据决定可引用性
# --------------------------------------------------------------------------


def test_seed_absence_downgrades_to_lower_bound():
    """attempts 没落盘 seed → 无法证明变序生效 → 只能宣布"保守下界"。"""
    records = _attempts({f"c{i}": [True, i % 2 == 0] for i in range(6)})
    result = calibrate_group(records, BASE_CONFIG)
    assert result["seed_evidence"]["variation_known"] is False
    assert result["band_status"] == "measured_lower_bound"
    assert result["lower_bound"] is True
    assert "保守下界" in result["bound_note"]


def test_seed_variation_present_allows_measured():
    records = _attempts({f"c{i}": [True, i % 2 == 0] for i in range(6)})
    for bits in records.values():
        bits[0]["seed"] = 42
        bits[1]["seed"] = 8021
    evidence = seed_evidence(records)
    assert evidence["variation_known"] is True
    result = calibrate_group(records, BASE_CONFIG)
    assert result["band_status"] == "measured"
    assert result["lower_bound"] is False


def test_single_pair_period_is_marked_low_power():
    """一个差值不是标准差：配对不足必须标 low_power，不能让它去定带宽。"""
    records = _attempts({f"c{i}": [True, i % 2 == 0] for i in range(8)})
    result = calibrate_group(records, BASE_CONFIG)
    assert result["pair_count"] == 1
    assert result["low_power"] is True
    assert result["empirical_band_pp"] is None
    assert result["unavailable_reason"]


def test_surrogate_band_is_reported_alongside_not_replacing():
    """代理值与实测并排输出，是为了让人看见差多少，而不是顺手替换掉分档带宽。"""
    records = _attempts({f"c{i}": [i % 3 != 0, i % 2 == 0, i % 5 == 0] for i in range(12)})
    result = calibrate_group(records, BASE_CONFIG)
    assert "surrogate_band_pp" in result
    assert result["cases_per_period"] == 12


# --------------------------------------------------------------------------
# 跨场分组
# --------------------------------------------------------------------------


def _make_run(tmp_path: Path, name: str, bits_by_case: dict, config: dict) -> Path:
    run = tmp_path / name
    run.mkdir(parents=True)
    (run / "config.json").write_text(json.dumps(config), encoding="utf-8")
    lines = []
    for case_id, bits in bits_by_case.items():
        row = {
            "result": {
                "case_id": case_id,
                "strict_passed": all(bit is True for bit in bits),
                "attempts": [
                    {"attempt": index + 1, "strict_passed": bit}
                    for index, bit in enumerate(bits)
                ],
            }
        }
        lines.append(json.dumps(row, ensure_ascii=False))
    (run / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run


def test_repeat_lt_two_runs_are_skipped_not_counted_as_zero_noise(tmp_path):
    """单发批没有"重复"可言：当成 0 抖动会把带宽压到 0，那是把没测到说成噪声小。"""
    run = _make_run(tmp_path, "r1", {"a": [True], "b": [False]}, {**BASE_CONFIG, "repeat": 1})
    result = calibrate_runs([run])
    assert result["groups"] == 0
    assert result["skipped"]["repeat_lt_2"] == 1
    assert result["summary_status"] == "no_data"


def test_same_config_runs_merge_periods_instead_of_overwriting(tmp_path):
    """同配置两场必须合成 4 期，而不是后者覆盖前者只剩 1 个配对。"""
    bits = {f"c{i}": [True, i % 2 == 0] for i in range(40)}
    first = _make_run(tmp_path, "r1", bits, BASE_CONFIG)
    second = _make_run(tmp_path, "r2", bits, BASE_CONFIG)
    result = calibrate_runs([first, second])
    assert result["groups"] == 1
    only = next(iter(result["detail"].values()))
    assert only["periods"] == 4
    assert only["runs_in_group"] == 2
    assert only["pair_count"] >= MIN_PAIRS_FOR_SD


def test_shape_mismatch_run_is_excluded_and_counted(tmp_path):
    """题集不一致的第二场并进来会让"第 k 期"在不同场指代不同题 → 整场剔除并如实计数。"""
    bits = {f"c{i}": [True, i % 2 == 0] for i in range(40)}
    first = _make_run(tmp_path, "r1", bits, BASE_CONFIG)
    other = {f"d{i}": [True, False] for i in range(40)}
    second = _make_run(tmp_path, "r2", other, BASE_CONFIG)
    result = calibrate_runs([first, second])
    assert result["skipped"]["shape_mismatch"] == 1
    assert next(iter(result["detail"].values()))["runs_in_group"] == 1


def test_small_sample_groups_never_set_the_summary_floor(tmp_path):
    """6 条题的自检批偶然 0 翻转会得到 0.00pp 的下界；汇总不能被它压成 0。"""
    big = _make_run(
        tmp_path, "r1", {f"c{i}": [True, i % 7 == 0] for i in range(40)}, BASE_CONFIG
    )
    small_config = {**BASE_CONFIG, "model": "tiny"}
    small = _make_run(tmp_path, "r2", {f"a{i}": [True, True] for i in range(6)}, small_config)
    result = calibrate_runs([big, small])
    assert result["groups"] == 2
    assert result["summary_group_count"] == 1
    assert result["summary_band_pp"] and result["summary_band_pp"] > 0
    assert result["summary_band_median_pp"] is not None


def test_summary_band_takes_the_smallest_group_not_the_largest(tmp_path):
    """下界的含义是“至少这么大”：取最大值等于把噪声小的那组隐掉，把话说满。"""
    quiet = _make_run(
        tmp_path, "r1", {f"c{i}": [True, i % 40 != 0] for i in range(40)}, BASE_CONFIG
    )
    noisy_config = {**BASE_CONFIG, "model": "noisy"}
    noisy = _make_run(
        tmp_path, "r2", {f"c{i}": [True, i % 4 != 0] for i in range(40)}, noisy_config
    )
    result = calibrate_runs([quiet, noisy])
    bands = [
        item["empirical_flip_band"]["band_pp"]
        for item in result["detail"].values()
        if item["empirical_flip_band"]["quotable_for_summary"]
    ]
    assert len(bands) == 2 and bands[0] != bands[1]
    assert result["summary_band_pp"] == min(bands)
    assert result["summary_band_median_pp"] == pytest.approx(sum(bands) / 2)


def test_flip_rate_is_the_fallback_summary_method(tmp_path):
    """历史数据上配对凑不齐，汇总只能走翻转率法且定性为下界——这条是措辞红线。"""
    run = _make_run(tmp_path, "r1", {f"c{i}": [True, i % 9 == 0] for i in range(40)}, BASE_CONFIG)
    result = calibrate_runs([run])
    assert result["quotable_groups"] == 0
    assert result["summary_status"] == "lower_bound_only"
    assert result["summary_method"] == "flip_rate"
    assert result["summary_band_pp"] is not None
