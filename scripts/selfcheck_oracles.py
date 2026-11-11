#!/usr/bin/env python3
"""C01 判据自检 runner：读 suite → 逐条离线自检 → 落 json + md 报告。

不调用任何 LLM（LayeredJudge 不传 client），零网络零费用。只报告、不修改用例：
DEAD_CASE／LEAKY 的修复是人的决定，改动走正常切代评审。

用法：
    python scripts/selfcheck_oracles.py                          # 默认 core_suite 全量
    python scripts/selfcheck_oracles.py --input cases/smoke.jsonl --limit 20
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.engine.dataset import load_cases  # noqa: E402
from src.engine.judge import LayeredJudge  # noqa: E402
from src.engine.oracle import selfcheck_suite  # noqa: E402

DEFAULT_INPUT = "cases/core_suite.jsonl"
DEFAULT_JSON = "reports/oracle_selfcheck.json"
DEFAULT_MD = "reports/oracle_selfcheck.md"

_VERDICT_ORDER = {"DEAD_CASE": 0, "LEAKY": 1, "UNDETERMINED": 2, "OK": 3}


def _render_md(summary: dict, generated_at: str, source: str) -> str:
    lines = [
        "# C01 判据自检报告",
        "",
        f"- 生成时间：{generated_at}",
        f"- 数据源：`{source}`（{summary['total_cases']} 条）",
        "- 口径：正面样本必过（否则 DEAD_CASE）× 反面变异必被抓（否则 LEAKY）；"
        "裁决落到 llm/local_fallback 层的记 NOT_DETERMINISTIC，只计覆盖率不计入两类失效。",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 用例总数 | {summary['total_cases']} |",
        f"| OK | {summary['ok']} |",
        f"| DEAD_CASE | {len(summary['dead_cases'])} |",
        f"| LEAKY | {len(summary['leaky_cases'])} |",
        f"| UNDETERMINED | {len(summary['undetermined_cases'])} |",
        f"| 变异总数（适用分母） | {summary['mutation_total']} |",
        f"| 变异被捕获 | {summary['mutation_caught']} |",
        f"| 变异漏网 | {summary['mutation_leaky']} |",
        f"| 确定性捕获率 | {summary['capture_rate'] if summary['capture_rate'] is not None else 'n/a'} |",
        "",
    ]

    if summary["dead_cases"]:
        lines += ["## DEAD_CASE 清单（正面样本过不了，压分不携带信息）", ""]
        lines += [f"- `{cid}`" for cid in summary["dead_cases"]]
        lines.append("")
    if summary["leaky_cases"]:
        lines += ["## LEAKY 清单（破坏断言仍得满分）", ""]
        lines += [f"- `{cid}`" for cid in summary["leaky_cases"]]
        lines.append("")
    if summary["undetermined_cases"]:
        lines += ["## UNDETERMINED 清单（离线不可判，进 C04 可判率口径）", ""]
        lines += [f"- `{cid}`" for cid in summary["undetermined_cases"]]
        lines.append("")

    # 按 category 聚合，找系统性短板
    by_cat: dict = {}
    for r in summary["results"]:
        cat = by_cat.setdefault(
            r["category"], {"total": 0, "ok": 0, "dead": 0, "leaky": 0, "muts": 0, "caught": 0}
        )
        cat["total"] += 1
        if r["verdict"] == "OK":
            cat["ok"] += 1
        elif r["verdict"] == "DEAD_CASE":
            cat["dead"] += 1
        elif r["verdict"] == "LEAKY":
            cat["leaky"] += 1
        cat["muts"] += r["mutations_applied"]
        cat["caught"] += r["mutations_caught"]
    lines += [
        "## 按 category 聚合",
        "",
        "| category | 用例 | OK | DEAD | LEAKY | 变异捕获/适用 |",
        "|---|---|---|---|---|---|",
    ]
    for cat in sorted(by_cat):
        c = by_cat[cat]
        ratio = f"{c['caught']}/{c['muts']}" if c["muts"] else "n/a"
        lines.append(f"| {cat} | {c['total']} | {c['ok']} | {c['dead']} | {c['leaky']} | {ratio} |")
    lines.append("")

    # 漏网变异 top（按 mut_id 聚合，暴露哪类断言闸口最松）
    mut_leak: dict = {}
    for r in summary["results"]:
        for row in r["rows"]:
            if row["verdict"] == "LEAKY":
                key = row["mut_id"].split(":")[0]
                mut_leak[key] = mut_leak.get(key, 0) + 1
    if mut_leak:
        lines += ["## 漏网变异组 TOP（哪类闸口最松）", "", "| 变异组 | 漏网次数 |", "|---|---|"]
        for k, v in sorted(mut_leak.items(), key=lambda x: -x[1]):
            lines.append(f"| `{k}` | {v} |")
        lines.append("")

    lines.append(
        "> 机器可读全量（用例 × 变异组 → 裁决 + 裁判 reason 原文）：`reports/oracle_selfcheck.json`"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="C01 判据自检（离线、零 API）")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--json-output", default=DEFAULT_JSON)
    parser.add_argument("--md-output", default=DEFAULT_MD)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全量）")
    parser.add_argument(
        "--only",
        default="",
        help="逗号分隔的 case_id 片段，只跑命中的用例（调试用）",
    )
    args = parser.parse_args()

    cases, load_report = load_cases(args.input, dedupe=True)
    if load_report.get("validation_errors"):
        print(f"[warn] {len(load_report['validation_errors'])} 条用例校验失败，已跳过", file=sys.stderr)
    if args.only:
        frags = [f.strip() for f in args.only.split(",") if f.strip()]
        cases = [c for c in cases if any(f in c.id for f in frags)]
    if args.limit > 0:
        cases = cases[: args.limit]
    print(f"[info] selfcheck {len(cases)} cases from {args.input} (offline, no LLM)")

    judge = LayeredJudge()  # 不传 client：llm 层自动走 local_fallback，被记为 NOT_DETERMINISTIC
    summary = selfcheck_suite(judge, cases)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    payload = {"generated_at": generated_at, "source": args.input, **summary}
    json_path = Path(args.json_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = Path(args.md_output)
    md_path.write_text(_render_md(summary, generated_at, args.input), encoding="utf-8")

    print(
        f"[result] total={summary['total_cases']} ok={summary['ok']} "
        f"dead={len(summary['dead_cases'])} leaky={len(summary['leaky_cases'])} "
        f"undetermined={len(summary['undetermined_cases'])} "
        f"capture_rate={summary['capture_rate']}"
    )
    print(f"[report] {json_path} / {md_path}")
    # 自检不因失效而失败退出：报告本身就是产物，是否放行由批前收口人裁决
    return 0


if __name__ == "__main__":
    sys.exit(main())
