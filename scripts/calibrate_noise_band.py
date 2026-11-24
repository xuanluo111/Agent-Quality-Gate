#!/usr/bin/env python3
"""T32 噪声带实测标定：从历史 `repeat>1` 批次里把「重跑抖动」量出来（纯本地、零 API、零写入）。

为什么不用新跑一批：本仓已有 24 个同配置 `repeat=2` 组（core_suite 全量为主），
逐次结论位都落在 `results.jsonl` 的 `result.attempts[]` 里。把它们读出来就能标定，
再跑一遍只是把钱花在已经有的信息上。

诚实边界（脚本自己会说，不允许只挑好看的数）：
1. 是否变序以统一读取口合并后的 seed 证据为准；确实没有 seed 的批次只能给噪声带的
   **保守下界**，不能宣布已标定值；
2. 抖动带（重跑同一批题会差多少）与 T14 的 Wilson 代理带（换一个样本估计量会差多少）
   回答的不是同一个问题，脚本两者并列输出并标明各自适用面；
3. 样本量 < 30 条的自检批不参与汇总：偶然 0 翻转会把下界压成 0，那是"没测到"不是"噪声小"。

用法：
    python scripts/calibrate_noise_band.py                 # 扫 reports/runs 与归档
    python scripts/calibrate_noise_band.py --json out.json  # 落盘（默认不写任何文件）
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.engine.noise_calibration import MIN_N_FOR_SUMMARY, calibrate_runs

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOBS = ("reports/runs/*", ".reports-archive-*/runs/*")


def collect_run_dirs(extra: list[str]) -> list[str]:
    """收集待扫描的 run 目录。只返真目录（含 results.jsonl），其余一律丢弃。

    传目录时同时接受两种形态：“这一片目录下的各 run”与“它本身就是一个 run”。
    只展开子项会让 `--runs-dir reports/runs/<某个批>` 这种最自然的用法拿到空集，
    而空集在下游会被当成“没有可标定的数据”而不是“路径给错了”——两者必须可区分。
    """
    found: list[str] = []
    for pattern in list(DEFAULT_GLOBS) + list(extra or []):
        target = pattern if Path(pattern).is_absolute() else str(ROOT / pattern)
        if any(ch in target for ch in "*?["):
            found.extend(glob.glob(target))
        else:
            found.extend(glob.glob(str(Path(target) / "*")))
            found.append(target)
    return sorted({item for item in found if (Path(item) / "results.jsonl").is_file()})


def label_of(fingerprint: str) -> str:
    """把配置指纹缩成一行能读懂的标识（model + 数据集 + 预算）。"""
    fields = dict(part.split("=", 1) for part in fingerprint.split("|") if "=" in part)
    return (
        f"{fields.get('model', '?')} / {Path(fields.get('dataset', '?')).name} / "
        f"budget={fields.get('token_budget')}/{fields.get('max_tokens')} / "
        f"judge={fields.get('judge_samples')}×{fields.get('judge_model', '?')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-dir", action="append", default=[], help="额外扫描目录（可重复）")
    parser.add_argument("--json", dest="json_out", default="", help="把完整结果写到指定 JSON（默认不写）")
    parser.add_argument("--top", type=int, default=12, help="明细展示条数")
    args = parser.parse_args()

    run_dirs = collect_run_dirs(args.runs_dir)
    print(f"扫描 run 目录: {len(run_dirs)} 个（只读，不调任何 API）")
    result = calibrate_runs(run_dirs)
    detail = result["detail"]

    quotable = [item for item in detail.values() if item.get("band_status") == "measured"]
    flip_groups = [
        item for item in detail.values() if item["empirical_flip_band"].get("quotable_for_summary")
    ]
    print(
        f"\n同配置分组: {result['groups']} | 可宣布已标定: {result['quotable_groups']} | "
        f"参与汇总(n≥{MIN_N_FOR_SUMMARY}): {len(flip_groups)}"
    )
    print(f"剔除: {result['skipped']}")

    if result["summary_band_pp"] is None:
        print("\n没有任何可用样本，不给结论。")
        return 1

    print(
        f"\n=== 实测噪声带（重跑抖动口径）===\n"
        f"  下界(取最小组带宽): {result['summary_band_pp']:.2f} pp\n"
        f"  中位: {result['summary_band_median_pp']:.2f} pp\n"
        f"  状态: {result['summary_status']} / 方法: {result['summary_method']}"
    )
    bands = [item["empirical_flip_band"]["band_pp"] for item in flip_groups]
    surrogates = [item["surrogate_band_pp"] for item in flip_groups if item.get("surrogate_band_pp")]
    print(f"  跨度: {min(bands):.2f} ~ {max(bands):.2f} pp")
    if surrogates:
        narrower = sum(
            1 for item in flip_groups
            if item.get("surrogate_band_pp")
            and item["empirical_flip_band"]["band_pp"] < item["surrogate_band_pp"]
        )
        print(
            f"\n=== 与 T14 Wilson 代理带对照 ===\n"
            f"  代理带跨度: {min(surrogates):.2f} ~ {max(surrogates):.2f} pp\n"
            f"  实测窄于代理: {narrower}/{len(surrogates)} 组"
        )
        print(
            "  口径纪律: 两者答的不是同一个问题——代理带=换一个题集的抽样误差（两模型横比用），\n"
            "            实测带=同一题集重跑一次的运行抖动（跨窗口 ΔJ 用）。\n"
            "            现有 A/B/C 分档判据**不替换**：替换会让门禁对横比结论更宽松，\n"
            "            而 AGENTS.md 纪律 1 只允许新增判据更严或持平。"
        )

    print("\n=== 明细（按实测带宽升序）===")
    ordered = sorted(flip_groups, key=lambda item: item["empirical_flip_band"]["band_pp"])
    for item in ordered[: max(1, args.top)]:
        band = item["empirical_flip_band"]
        seed = item["seed_evidence"]
        print(
            f"  {label_of(next(key for key, value in detail.items() if value is item)):<58} "
            f"n={band['n']:>3} 翻转={band['flips']:>3} 带={band['band_pp']:>5.2f}pp "
            f"CI[{band['band_pp_ci'][0]:.2f},{band['band_pp_ci'][1]:.2f}] "
            f"代理={item.get('surrogate_band_pp', 0):.2f} "
            f"变序可证={'是' if seed['variation_known'] else '否'}"
        )

    if not quotable:
        print(
            "\n⚠ 全部组的 attempts 都未落盘 seed（K08 变序之前的批次）。上面这组数只能引用为\n"
            "  「噪声带保守下界」；要拿到可宣布的标定值，需要一次带变序采样的 repeat 批（须授权）。"
        )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果已写: {args.json_out}")
    else:
        print("（未写任何文件；需要落盘加 --json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
