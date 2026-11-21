#!/usr/bin/env python3
"""T22：把批次门禁摘要渲染成 PR 评论正文（RM-06「CI PR 评论贴 summary 摘要＋阻断原因」）。

设计边界（与 06 T22 的验收口径一致）：
- **零 token、不调模型**：数据源只有随仓库分发的 `reports/batch-*/summary.json`
  （白名单件，见 08 待拍板 #2）——评论里的每个数字外部可复核。
- **渲染与投递分离**：本脚本只产出 Markdown；贴到 PR 由 CI 里的 `gh pr comment`
  完成（GitHub Actions 自带 token，无需本机登录）。这样单测不需要网络与 gh。
- 报告正文（`*.report.md`／`.log`／observability）**刻意不进评论**：含原始回答与
  裁判依据引用，不随仓库分发；评论只给摘要表＋逐场阻断原因。

用法：
    python scripts/pr_comment.py                        # 自动取最新批次，打到 stdout
    python scripts/pr_comment.py --out comment.md       # 写文件（CI 用）
    python scripts/pr_comment.py --summary path/to/summary.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STATUS_ICON = {"passed": "✅", "warn": "⚠️", "blocked": "⛔", "invalid": "❓"}
# run_batch 的 --batch-name 允许非时间戳后缀（如 batch-20261121-0147），所以不能
# 用严格日期格式；但干跑副产物 batch-dryrun-* 不反映门禁事实，必须排除。
BATCH_DIR_RE = re.compile(r"^batch-(?!dryrun-)\d")


def find_latest_summary(reports_dir: Path) -> Path | None:
    """取 `reports/batch-*/summary.json` 中批号最大（＝时间戳最新）的一份。

    批号以时间戳开头（字典序即时间序）；`batch-dryrun-*` 之类非正式批会被
    BATCH_DIR_RE 剔除（glob `batch-*` 会误匹配，不能只靠它）。
    """
    candidates = sorted(
        p for p in reports_dir.glob("batch-*/summary.json")
        if BATCH_DIR_RE.match(p.parent.name)
    )
    return candidates[-1] if candidates else None


def _fmt_rate(v) -> str:
    return "—" if v is None else f"{v:.2f}%"


def build_comment(summary: dict) -> str:
    """summary.json（dict）→ PR 评论 Markdown。缺字段一律降级为「—」，不抛异常。"""
    batch_id = summary.get("batch_id", "unknown")
    rows = summary.get("rows") or []
    lines: list[str] = []

    lines.append(f"### AQG 门禁摘要｜批次 `{batch_id}`")
    lines.append("")
    if not rows:
        lines.append("本批次没有任何 run 记录。")
        return "\n".join(lines) + "\n"

    epoch = {(r.get("metrics") or {}).get("dataset_epoch") for r in rows if r.get("metrics")}
    if len(epoch) == 1:
        lines.append(f"数据集代际 `{epoch.pop()}`｜共 {len(rows)} 场")
    else:
        lines.append(f"共 {len(rows)} 场（数据集代际不一致，逐场见表）")
    lines.append("")
    lines.append("| 模型 | 门禁 | Strict | Critical | High | 有效样本率 |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        m = r.get("metrics") or {}
        g = m.get("gate") or {}
        status = g.get("status") or r.get("status") or "unknown"
        icon = STATUS_ICON.get(status, "")
        lines.append(
            f"| {r.get('name', '?')} | {icon} `{status}` "
            f"| {_fmt_rate(m.get('strict_rate'))} | {m.get('critical', '—')} "
            f"| {m.get('high', '—')} | {_fmt_rate(m.get('valid_rate'))} |"
        )
    lines.append("")

    blocked = [
        (r.get("name", "?"), (r.get("metrics") or {}).get("gate", {}).get("reasons") or [])
        for r in rows
        if ((r.get("metrics") or {}).get("gate") or {}).get("status") not in (None, "passed")
    ]
    blocked = [(n, rs) for n, rs in blocked if rs]
    if blocked:
        lines.append("<details><summary>逐场阻断/降级原因（原文摘自当场产物）</summary>")
        lines.append("")
        for name, reasons in blocked:
            lines.append(f"- **{name}**")
            for reason in reasons:
                lines.append(f"  - {reason}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append(
        "> 本评论由 AQG CI 自动生成：数字只取自随仓库分发的 "
        "`reports/batch-*/summary.json`（可外部复核）；逐条证据在 `reports/runs/`，不随仓库分发。"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", type=Path, default=None,
                        help="summary.json 路径；缺省自动取 reports/ 下最新批次")
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"),
                        help="自动查找时的根目录（默认 reports/）")
    parser.add_argument("--out", type=Path, default=None,
                        help="把评论写到文件；缺省打 stdout")
    args = parser.parse_args(argv)

    path = args.summary
    if path is None:
        path = find_latest_summary(args.reports_dir)
        if path is None:
            print(f"错误：{args.reports_dir} 下没有 batch-*/summary.json（批次白名单件未入库？）",
                  file=sys.stderr)
            return 3
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"错误：读取 {path} 失败：{exc}", file=sys.stderr)
        return 1

    comment = build_comment(summary)
    if args.out:
        args.out.write_text(comment, encoding="utf-8")
        print(f"评论已写入 {args.out}（数据源 {path.name}，{len(summary.get('rows') or [])} 场）")
    else:
        sys.stdout.write(comment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
