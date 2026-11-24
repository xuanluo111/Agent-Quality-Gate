"""噪声带的实测标定（T32 / M2 实验半）：把「代理值」换成「同配置重复跑出来的真抖动」。

背景：T14 落地的 `dataset.noise_band_pp` 是**代理值**——由两组 Wilson 区间半宽取均值
推出来，它衡量的是"通过率这个估计量本身的抽样误差"，不是"同一套题同一份配置重跑一次
会漂多少 pp"。两者是不同的东西：后者还包含服务端非确定性、裁判采样、工具时序等误差源，
通常更大。文档里当时就明写了这条边界，并把实测排到 T32。

本模块做实测，并把**可引用性**当作和数据同等重要的输出：
- 抖动只在**同一配置**下可比。`token_budget` 变了、裁判采样数变了、模型换了，都不是
  同一个噪声源，混池算出来的带宽度没有意义 → 所以先按配置指纹分组，只组内配对。
- 早期读取器只读 `result.attempts[]`，曾把顶层 trace 里的 seed 漏掉，进而误判历史批
  全部未落盘 seed。统一读取口按 attempt 编号合并两层后，是否变序以实际 seed 证据为准；
  确实没有 seed 的批次仍只能给**保守下界**，不能宣布"已标定"。
- 期数太少（可比配对 < 3）时结论统计上无力，必须显式标 `low_power`，不能让一个差值
  冒充标准差。
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.engine.dataset import noise_band_pp, wilson_interval
from src.engine.runload import bit_of, case_attempts_from_run, read_run_config, seed_of

Z_TWO_SIDED = 1.96
MIN_PAIRS_FOR_SD = 3
# 参与汇总的最小题数：样本太小的自检批（5~18 条）偶然 0 翻转会把下界压成 0，
# 那不是“噪声小”，而是“没测到”。阈值取 30：低于这个题数时翻转率本身的估计已很粗。
MIN_N_FOR_SUMMARY = 30
# 配置里会改变抖动来源的项。刻意不含 limit/workers/run_id/output_root：
# 它们不改变单条用例的判定路径，抽掉才能让同一批的不同切片合池。
_CONFIG_FINGERPRINT_KEYS = (
    "model",
    "model_version",
    "base_url",
    "dataset",
    "dataset_generation",
    "seed",
    "temperature",
    "max_rounds",
    "token_budget",
    "max_tokens",
    "case_time_budget",
    "judge_model",
    "judge_samples",
    "repeat",
    "jcv_threshold",
    "collateral_risk",
)


def config_fingerprint(config: dict) -> str:
    """把配置里影响抖动的项压成一个可分组的短指纹。"""
    return "|".join(f"{key}={config.get(key)}" for key in _CONFIG_FINGERPRINT_KEYS)


def bits_matrix(attempts_by_case: Dict[str, Sequence[dict]]) -> Dict[str, List[Optional[bool]]]:
    """原始尝试记录 → 结论位矩阵。抖动与变序自证必须来自同一份记录，所以只留这一个入口。"""
    return {
        case_id: [bit_of(item) for item in records]
        for case_id, records in attempts_by_case.items()
    }


def attempt_columns(
    attempts_by_case: Dict[str, Sequence[dict]],
) -> List[List[Optional[bool]]]:
    """把 {case: [每次尝试]} 转成按期排列的矩阵：columns[k][i] = 第 k 期第 i 条题的结论。

    只对各期**都有该题**的 case 建列，并截到公共期数：拿不同题集的两期算差，
    差值里混的是"题不一样"而不是抖动。
    """
    if not attempts_by_case:
        return []
    lengths = {len(records) for records in attempts_by_case.values() if len(records) > 1}
    if not lengths:
        return []
    n = min(lengths)
    columns: List[List[Optional[bool]]] = [[] for _ in range(n)]
    for case_id in sorted(attempts_by_case):
        records = list(attempts_by_case[case_id])[:n]
        if len(records) < n:
            continue
        for index, item in enumerate(records):
            columns[index].append(bit_of(item))
    return columns


def _usuable_rate(bits: Sequence[Optional[bool]]) -> Optional[float]:
    """通过率（百分点）。None 位剔出分母：环境故障不是模型失败（AGENTS.md 纪律 3）。"""
    usable = [bit for bit in bits if bit is not None]
    if not usable:
        return None
    return 100.0 * sum(1 for bit in usable if bit) / len(usable)


def seed_evidence(attempts_by_case: Dict[str, Sequence[dict]]) -> dict:
    """核对这批重复采样究竟是不是变序（K08）。

    判据不是"我们跑过 repeat"，而是每次尝试落盘的 seed 是否出现了多个值——
    与 `stability.attempt_divergence` 的 `seed_unique` 同一口径，两处不能各说一套。
    """
    seeds: set = set()
    key_present = False
    for records in attempts_by_case.values():
        for item in records:
            if "seed" in item:
                key_present = True
            value = seed_of(item)
            if value is not None:
                seeds.add(value)
    return {
        "seed_field_present": key_present,
        "seed_unique": len(seeds),
        "variation_known": bool(key_present and len(seeds) > 1),
    }


def pairwise_abs_diffs(columns: Sequence[Sequence[Optional[bool]]]) -> List[float]:
    """各期两两之间的通过率绝对差（百分点）。"""
    diffs: List[float] = []
    rates = [_usuable_rate(column) for column in columns]
    for i in range(len(rates)):
        for j in range(i + 1, len(rates)):
            if rates[i] is None or rates[j] is None:
                continue
            diffs.append(abs(rates[i] - rates[j]))
    return diffs


def _flaky_stats(bits_by_case: Dict[str, Sequence[Optional[bool]]]) -> Tuple[int, int]:
    """(至少翻转一次的题数, 参与统计的题数)。"""
    total = flaky = 0
    for bits in bits_by_case.values():
        usable = [bit for bit in bits if bit is not None]
        if len(usable) < 2:
            continue
        total += 1
        if len(set(usable)) > 1:
            flaky += 1
    return flaky, total


def case_flip_counts(bits_by_case: Dict[str, Sequence[Optional[bool]]]) -> Dict[str, int]:
    """逐条题的翻转配对数。用于定位不稳定题（题库代谢 C13/C19 的直接输入）。"""
    out: Dict[str, int] = {}
    for case_id, bits in bits_by_case.items():
        usable = [bit for bit in bits if bit is not None]
        if len(usable) < 2:
            continue
        flips = 0
        for i in range(len(usable)):
            for j in range(i + 1, len(usable)):
                if usable[i] != usable[j]:
                    flips += 1
        if flips:
            out[case_id] = flips
    return out


def flip_band_pp(flips: int, n: int, z: float = Z_TWO_SIDED) -> dict:
    """从**实测翻转率**推「同一批题、同一配置重跑两次，通过率会差多少 pp」的噪声带。

    为什么需要它：经验配对法要求 ≥ 2 个可比配对，而单场 `repeat=2` 永远只有 1 个配对；
    跨场凑配对在本仓历史数据上不成立（24 个同配置组各自只有 1 场，配置彼此不同）。
    翻转率法只用单场数据，不需要跨场配对。

    推导（带两条可证伪的假设，所以必须写明）：
    记每条题两期的结论差 X_i ∈ {-1, 0, +1}，两期通过率差 D = (1/n)·Σ X_i。
    在两期同分布且题间独立的假设下 E[X_i]=0、E[X_i²]=q（翻转率），
    故 Var(D) = q/n、σ_D = √(q/n)，带宽取 95% 双侧 z·σ_D（换算成百分点再乘 100）。

    与 Wilson 代理值的区别是本函数存在的意义：代理值度量「换一个样本估计量会抖多少」
    （抽样误差），这里度量「同一批题重跑一次会差多少」（运行抖动）。两个数不能互换。
    """
    if n <= 0:
        return {"flips": flips, "n": 0, "available": False, "reason": "无可比题集"}
    q = flips / n
    sigma_pp = 100.0 * math.sqrt(q / n)
    # `wilson_interval` 返回的是**百分点**（0~100），这里要的是比例的置信区间才能开方除以 n。
    # 直接当比例用会把带宽算大一个量级（实测错值 50~74pp），这类单位错在纸面上看不出来。
    lo_q, hi_q = (value / 100.0 for value in wilson_interval(flips, n))
    return {
        "flips": flips,
        "n": n,
        "flips_basis": "first_two_periods",
        "flip_rate": q,
        "sigma_diff_pp": sigma_pp,
        "band_pp": z * sigma_pp,
        "band_pp_ci": [z * 100.0 * math.sqrt(lo_q / n), z * 100.0 * math.sqrt(hi_q / n)],
        "quotable_for_summary": n >= MIN_N_FOR_SUMMARY,
        "assumptions": ["两期同分布", "题间独立"],
        "available": True,
    }


def calibrate_group(
    attempts_by_case: Dict[str, Sequence[dict]],
    config: Optional[dict] = None,
    z: float = Z_TWO_SIDED,
) -> dict:
    """一个同配置组内的实测噪声带标定。"""
    bits_by_case = bits_matrix(attempts_by_case)
    columns = attempt_columns(attempts_by_case)
    diffs = pairwise_abs_diffs(columns)
    known_rates = [r for r in (_usuable_rate(column) for column in columns) if r is not None]
    flaky, total = _flaky_stats(bits_by_case)

    result: dict = {
        "periods": len(columns),
        "cases_with_repeat": total,
        "flaky_cases": flaky,
        "flaky_rate_pct": (100.0 * flaky / total) if total else None,
        "period_pass_rates_pp": known_rates,
        "pair_count": len(diffs),
        "mean_abs_diff_pp": statistics.fmean(diffs) if diffs else None,
        "max_abs_diff_pp": max(diffs) if diffs else None,
        "unstable_cases": dict(
            sorted(case_flip_counts(bits_by_case).items(), key=lambda kv: -kv[1])[:10]
        ),
    }

    if len(diffs) >= 2:
        sd = statistics.stdev(diffs)
        result["sd_of_abs_diff_pp"] = sd
        # 带宽度直接取「两期之差」的经验分布：它度量的是"同一份配置重跑两次会得到
        # 多差的两个数"，比从单期 σ 再乘 sqrt(2) 少一层独立性假设。
        result["empirical_band_pp"] = z * sd
        result["empirical_band_1sigma_pp"] = sd
    else:
        result["sd_of_abs_diff_pp"] = None
        result["empirical_band_pp"] = None

    result["low_power"] = len(diffs) < MIN_PAIRS_FOR_SD
    if len(diffs) < 2:
        result["unavailable_reason"] = "可比配对不足 2 个，经验离散度算不出，改用翻转率法"
    elif result["low_power"]:
        result["unavailable_reason"] = (
            f"可比配对只有 {len(diffs)} 个（<{MIN_PAIRS_FOR_SD}），离散度不足以定带"
        )
    else:
        result["unavailable_reason"] = ""

    # 翻转率带：单场就能出数，是历史数据上唯一可用的实测口径。
    # 分母只算“两期都有可用结论位”的题：把两期都是环境故障的题放进分母会拉低翻转率，
    # 等于把“没测到”当成“很稳定”，带宽会往窄的错误方向偏。
    if len(columns) >= 2:
        pairs = [
            (first, second)
            for first, second in zip(columns[0], columns[1])
            if first is not None and second is not None
        ]
        cases_per_period = len(pairs)
        flips_total = sum(1 for first, second in pairs if first != second)
    else:
        cases_per_period = 0
        flips_total = 0
    result["empirical_flip_band"] = flip_band_pp(flips_total, cases_per_period, z)

    # 变序是否可证：决定这份实测能不能被引用为"已标定"。
    variation = seed_evidence(attempts_by_case)
    result["seed_evidence"] = variation
    if variation["variation_known"]:
        result["band_status"] = "measured"
        result["lower_bound"] = False
    elif total:
        # seed 未落盘 → 无法证明变序生效 → 只能按保守下界处理。
        result["band_status"] = "measured_lower_bound"
        result["lower_bound"] = True
        result["bound_note"] = (
            "该组无法证实变序采样生效（attempts 未落盘 seed）。同种子重复的方差小于变序后的"
            "方差，故此带宽度是噪声带的保守下界，不得引用为已标定值。"
        )
    else:
        result["band_status"] = "no_repeat_data"
        result["lower_bound"] = True

    if config and len(known_rates) >= 2 and columns:
        # 与代理值并排：让人看得见"实测比代理宽多少"，而不是只换一个数。
        n_cases = len(columns[0])
        successes = [int(round(rate * n_cases / 100.0)) for rate in known_rates[:2]]
        result["surrogate_band_pp"] = noise_band_pp(
            successes[0], n_cases, successes[1], n_cases
        )
        result["cases_per_period"] = n_cases
    return result


def calibrate_runs(run_dirs: Iterable[Path | str]) -> dict:
    """把若干 run 按配置指纹分组，逐组标定，并给出跨组汇总。

    分组即拒绝混池：不同预算/不同裁判采样下的抖动不是同一个量，把它们平均成一个
    "噪声带"是统计上的伪工作。同配置的多场可以合并期数（期数越多配对越强），
    但要求各场题集与每场期数一致——不一致就整场剔除并如实计数。
    """
    groups: Dict[str, dict] = {}
    skipped: Dict[str, int] = {"repeat_lt_2": 0, "no_results": 0, "shape_mismatch": 0}
    for run_dir in run_dirs:
        path = Path(run_dir)
        config = read_run_config(path)
        if not config:
            continue
        if (config.get("repeat") or 1) < 2:
            skipped["repeat_lt_2"] += 1
            continue
        attempts, audit = case_attempts_from_run(path)
        if not attempts:
            skipped["no_results"] += 1
            continue
        key = config_fingerprint(config)
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "config": config,
                "attempts": {case_id: list(records) for case_id, records in attempts.items()},
                "runs": [str(path)],
                "periods_per_run": {len(records) for records in attempts.values()},
            }
            continue
        run_periods = {len(records) for records in attempts.values()}
        same_shape = run_periods == group["periods_per_run"] and set(attempts) == set(
            group["attempts"]
        )
        if not same_shape:
            # 题集或期数对不上：并进去会让"第 k 期"在不同场指代不同的东西。
            skipped["shape_mismatch"] += 1
            continue
        for case_id, records in attempts.items():
            groups[key]["attempts"][case_id].extend(records)
        groups[key]["runs"].append(str(path))

    detail = {key: calibrate_group(item["attempts"], item["config"]) for key, item in groups.items()}
    for key, item in detail.items():
        item["runs_in_group"] = len(groups[key]["runs"])
    quotable = [
        item for item in detail.values()
        if item.get("band_status") == "measured" and not item.get("low_power")
    ]
    # 两档实测口径：经验配对带（假设最少）与翻转率带（单场可算）。
    empirical = [
        item["empirical_band_pp"] for item in detail.values()
        if item.get("empirical_band_pp") is not None and not item.get("low_power")
    ]
    flip_bounds = [
        item["empirical_flip_band"]["band_pp"]
        for item in detail.values()
        if item.get("empirical_flip_band", {}).get("quotable_for_summary")
    ]
    if quotable:
        summary_band = statistics.fmean(item["empirical_band_pp"] for item in quotable)
        summary_status = "measured"
    elif empirical:
        # 配对够了但 seed 未落盘：依旧只能是下界，不能越过变序证据宣布已标定。
        summary_band = min(empirical)
        summary_status = "lower_bound_only"
    elif flip_bounds:
        # 取最小值：下界只能说“至少这么大”，给均值或最大值都是把话说满。
        summary_band = min(flip_bounds)
        summary_status = "lower_bound_only"
    else:
        summary_band = None
        summary_status = "no_data"
    if quotable or empirical:
        summary_method = "paired_empirical"
    elif flip_bounds:
        summary_method = "flip_rate"
    else:
        summary_method = "none"
    return {
        "groups": len(detail),
        "quotable_groups": len(quotable),
        "skipped": skipped,
        "detail": detail,
        "summary_band_pp": summary_band,
        "summary_band_median_pp": statistics.median(flip_bounds) if flip_bounds else None,
        "summary_group_count": len(flip_bounds),
        "summary_min_n": MIN_N_FOR_SUMMARY,
        "summary_status": summary_status,
        "summary_method": summary_method,
        "flip_groups": len(flip_bounds),
    }
