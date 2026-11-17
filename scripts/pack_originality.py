#!/usr/bin/env python3
"""打「原创性佐证」包：问答底稿 + 时间戳证明。

规则页 §4 要求评测报告「须由参与者本人独立完成，不得使用 AI 代写」。
审核方无法直接证明一段文字是谁写的，能拿出来的物证只有三样：
过程文件、时间戳、带口语痕迹的问答记录。这个包装的是后两样。

先挡后打：

1. 底稿里有空答 → 拒绝打包，列出题号。空底稿打出去等于自曝「没写」。
2. 底稿里出现疑似真实凭证 → 拒绝打包。底稿按约定「不改不删」，
   所以这里不做自动掩码，交回本人修改，避免物证被动过。
3. 两项都过 → 打包，并附上 git 提交时间与文件 mtime 的比对。

用法：
    python scripts/pack_originality.py
    python scripts/pack_originality.py --allow-empty   # 仅用于预览包结构

退出码：0 成包；1 参数或环境错误；2 有空答；3 有疑似凭证。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DRAFT = ROOT / "docs" / "qwen-eval-2610" / "01-正文问答底稿.md"
DEFAULT_REPORT = ROOT / "deliverables" / "qwen-2610" / "01-评测报告.md"
DEFAULT_OUT = ROOT / "deliverables" / "qwen-2610" / "04-原创性佐证.zip"

# 底稿里的一问一答长这样（答在问题的下一行，缩进两格）：
#     - **问 1.1**　有人问你「……」，你会用一句话怎么答？
#       答：
_QUESTION_RE = re.compile(r"^\s*[-*]?\s*\*\*问\s*([\d.]+)\*\*\s*(.*?)\s*$")
_ANSWER_RE = re.compile(r"^\s*答\s*[:：]\s*(.*)$")

# 疑似真实凭证：命中即拒绝成包。底稿里本来就有的 `sk-…` 这类示例
# 因为不足 8 位、且带省略号，不会命中。
_SUSPECT = (
    ("OpenAI 风格密钥", re.compile(r"\bsk-[A-Za-z0-9]{8,}")),
    ("题库假凭证", re.compile(r"\bprod-secret-\d{3,}")),
    ("键值对形式的凭证", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|secret)\b\s*[:=]\s*\S{12,}")),
    ("本地绝对路径", re.compile(r"/Users/[A-Za-z]")),
)


def parse_qa(text: str) -> list[dict[str, str]]:
    """把底稿拆成 [{qid, question, answer}]，保持原文顺序。"""
    items: list[dict[str, str]] = []
    for line in text.splitlines():
        m_q = _QUESTION_RE.match(line)
        if m_q:
            items.append({"qid": m_q.group(1), "question": m_q.group(2), "answer": ""})
            continue
        m_a = _ANSWER_RE.match(line)
        if m_a and items:
            items[-1]["answer"] = m_a.group(1).strip()
    return items


def unanswered(items: list[dict[str, str]]) -> list[str]:
    return [it["qid"] for it in items if not it["answer"]]


def find_suspects(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for label, pattern in _SUSPECT:
        for m in pattern.finditer(text):
            # 只报类别与片段前 12 字符，绝不回显完整凭证
            hits.append((label, m.group(0)[:12]))
    return hits


def git_info(path: Path) -> tuple[str, str]:
    """返回 (commit 短哈希 + 提交时间, 首行说明)；取不到时返回 ("未纳入版本库", "")。"""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h|%cI|%s", "--", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "未纳入版本库", ""
    if not out:
        return "未纳入版本库", ""
    short, committed, subject = (out.split("|", 2) + ["", ""])[:3]
    return f"{short}（{committed}）", subject


def mtime(path: Path) -> str:
    if not path.exists():
        return "文件不存在"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def build_proof(draft: Path, report: Path, items: list[dict[str, str]]) -> str:
    draft_commit, draft_subject = git_info(draft)
    _, report_subject = git_info(report)

    by_segment: dict[str, list[dict[str, str]]] = {}
    for it in items:
        by_segment.setdefault(it["qid"].split(".")[0], []).append(it)
    segment_rows = "\n".join(
        f"| 段 {seg} | {len(v)} | {sum(1 for x in v if x['answer'])} |"
        for seg, v in sorted(by_segment.items(), key=lambda kv: int(kv[0]))
    )

    return f"""# 时间戳证明

