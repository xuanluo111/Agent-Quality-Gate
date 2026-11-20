#!/usr/bin/env python3
"""裁判 token 烧速监控：读各 run 的 results.jsonl，累计 5.3 裁判 token 并外推总量。

纯本地：只读已落盘的 results.jsonl，不调任何 API、不碰正在跑的批、零成本。
用途：全量批跑期间随时看「已用/预计总/池余量」，在耗尽前预警，避免裁判兜底→invalid。

池口径：5.3-flash 尝鲜包 = 50,000,000 tokens（可在 --pool 覆盖）。
每条样本点的裁判 token 记在 result.token_cost（judge_samples 次调用之和）。

用法：
    .venv/bin/python scripts/judge_token_monitor.py                       # 自动找最新批
    .venv/bin/python scripts/judge_token_monitor.py --batch 20261118-131428
    .venv/bin/python scripts/judge_token_monitor.py --watch 300           # 每 5 分钟刷新
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "reports" / "runs"
DEFAULT_POOL = 50_000_000
# results.jsonl 一行 = 一个用例；repeat=2 折进同一行的 attempts[]，
# result.token_cost 已含该行全部裁判调用（repeat × judge_samples 次之和）。
# 实测：qwen run 收工时恰为 207 行且 report/summary 已落盘。
ROWS_PER_MODEL = 207


def case_token_cost(path: str) -> tuple[int, int]:
    """返回 (累计裁判 token, 已完成样本点数)。读不了/写一半就跳过坏行。"""
    total = 0
    n = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 批正在追加，末行可能写了一半
                total += int(rec.get("result", {}).get("token_cost", 0) or 0)
                n += 1
    except OSError:
        pass
    return total, n


def find_batch_runs(batch: str) -> list[tuple[str, str]]:
    """返回 [(model_name, results.jsonl 路径)]，按 run_id 前缀=批次 STAMP 过滤。"""
    out = []
    for d in sorted(RUNS.glob(f"{batch}-*")):
        rj = d / "results.jsonl"
        if rj.exists():
            # run 目录名形如 <STAMP>-<model-name>
            name = d.name[len(batch) + 1:]
            out.append((name, str(rj)))
    return out


def latest_batch() -> str:
    # 只认时间戳型 STAMP（8位数字开头），排除 attack-probe 等非批次目录；按 mtime 取最新
    stamps = [
        p for p in glob.glob(str(ROOT / "reports" / "batch-*"))
        if re.match(r"batch-\d{8}", os.path.basename(p))
    ]
    if not stamps:
        return ""
    newest = max(stamps, key=os.path.getmtime)
    return os.path.basename(newest).replace("batch-", "")


def render(batch: str, pool: int, expected_models: int) -> str:
    runs = find_batch_runs(batch)
    grand = 0
    lines = [f"批次 {batch}｜池 {pool:,}｜裁判 token 烧速", "=" * 60]
    for name, path in runs:
        t, n = case_token_cost(path)
        grand += t
        done = f"{n}/{ROWS_PER_MODEL}"
        per_pt = t / n if n else 0
        lines.append(f"  {name:34s} {done:>9s}  已用 {t:>10,}  每点 {per_pt:>6.0f}")
    completed_models = sum(1 for _, p in runs if case_token_cost(p)[1] >= ROWS_PER_MODEL)
    finished = len(runs)
    lines.append("-" * 60)
    lines.append(f"已完成模型：{completed_models}/{expected_models}｜在跑/已落盘 run：{finished}")
    lines.append(f"累计裁判 token：{grand:,}  占池 {grand / pool:.1%}")

    # 外推：用「已完成模型的真实单价」，未完成按均值估；一个都没完成则按当前每点估
    if completed_models >= 1:
        avg_per_model = grand / max(completed_models, 1)
        projected = avg_per_model * expected_models
        basis = f"按 {completed_models} 个完整模型均单价 {avg_per_model:,.0f}/模型"
    elif runs:
        # 用已观测每点均值 × 每模型点数 × 模型数（偏保守，因贵模型在前）
        all_pts = sum(case_token_cost(p)[1] for _, p in runs) or 1
        per_pt = grand / all_pts
        projected = per_pt * ROWS_PER_MODEL * expected_models
        basis = f"按每点均值 {per_pt:,.0f}（贵模型在前，实际多半更低）"
    else:
        projected = 0
        basis = "尚无数据"

    margin = pool - projected
    lines.append(f"预计总量：{projected:,.0f}（{basis}）")
    lines.append(f"预计剩余：{margin:+,.0f}  →  " + (
        "✅ 充裕" if margin > pool * 0.2 else
        "⚠️ 贴边，留意" if margin > 0 else
        "❌ 预计超池！尽快补包或降 judge_samples"
    ))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="裁判 token 烧速监控（纯本地读盘）")
    ap.add_argument("--batch", default="", help="批次 STAMP；空=自动取最新 batch-*")
    ap.add_argument("--pool", type=int, default=DEFAULT_POOL, help="池容量，默认 5000 万")
    ap.add_argument("--models", type=int, default=9, help="模型总数，默认 9")
    ap.add_argument("--watch", type=int, default=0, metavar="SEC", help=">0 则每隔 SEC 秒刷新")
    args = ap.parse_args()

    batch = args.batch or latest_batch()
    if not batch:
        print("没找到任何 batch-* 目录")
        return 1

    while True:
        print("\033c" if args.watch else "", end="")  # watch 模式清屏
        print(render(batch, args.pool, args.models))
        if not args.watch:
            break
        time.sleep(args.watch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
