#!/usr/bin/env python3
"""K13 case lint 命令行入口：用例四问齐备性检查（做什么／背景／不能做什么／怎样算成功）。

纯离线、零 API、只读数据集。规则实现与判据口径在 `src/engine/case_lint.py`。

用法：
    python scripts/lint_cases.py                    # 默认查 core_suite，人也看得懂的表格
    python scripts/lint_cases.py --json             # 机器可读（接 CI 门禁/看板）
    python scripts/lint_cases.py --strict           # warning 也当失败（CI 暂不启用）
    python scripts/lint_cases.py --path cases/smoke.jsonl
    python scripts/lint_cases.py --no-exemptions    # 忽略豁免清单，看全量原始问题

退出码：
    0  通过（error 为 0；默认 warning 不拦）
    1  error 存在，或 --strict 下存在任何**豁免外**的 finding
豁免清单见 `docs/quality/case-lint-exemptions.json`——改 case 数据会动 dataset_sha256
触发切代，所以存量缺陷只登记不改写。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.engine.case_lint import (  # noqa: E402  (需先插入 ROOT 才能导入)
    load_exemptions,
    lint_summary,
    render_summary,
)

DEFAULT_CASES = ROOT / "cases" / "core_suite.jsonl"
DEFAULT_EXEMPTIONS = ROOT / "docs" / "quality" / "case-lint-exemptions.json"


def _load(path: Path) -> list:
    """逐行读 JSONL。不经过 pydantic：lint 要能检查字段缺失本身，不能被校验提前拦下。"""
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _findings_payload(findings) -> list:
    return [
        {
            "case_id": finding.case_id,
            "rule": finding.rule,
            "level": finding.level,
            "message": finding.message,
        }
        for finding in findings
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="K13 case lint：用例四问齐备性检查")
    parser.add_argument("--path", default=str(DEFAULT_CASES), help="用例 JSONL 路径")
    parser.add_argument("--exemptions", default=str(DEFAULT_EXEMPTIONS), help="豁免清单路径")
    parser.add_argument("--no-exemptions", action="store_true", help="忽略豁免清单")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--strict", action="store_true", help="warning 也视为失败（CI 默认关闭，warning 起步）"
    )
    args = parser.parse_args()

    cases = _load(Path(args.path))
    exemptions = {} if args.no_exemptions else load_exemptions(Path(args.exemptions))
    summary = lint_summary(cases, exemptions)

    if args.json:
        print(
            json.dumps(
                {
                    "path": str(args.path),
                    "total": summary["total"],
                    "errors": summary["errors"],
                    "warnings": summary["warnings"],
                    "by_rule": summary["by_rule"],
                    "severity_strength": summary["severity_strength"],
                    "findings": _findings_payload(summary["findings"]),
                    "exempted": _findings_payload(summary["exempted"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"case lint: {args.path}")
        print(render_summary(summary, exemptions))

    if summary["errors"]:
        return 1
    if args.strict and (summary["errors"] or summary["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