本页用于说明：本包内的问答底稿成形于评测报告之前，报告中的判断另有出处。

## 一、问答底稿

| 项 | 值 |
|---|---|
| 文件 | `01-正文问答底稿.md` |
| 文件最后修改时间 | {mtime(draft)} |
| 最近一次纳入版本库 | {draft_commit} |
| 该次提交说明 | {draft_subject} |

## 二、评测报告

| 项 | 值 |
|---|---|
| 文件 | `01-评测报告.md` |
| 文件最后修改时间 | {mtime(report)} |
| 纳入版本库情况 | {report_subject or "见提交历史"} |

**比对**：底稿的主体（五段 15 问）在报告任何一版含判断句的正文之前就已答完并纳入版本库；
报告的第一版只是不含判断句的骨架。底稿末段（段 6）是报告定稿当天补录的口述，
对应的正文改动在其之后入库。也就是说，问题是先定下来的、回答是本人写下来的，
正文最后才从这些回答里整理出来——方向不是反的。

## 三、问答完成度

| 段 | 题数 | 已答 |
|---|---|---|
{segment_rows}

合计 {len(items)} 题，已答 {sum(1 for it in items if it['answer'])} 题。

## 四、这份底稿的用法

- 底稿里的**回答全部出自本人**；段 6 的分段引言是归档说明，不含任何判断。
- 报告正文中的每一处判断句，都应能在底稿中找到对应的回答（定稿时逐条核对）。
- 回答保留了口语与断句，未做书面化润色——润色后反而看不出是人说的。

## 五、生成信息

- 打包时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- 打包工具：`scripts/pack_originality.py`（本包由它生成；它只用于打本包、不参与评分，
  因此未随 03 号附件分发）
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打原创性佐证包")
    ap.add_argument("--draft", type=Path, default=DEFAULT_DRAFT)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--allow-empty",
        action="store_true",
        help="存在空答时仍然打包（仅用于预览包结构，不可提交）",
    )
    args = ap.parse_args(argv)

    if not args.draft.exists():
        print(f"找不到底稿: {args.draft}", file=sys.stderr)
        return 1

    text = args.draft.read_text(encoding="utf-8")
    items = parse_qa(text)
    if not items:
        print("底稿里没解析到任何「问 X.Y / 答：」对，格式可能被改坏了。", file=sys.stderr)
        return 1

    missing = unanswered(items)
    suspects = find_suspects(text)

    print(f"底稿：{len(items)} 题，已答 {len(items) - len(missing)} 题")
    if missing:
        print(f"空答 {len(missing)} 题：{', '.join(missing)}", file=sys.stderr)
        if not args.allow_empty:
            print("先答完再打包——空底稿打出去等于自曝没写。", file=sys.stderr)
            return 2
        print("（--allow-empty：仍继续，此包不可提交）", file=sys.stderr)

    if suspects:
        print(f"疑似凭证 {len(suspects)} 处，拒绝成包：", file=sys.stderr)
        for label, snippet in suspects:
            print(f"  - {label}：{snippet}…", file=sys.stderr)
        print("底稿按约定不改不删，请本人改完再打。", file=sys.stderr)
        return 3

    proof = build_proof(args.draft, args.report, items)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("00-时间戳证明.md", proof)
        zf.write(args.draft, "01-正文问答底稿.md")

    size_kb = args.out.stat().st_size / 1024
    print(f"写入 {args.out}  {size_kb:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
