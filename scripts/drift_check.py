"""跨批次漂移核验（C10 供应商一致性 + C9/C11 质量趋势）。

用途：拿一个基线批次和一个当前批次的 summary.json 做对照，回答三个问题——
1. 这两批到底能不能直接比？数据集换代（dataset_hash 变了）时比率不可同口径比较，
   必须先看代际，不能把换代当成“模型退步”。
2. 被测/裁判的供应商身份变没变？model_version 或 judge_model 变了就是 ISACA
   说的 Provider_Change：换了上游，分数波动得归因到供应商，而不是模型能力。
3. Strict / Standard 通过率的差异是不是统计显著？复用 dataset 里的两比例 z 检验，
   只有 p<显著性水平才叫“漂移”，避免把 1~2 条用例的抖动读成趋势。

本脚本完全离线：只读两份 JSON 摘要、做算术，不调模型、不联网，因此可单测。

用法：
    python scripts/drift_check.py --baseline reports/runs/<runA>/summary.json \
                                   --current  reports/runs/<runB>/summary.json
    python scripts/drift_check.py ... --alpha 0.05 --json   # 机器可读输出
退出码：0=可比较且无显著劣化；1=显著劣化，或供应商身份变化，或跨代不可比（需人工判读）。
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.engine.dataset import two_proportion_z_test, wilson_interval

# 判定“供应商身份”的字段：这些变了，分数差异要先归因到上游变更，而非模型能力。
PROVIDER_FIELDS = ("model", "model_version", "judge_model")
# 参与比率检验的口径 → (成功数字段, 分母字段)。分母用 total_cases，与 report 出 CI 一致。
RATE_METRICS = {"strict": ("strict_pass", "total_cases"), "standard": ("standard_pass", "total_cases")}


def _rate_field(summary: dict, metric: str, total_key: str) -> Tuple[int, int]:
    successes = int(summary.get(RATE_METRICS[metric][0], 0) or 0)
    total = int(summary.get(total_key, 0) or 0)
    return successes, total


def compare(baseline: dict, current: dict, alpha: float = 0.05) -> dict:
    """纯函数：给两份摘要，出结构化漂移报告（便于单测，不碰文件系统）。"""
    signals = []

    # 1) 可比较性：数据集换代 = 不可同口径直接比。
    base_hash, cur_hash = baseline.get("dataset_hash"), current.get("dataset_hash")
    comparable = bool(base_hash) and base_hash == cur_hash
    generation_breakpoint = bool(base_hash) and bool(cur_hash) and base_hash != cur_hash
    if generation_breakpoint:
        signals.append(
            f"数据集换代：{str(base_hash)[:12]}… → {str(cur_hash)[:12]}…，"
            "通过率不可直接比较，请先按换代流程判读"
        )

    # 2) 供应商身份变化（Provider_Change）。
    provider_changes = []
    for field in PROVIDER_FIELDS:
        base_val, cur_val = baseline.get(field, ""), current.get(field, "")
        if base_val and cur_val and base_val != cur_val:
            provider_changes.append({"field": field, "baseline": base_val, "current": cur_val})
            signals.append(f"供应商变更：{field} {base_val} → {cur_val}（分数波动须先归因上游）")

    # 3) 比率显著漂移。换代时仍算，但在报告里标注“仅供参考、跨代不可直接采信”。
    metrics = {}
    strict_regression = False
    for metric, (_succ_key, total_key) in RATE_METRICS.items():
        s_b, n_b = _rate_field(baseline, metric, total_key)
        s_c, n_c = _rate_field(current, metric, total_key)
        rate_b = round(100 * s_b / n_b, 2) if n_b else 0.0
        rate_c = round(100 * s_c / n_c, 2) if n_c else 0.0
        z, p = two_proportion_z_test(s_b, n_b, s_c, n_c)
        ci_b = wilson_interval(s_b, n_b)
        ci_c = wilson_interval(s_c, n_c)
        significant = p < alpha and comparable
        direction = "down" if rate_c < rate_b else "up" if rate_c > rate_b else "flat"
        metrics[metric] = {
            "baseline_rate": rate_b,
            "current_rate": rate_c,
            "delta": round(rate_c - rate_b, 2),
            "baseline_ci": [round(ci_b[0], 2), round(ci_b[1], 2)],
            "current_ci": [round(ci_c[0], 2), round(ci_c[1], 2)],
            "z": round(z, 3),
            "p": round(p, 4),
            "significant": significant,
            "direction": direction,
        }
        if significant and direction == "down":
            strict_regression = True
            signals.append(
                f"{metric} 显著劣化：{rate_b}% → {rate_c}%（Δ{rate_c - rate_b:.2f}pp，p={p:.4f}）"
            )
        elif significant and direction == "up":
            signals.append(
                f"{metric} 显著改善：{rate_b}% → {rate_c}%（Δ{rate_c - rate_b:+.2f}pp，p={p:.4f}）"
            )

    verdict = _verdict(comparable, generation_breakpoint, provider_changes, strict_regression, metrics)
    return {
        "comparable": comparable,
        "generation_breakpoint": generation_breakpoint,
        "provider_changes": provider_changes,
        "metrics": metrics,
        "alpha": alpha,
        "verdict": verdict,
        "signals": signals,
        "action_required": (
            strict_regression or bool(provider_changes) or generation_breakpoint
        ),
    }


def _verdict(comparable, breakpoint_, provider_changes, regression, metrics) -> str:
    if not comparable:
        return "跨代不可比" if breakpoint_ else "缺少 dataset_hash，无法确认可比性"
    if regression:
        return "显著劣化"
    if provider_changes:
        return "供应商变更"
    strict = metrics.get("strict", {})
    if strict.get("significant") and strict.get("direction") == "up":
        return "显著改善"
    return "统计稳定"


def load_summary(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="跨批次漂移核验（离线，复用 z 检验）")
    parser.add_argument("--baseline", required=True, help="基线批次 summary.json")
    parser.add_argument("--current", required=True, help="当前批次 summary.json")
    parser.add_argument("--alpha", type=float, default=0.05, help="显著性水平，默认 0.05")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    baseline = load_summary(args.baseline)
    current = load_summary(args.current)
    report = compare(baseline, current, alpha=args.alpha)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"漂移核验 | {baseline.get('run_id', '?')} → {current.get('run_id', '?')} | "
            f"结论：{report['verdict']}（α={args.alpha}）"
        )
        print(f"  可比较：{report['comparable']} | 换代：{report['generation_breakpoint']}")
        for metric, data in report["metrics"].items():
            print(
                f"  [{metric}] {data['baseline_rate']}% → {data['current_rate']}% "
                f"Δ{data['delta']:+.2f}pp z={data['z']} p={data['p']} "
                f"{'显著' if data['significant'] else '不显著'}"
            )
        for line in report["signals"]:
            print(f"  ! {line}")
        if not report["signals"]:
            print("  （无显著信号）")

    return 1 if report["action_required"] else 0


if __name__ == "__main__":
    sys.exit(main())
