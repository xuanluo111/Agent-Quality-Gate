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
    duplicate_id_report,
    load_cases,
    select_smoke,
    write_jsonl,
)


DEFAULT_INPUTS = ["cases/curated_cases.jsonl", "cases/regression.jsonl"]
# 新能力必须出现在冒烟集里，否则只能等全量评测才会暴露回归。
DEFAULT_MUST_INCLUDE = ["feature:state", "feature:provenance", "feature:correction"]


def dedupe_file(path: Path) -> dict:
    """按 id 保留首条、原地重写。

    保留的行逐字原样写回（不重新序列化），坏行也不碰，交给 `load_cases` 去报错；
    同 id 但内容不同的条目算“冲突”：保留首条会把另一套场景丢掉，必须列出来让人改 id。
    """
    kept: list[str] = []
    seen: dict[str, dict] = {}
    dropped = 0
    conflicts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        case_id = record.get("id")
        if case_id and case_id in seen:
            dropped += 1
            if seen[case_id] != record:
                conflicts.append(str(case_id))
            continue
        if case_id:
            seen[case_id] = record
        kept.append(line)
    if dropped:
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return {"path": str(path), "dropped": dropped, "conflicts": sorted(set(conflicts))}


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
    parser.add_argument(
        "--dedupe-inputs",
        action="store_true",
        help="先把输入文件按 id 去重（保留首条）再构建：重复条目只会让 --suite regression 白烧一遍钱",
    )
    args = parser.parse_args()

    missing = [path for path in args.inputs if not Path(path).is_file()]
    for path in missing:
        print(f"WARN: 输入不存在，已忽略: {path}")
    inputs = [path for path in args.inputs if Path(path).is_file()]
    if not inputs:
        print("没有可用的输入数据集")
        return 1

    if args.dedupe_inputs:
        for path in inputs:
            removed = dedupe_file(Path(path))
            print(f"去重 {removed['path']}: 丢弃 {removed['dropped']} 条重复")
            if removed["conflicts"]:
                preview = ", ".join(removed["conflicts"][:10])
                print(
                    f"WARN: {removed['path']} 有 {len(removed['conflicts'])} 个 id 内容不同"
                    f"（已保留首条，另一套场景被丢弃，建议改成新 id）: {preview}"
                )

    # 构建前先看一眼原始行：`load_cases` 自己会去重，不去重前的重复在这里才看得见。
    for path in inputs:
        report = duplicate_id_report(path)
        print(
            f"id 校验(输入) {path}: {report['rows']} 行 / {report['unique_ids']} 唯一 / 重复 {report['duplicates']} 条"
        )

    result = build_suite(inputs, args.output)
    input_duplicates = sum(report["duplicates"] for report in result["load_reports"])
    print(f"core: {result['written']} 条 -> {args.output}")
    print(
        f"去重: {result['duplicates_removed'] + input_duplicates} 条"
        f"（输入内 {input_duplicates}，跨输入 {result['duplicates_removed']}）"
    )
    print(f"排除未支持工具用例: {len(result['excluded_unsupported'])} 条")
    for report in result["load_reports"]:
        if report["duplicate_conflicts"]:
            preview = ", ".join(report["duplicate_conflicts"][:10])
            print(
                f"WARN: {report['path']} 有 {len(report['duplicate_conflicts'])} 个 id 内容不同"
                f"（保留首条）: {preview}"
            )

    smoke_audit = None
    if Path(args.smoke_source).is_file():
        smoke_cases, _ = load_cases(args.smoke_source, dedupe=True)
        smoke = select_smoke(smoke_cases, args.max_smoke, must_tags=args.must_include)
        write_jsonl(args.smoke, smoke)
        smoke_audit = audit_cases(smoke)
        print(f"smoke: {len(smoke)} 条 -> {args.smoke}")
    else:
        print(f"WARN: smoke 来源不存在，未生成 smoke: {args.smoke_source}")

    # 构建后必须无重复 id：差一条就是白烧一次真机调用，且统计分母跟着错。
    for label, path in (("core", args.output), ("smoke", args.smoke)):
        if not Path(path).is_file():
            continue
        report = duplicate_id_report(path)
        if report["duplicates"]:
            print(
                f"ERROR: {label} 仍有 {report['duplicates']} 条重复 id"
                f"（{report['rows']} 行 / {report['unique_ids']} 唯一）"
            )
            return 1
        print(f"id 校验 {label}: {report['rows']} 行 / {report['unique_ids']} 唯一 / 重复 0")

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
