#!/usr/bin/env python3
"""T32「仅增预算」对照组：把「分数变了」拆成「能力变了」与「脚手架松了」两种解释（纯本地、零 API）。

为什么必须有这一组：本仓的判定里，预算不足会把「没答完」直接记成不合规——
`docs/priority-2026q4/02` 的口径声明原文就写了「预算收紧会把'没答完'记成不合规」。
于是同一个模型在不同 `token_budget` 下可以名正言顺地拿到两个分数。跨窗口比较一旦
同时动了预算，ΔJ 里就混进了脚手架效应，读者会把它当成能力变化。

三条硬规矩：
1. `plan` 先做**唯一变量自检**：两档配置若除预算项之外还有任何一项不同，直接拒绝出计划。
   「只增预算」是一个可以被违反的承诺，不是一个形容词。
2. `analyze` 把总差拆成三块分别报：截断率差、前置失败率差、以及二者之外的净差。
   只有第三块才允许往「能力」上解释，前两块是机制性效果。
3. 反向迁移（低档通过→高档失败）逐条列出来，不允许只报净差把它混掉。

用法：
    python scripts/budget_ab.py plan    --base-config reports/runs/<run>
    python scripts/budget_ab.py analyze --low-run reports/runs/<低档>/ --high-run reports/runs/<高档>/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.engine.noise_calibration import flip_band_pp
from src.engine.runload import (
    case_bits_from_run,
    case_final_bits,
    case_id_of,
    iter_result_rows,
    read_run_config,
)

# 允许不同的项：只有预算三件套。刻意不含 repeat/judge_samples——它们会改变
# 样本量与裁判采样，那就不再是「仅增预算」。
BUDGET_KEYS = ("token_budget", "max_tokens", "max_rounds")
# 必须全等的项。缺键按 None 比，宁可误报也不放过。
MUST_MATCH_KEYS = (
    "model",
    "model_version",
    "base_url",
    "dataset",
    "dataset_generation",
    "seed",
    "temperature",
    "judge_model",
    "judge_base_url",
    "judge_samples",
    "case_time_budget",
    "jcv_threshold",
    "collateral_risk",
    "repeat",
)
# 环境故障归因：与 failure_class 同源，本脚本不重新定义哪些算环境故障。
_PRECONDITION_REASONS = frozenset({"precondition_failed", "Harness_Error", "Judge_Unavailable"})


def diff_config(low: dict, high: dict) -> tuple[list[str], list[str]]:
    """返回（预算项差异，越界差异）。越界差异非空即说明这已经不是「仅增预算」。"""
    budget_diff = [key for key in BUDGET_KEYS if low.get(key) != high.get(key)]
    outside = [key for key in MUST_MATCH_KEYS if low.get(key) != high.get(key)]
    return budget_diff, outside


def read_markers(run_dir: Path) -> dict[str, dict]:
    """逐条读「这题为什么没答好」的机制标记，用于把分数差拆成可归因的三块。

    只读已落盘字段（`output_truncated` / `data_valid` / `fail_reason`），不重算判定：
    重算就要动裁判，那是另一件事，也不该在对照分析里顺手做。
    """
    path = Path(run_dir) / "results.jsonl"
    markers: dict[str, dict] = {}
    if not path.is_file():
        return markers
    for record, bad in iter_result_rows(path):
        if bad:
            continue
        result = record.get("result") or {}
        case_id = case_id_of(record)
        if not case_id:
            continue
        markers[case_id] = {
            "truncated": bool(result.get("output_truncated")),
            "invalid": result.get("data_valid") is False,
            "env_failure": str(result.get("fail_reason") or "") in _PRECONDITION_REASONS,
        }
    return markers


def _load_base(path_text: str) -> dict:
    path = Path(path_text)
    return read_run_config(path.parent if path.name == "config.json" else path)


def cmd_plan(args: argparse.Namespace) -> int:
    base = _load_base(args.base_config)
    if not base:
        print("❌ 读不到基线配置（传 run 目录或 run/config.json）")
        return 1
    # 默认不动 max_rounds：放大步数上限会同时改变轨迹形态（多步、可能自环），
    # 那就不是“预算松绑”而是“行为空间放宽”，两种效应混在一个 Δ 里说不清。
    # 要一起对照就得显式指定 --keys max_rounds，并分开报两个 Δ。
    keys = [key for key in args.keys.split(",") if key]
    unknown = sorted(set(keys) - set(BUDGET_KEYS))
    if unknown:
        print(f"❌ 非预算项不得改: {unknown}（可选项: {', '.join(BUDGET_KEYS)}）")
        return 1
    high = dict(base)
    for key in keys:
        if base.get(key) is not None:
            high[key] = int(round(base[key] * args.scale))
    budget_diff, outside = diff_config(base, high)
    print(f"唯一变量自检: 预算项差异 {budget_diff or '无'} | 越界差异 {outside or '无'}")
    if outside:
        print("❌ 基线自身无法构成「仅增预算」对照：存在预算以外的差异项，拒绝出计划")
        return 1
    if not budget_diff:
        print("❌ 预算项一个都没变，这不是对照组")
        return 1
    print("\n两档配置对照（低档 = 基线，高档 = 仅增预算）:")
    for key in BUDGET_KEYS:
        marker = "*" if key in budget_diff else " "
        print(f"  {marker} {key:<14} {base.get(key)} → {high.get(key)}")
    print("\n跑批命令（本脚本不执行任何一条；真跑需明确授权，两档都会产生费用）:")
    for label, item in (("低档", base), ("高档", high)):
        print(
            f"  # {label}\n  python scripts/run_eval.py --dataset {item.get('dataset')} "
            f"--seed {item.get('seed')} --repeat {item.get('repeat')} "
            f"--token-budget {item.get('token_budget')} --max-tokens {item.get('max_tokens')} "
            f"--max-rounds {item.get('max_rounds')} --no-publish"
        )
    print("\n⚠ 两档必须同代际同题集同裁判采样；任一不同，结论就落回「脚手架差异」而不是预算差异。")
    return 0


def _rate(bits: list) -> tuple[int, int]:
    usable = [bit for bit in bits if bit is not None]
    return sum(1 for bit in usable if bit), len(usable)


def _marker_rate(markers: dict[str, dict], keys: list[str], field: str) -> float:
    if not keys:
        return 0.0
    return 100.0 * sum(1 for key in keys if markers.get(key, {}).get(field)) / len(keys)


def cmd_analyze(args: argparse.Namespace) -> int:
    low_cfg = read_run_config(Path(args.low_run))
    high_cfg = read_run_config(Path(args.high_run))
    budget_diff, outside = diff_config(low_cfg, high_cfg)
    if outside:
        print(f"❌ 两档存在预算之外的差异 {outside}：Δ 不可归因为预算，不予分析")
        return 1
    low_bits, _ = case_final_bits(Path(args.low_run))
    high_bits, _ = case_final_bits(Path(args.high_run))
    common = sorted(set(low_bits) & set(high_bits))
    if not common:
        print("❌ 两档没有共同题集，无法对照")
        return 1
    low_pass, low_n = _rate([low_bits[key] for key in common])
    high_pass, high_n = _rate([high_bits[key] for key in common])
    low_rate = 100.0 * low_pass / low_n
    high_rate = 100.0 * high_pass / high_n
    delta = high_rate - low_rate

    low_markers = read_markers(Path(args.low_run))
    high_markers = read_markers(Path(args.high_run))
    print(f"共同题集 {len(common)} 条 | 低档 {low_rate:.2f}% → 高档 {high_rate:.2f}% | Δ {delta:+.2f}pp")
    print(f"预算项差异: {budget_diff}")
    print("\n=== 分数差的三块拆解 ===")
    for field, label in (
        ("truncated", "输出截断率"),
        ("invalid", "样本无效率(data_valid=false)"),
        ("env_failure", "环境故障归因率"),
    ):
        low_value = _marker_rate(low_markers, common, field)
        high_value = _marker_rate(high_markers, common, field)
        print(f"  {label:<32} 低档 {low_value:6.2f}% → 高档 {high_value:6.2f}% (Δ {high_value - low_value:+.2f}pp)")

    freed = [key for key in common if low_bits[key] is False and high_bits[key] is True]
    broken = [key for key in common if low_bits[key] is True and high_bits[key] is False]
    print(f"\n低档失败→高档通过（预算松绑直接受益）: {len(freed)} 条")
    print(f"低档通过→高档失败（反向，须逐条解释）: {len(broken)} 条")
    for key in broken[:10]:
        print(f"    {key}")
    if len(broken) > 10:
        print(f"    ...（另 {len(broken) - 10} 条）")

    band = args.band_pp
    band_basis = "--band-pp"
    if band is None:
        attempts, _ = case_bits_from_run(Path(args.high_run))
        # 与 cross_window 同口径：两期都有可用结论位的题才进分母，
        # 否则环境故障会把翻转率顶低、把带宽往“很稳定”的方向推。
        measurable = [
            [bit for bit in bits[:2] if bit is not None] for bits in attempts.values()
        ]
        measurable = [pair for pair in measurable if len(pair) == 2]
        flips = sum(1 for pair in measurable if len(set(pair)) > 1)
        total = len(measurable)
        if total:
            band = flip_band_pp(flips, total)["band_pp"]
            band_basis = f"高档场实测翻转率(n={total})"
    if band is None:
        print("\n无噪声带可用（该场没有 repeat 数据，也没给 --band-pp）：只报数，不作判定。")
        return 0
    verdict = "超出噪声带：预算档位是本轮分数的解释变量之一" if abs(delta) > band else "差异未证实"
    print(f"\n噪声带 {band:.2f}pp（来源 {band_basis}）→ {verdict}")
    print(
        "解释边界: 即便超带，也只能说明「当前预算档位在卡分数」，不能反推「加大预算就能提能力」。\n"
        "截率与环境故障率的差属于机制性效果，不得计入能力增益。"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="T32 仅增预算对照组")
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan", help="生成两档命令并做唯一变量自检")
    p_plan.add_argument("--base-config", required=True, help="基线 run 目录或 config.json")
    p_plan.add_argument("--scale", type=float, default=3.0, help="预算放大倍数")
    p_plan.add_argument(
        "--keys", default="token_budget,max_tokens",
        help=f"只放大这些预算项（可选项: {', '.join(BUDGET_KEYS)}）；max_rounds 需显式列入",
    )
    p_plan.set_defaults(func=cmd_plan)
    p_analyze = sub.add_parser("analyze", help="分析两档结果")
    p_analyze.add_argument("--low-run", required=True)
    p_analyze.add_argument("--high-run", required=True)
    p_analyze.add_argument("--band-pp", type=float, default=None)
    p_analyze.set_defaults(func=cmd_analyze)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
