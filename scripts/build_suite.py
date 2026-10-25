#!/usr/bin/env python3
"""构建可复现的 core / smoke 数据集，并输出审计清单。"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.engine.dataset import (
    audit_cases,
    build_suite,
    dataset_sha256,
    load_cases,
    select_smoke,
    write_jsonl,
)


DEFAULT_INPUTS = ["cases/curated_cases.jsonl", "cases/regression.jsonl"]
# 新能力必须出现在冒烟集里，否则只能等全量评测才会暴露回归。
DEFAULT_MUST_INCLUDE = ["feature:state", "feature:provenance", "feature:correction"]


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Agent-Quality-Gate 数据集")
    parser.add_argument("--inputs", nargs="+", default=DEFAULT_INPUTS)
    parser.add_argument("--output", default="cases/core_suite.jsonl")
    parser.add_argument("--smoke", default="cases/smoke.jsonl")
    parser.add_argument("--smoke-source", default="cases/curated_cases.jsonl")
    parser.add_argument("--max-smoke", type=int, default=18)
    parser.add_argument(
        "--must-include",
        nargs="*",
        default=DEFAULT_MUST_INCLUDE,
        help="这些 tag 的用例强制进 smoke 并排在最前（传空字符串取消）",
    )
    parser.add_argument("--manifest", default="cases/suite_manifest.json")
    args = parser.parse_args()

    missing = [path for path in args.inputs if not Path(path).is_file()]
    for path in missing:
        print(f"WARN: 输入不存在，已忽略: {path}")
    inputs = [path for path in args.inputs if Path(path).is_file()]
    if not inputs:
        print("没有可用的输入数据集")
        return 1

    result = build_suite(inputs, args.output)
    input_duplicates = sum(report["duplicates"] for report in result["load_reports"])
    print(f"core: {result['written']} 条 -> {args.output}")
    print(
        f"去重: {result['duplicates_removed'] + input_duplicates} 条"
        f"（输入内 {input_duplicates}，跨输入 {result['duplicates_removed']}）"
    )
    print(f"排除未支持工具用例: {len(result['excluded_unsupported'])} 条")

    smoke_audit = None
    if Path(args.smoke_source).is_file():
        smoke_cases, _ = load_cases(args.smoke_source, dedupe=True)
        smoke = select_smoke(smoke_cases, args.max_smoke, must_tags=args.must_include)
        write_jsonl(args.smoke, smoke)
        smoke_audit = audit_cases(smoke)
        print(f"smoke: {len(smoke)} 条 -> {args.smoke}")
    else:
        print(f"WARN: smoke 来源不存在，未生成 smoke: {args.smoke_source}")

    manifest = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "inputs": inputs,
        "missing_inputs": missing,
        "core": {
            "path": args.output,
            "written": result["written"],
            "duplicates_removed": result["duplicates_removed"],
            "input_duplicates": input_duplicates,
            "excluded_unsupported": result["excluded_unsupported"],
            "sha256": dataset_sha256(args.output),
            "audit": result["audit"],
            "load_reports": result["load_reports"],
        },
        "smoke": {
            "path": args.smoke,
            "written": smoke_audit["total"] if smoke_audit else 0,
            "sha256": dataset_sha256(args.smoke) if smoke_audit else "",
            "audit": smoke_audit,
        },
    }
    Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {args.manifest}")

    core_audit = result["audit"]
    for name, key in (("JCV", "expected_variables_cases"), ("HITL", "hitl_cases"), ("fixture", "fixture_cases")):
        print(f"{name}: {core_audit[key]}")
    print(f"敏感度: {core_audit['by_sensitivity']}")
    if core_audit["unsupported_tools"]:
        print(f"WARN: 存在未支持工具: {core_audit['unsupported_tools']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
