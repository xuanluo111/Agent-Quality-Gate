#!/usr/bin/env python3
"""规范化 JSONL 用例：字段归一化、去重、审计，不修改原始文件。"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.engine.dataset import audit_cases, dataset_sha256, load_cases, write_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(description="规范化并审计测试用例")
    parser.add_argument("--input", required=True, help="输入 JSONL")
    parser.add_argument("--output", help="输出 JSONL；不传则只审计")
    parser.add_argument("--no-dedupe", action="store_true", help="保留重复用例")
    parser.add_argument("--report", help="审计报告 JSON 路径")
    args = parser.parse_args()

    source = Path(args.input)
    if not source.is_file():
        print(f"输入不存在: {source}")
        return 1

    cases, load_report = load_cases(source, dedupe=not args.no_dedupe)
    audit = audit_cases(cases, load_report)
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(source),
        "source_sha256": dataset_sha256(source),
        "load": load_report,
        "audit": audit,
    }

    if args.output:
        written = write_jsonl(args.output, cases)
        report["output"] = args.output
        report["written"] = written
        report["output_sha256"] = dataset_sha256(args.output)
        print(f"已写出 {written} 条 -> {args.output}")

    print(f"原始记录: {load_report['raw_records']} | 可用: {load_report['loaded_cases']}")
    print(f"解析失败: {len(load_report['parse_errors'])} | 校验失败: {len(load_report['validation_errors'])}")
    print(f"重复: {load_report['duplicates']} | 缺少敏感度: {load_report['missing_sensitivity']}")
    print(f"类别: {audit['by_category']}")
    print(f"敏感度: {audit['by_sensitivity']}")
    print(f"JCV: {audit['expected_variables_cases']} | HITL: {audit['hitl_cases']} | fixture: {audit['fixture_cases']}")
    if audit["unsupported_tools"]:
        print(f"未支持工具: {audit['unsupported_tools']}")

    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"审计报告: {args.report}")

    has_errors = bool(load_report["parse_errors"] or load_report["validation_errors"])
    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
