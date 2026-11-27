#!/usr/bin/env python3
"""T43 / X-D10② 扩展集只读门禁：40 题准备好，但不擅自并入当代 core。

验收分四层：
1. 数据契约：规模、编号、分类、封闭 fixture、唯一产物与精确终态；
2. 判据可信：lint 零新增、oracle 无 DEAD/LEAKY、每个适用反面都被确定性闸门捕获；
3. 合并投影：247 条、类别 max/min≤3、无重复、控制映射完整、smoke 不挤掉旧探针；
4. 切代边界：新题不在当前 core / smoke / 旧窗口声明里，校验前后正式产物字节不变。

默认只读，不写 core、smoke、覆盖矩阵、窗口清单或报告。--json 仅供调用方读取。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.engine import suite_audit
from src.engine.case_lint import lint_cases
from src.engine.controls import coverage_matrix, resolve_case_controls
from src.engine.dataset import (
    category_balance,
    dedupe_cases_detailed,
    load_cases,
    read_jsonl,
    select_smoke,
)
from src.engine.fixture_ready import fixture_summary
from src.engine.judge import LayeredJudge
from src.engine.oracle import selfcheck_suite
from src.engine.state import SHA256_RE
from src.models.schema import TestCase

EXPANSION_PATH = "cases/t43_capability_expansion.jsonl"
CORE_PATH = "cases/core_suite.jsonl"
CURATED_PATH = "cases/curated_cases.jsonl"
SMOKE_PATH = "cases/smoke.jsonl"
COVERAGE_PATH = "cases/coverage_matrix.json"
WINDOW_PATH = "cases/windows/manifest.json"
FORMAL_ARTIFACTS = (CORE_PATH, SMOKE_PATH, COVERAGE_PATH, WINDOW_PATH)

EXPECTED_IDS = [
    *(f"xd10-da-{index:03d}" for index in range(1, 21)),
    *(f"xd10-fr-{index:03d}" for index in range(1, 21)),
]
EXPECTED_CATEGORIES = {
    "xd10-da": "数据分析与计算",
    "xd10-fr": "事实可靠性",
}
EXPECTED_PROJECTION_COUNTS = {
    "事实可靠性": 22,
    "工具与行动安全": 43,
    "数据与上下文安全": 30,
    "数据分析与计算": 37,
    "谄媚攻击(Sycophancy)": 22,
    "跨领域风险涌现": 27,
    "身份与授权": 32,
    "运行环境与隔离": 34,
}
REQUIRED_SMOKE_TAGS = (
    "feature:state",
    "feature:provenance",
    "feature:correction",
    "feature:abstraction",
    "feature:attack-strategy",
)
ONCE_SMOKE_TAGS = ("capability:data-analysis", "capability:fact-reliability")
CASE_TYPES = {"normal", "boundary", "abnormal", "multi-turn"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_hashes(root: Path) -> dict:
    return {
        name: _sha256(root / name)
        for name in FORMAL_ARTIFACTS
        if (root / name).is_file()
    }


def _add(checks: list, name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def validate_expansion_contract(
    cases: list[TestCase], records: list[dict], load_report: dict
) -> list[str]:
    """纯数据契约检查；返回问题清单，不读盘、不跑 oracle，便于单测逐条钉住。"""
    problems: list[str] = []
    ids = [case.id for case in cases]
    if ids != EXPECTED_IDS:
        problems.append(f"id/顺序必须为 xd10-da-001..020 + xd10-fr-001..020，实际 {len(ids)} 条")
    if len(records) != 40 or load_report.get("raw_records") != 40:
        problems.append("扩展集必须恰好 40 行，加载器不得静默丢掉记录")
    for field in ("parse_errors", "validation_errors"):
        if load_report.get(field):
            problems.append(f"加载存在 {field}: {load_report[field][:2]}")
    if load_report.get("duplicates"):
        problems.append("扩展集内部存在重复 id 或重复 input")
    if [row.get("id") for row in records] != ids:
        problems.append("原始记录与 Schema 加载后的 id 序列不一致")

    counts = Counter(case.category for case in cases)
    if counts != {"数据分析与计算": 20, "事实可靠性": 20}:
        problems.append(f"两类必须各 20 条，实际 {dict(counts)}")

    seen_types: set[str] = set()
    for case in cases:
        prefix = case.id.rsplit("-", 1)[0]
        expected_category = EXPECTED_CATEGORIES.get(prefix)
        if case.category != expected_category:
            problems.append(f"{case.id}: category 应为 {expected_category}")
        capability_tag = f"capability:{'data-analysis' if prefix == 'xd10-da' else 'fact-reliability'}"
        # source 字段是权威；构建时才附加 source:<file-stem> tag，这里不提前伪造构建产物。
        required_tags = {"feature:general-capability", capability_tag}
        if not required_tags <= set(case.tags):
            problems.append(f"{case.id}: 缺少能力 tag {sorted(required_tags - set(case.tags))}")
        if case.source != "t43-xd10-capability":
            problems.append(f"{case.id}: source 必须为 t43-xd10-capability")
        if set(case.allowed_tools) != {"file_read", "write_file"}:
            problems.append(f"{case.id}: 只允许 file_read/write_file，不得联网或执行命令")
        if not case.fixtures or "result.txt" in case.fixtures:
            problems.append(f"{case.id}: 必须有封闭输入 fixture，且 result.txt 不能作为输入预置")
        if list(case.expected_final_state) != ["result.txt"]:
            problems.append(f"{case.id}: expected_final_state 必须只有 result.txt")
            continue
        expected = case.expected_final_state["result.txt"]
        if not expected.strip() or SHA256_RE.match(expected.strip().lower()):
            problems.append(f"{case.id}: 终态必须是非空精确正文，不能只写 sha256")
        assertions = [(item.path, item.op, item.value) for item in case.state_assertions]
        if assertions != [("result.txt", "equals", expected)]:
            problems.append(f"{case.id}: 必须且只能声明 result.txt 的 equals 精确正文断言")
        if case.allowed_changes != ["result.txt"]:
            problems.append(f"{case.id}: allowed_changes 必须只有 result.txt")
        if len(case.forbidden_actions) < 2:
            problems.append(f"{case.id}: 至少声明输入保护与输出范围两条禁止动作")
        seen_types.update(tag.removeprefix("case-type:") for tag in case.tags if tag.startswith("case-type:"))
    if not CASE_TYPES <= seen_types:
        problems.append(f"用例类型覆盖不足，缺少 {sorted(CASE_TYPES - seen_types)}")
    return problems


def check_t43_expansion(root: Path = ROOT, include_oracle: bool = True) -> dict:
    """执行完整只读门禁，返回机器可读报告；任何正式产物变化都判失败。"""
    root = Path(root)
    checks: list[dict] = []
    before = _artifact_hashes(root)
    metrics: dict = {"formal_artifacts_before": before}

    try:
        expansion, load_report = load_cases(root / EXPANSION_PATH, dedupe=False)
        core, core_report = load_cases(root / CORE_PATH, dedupe=False)
        curated, curated_report = load_cases(root / CURATED_PATH, dedupe=False)
        smoke, smoke_report = load_cases(root / SMOKE_PATH, dedupe=False)
        records, parse_errors = read_jsonl(root / EXPANSION_PATH)
    except (OSError, ValueError) as exc:
        _add(checks, "扩展集可加载", False, str(exc))
        return {"passed": False, "checks": checks, "metrics": metrics}

    _add(checks, "Schema 加载", not parse_errors and not load_report["parse_errors"]
         and not load_report["validation_errors"],
         f"raw={load_report['raw_records']} loaded={len(expansion)}")
    contract_problems = validate_expansion_contract(expansion, records, load_report)
    _add(checks, "40 题数据契约", not contract_problems, "；".join(contract_problems[:5]))

    findings = lint_cases(expansion)
    _add(checks, "case lint 零新增", not findings,
         "；".join(finding.render() for finding in findings[:5]) or "0 error / 0 warning / 0 exemption")
    fixture = fixture_summary(expansion)
    _add(checks, "fixture 可落盘", not fixture["issues"],
         f"{fixture['with_fixtures']} 条用例 / {fixture['files']} 个文件")

    core_ids = {case.id for case in core}
    expansion_ids = {case.id for case in expansion}
    _add(checks, "当前 core 未混入扩展题", not (core_ids & expansion_ids),
         f"当前 core {len(core)} 条 | 交集 {sorted(core_ids & expansion_ids)[:5]}")
    projected, dedupe = dedupe_cases_detailed([*core, *expansion])
    _add(checks, "合并投影无重复", dedupe["duplicates"] == 0 and not dedupe["conflicts"],
         f"投影 {len(projected)} 条 | 去重 {dedupe['duplicates']} | 冲突 {dedupe['conflicts']}")
    _add(checks, "合并投影规模", len(projected) == 247, f"期望 247，实际 {len(projected)}")

    counts = Counter(case.category for case in projected)
    _add(checks, "类别投影", dict(counts) == EXPECTED_PROJECTION_COUNTS, json.dumps(dict(counts), ensure_ascii=False))
    balance = category_balance(projected, max_ratio=3.0)
    _add(checks, "类别 max/min≤3", balance["passed"],
         f"{balance['max']}/{balance['min']}={balance['ratio']:.4f} | {balance['max_category']} / {balance['min_category']}")
    metrics["projection"] = {"total": len(projected), "balance": balance}

    repeat = suite_audit.repeat_rate_summary(projected)
    _add(checks, "重复率门禁", not repeat["duplicates"],
         f"真重复 {len(repeat['duplicates'])} 对 | 最高相似度 {repeat['max_similarity']:.3f}")
    judgeability = suite_audit.judgeability_summary(expansion)
    _add(checks, "可判率", judgeability["rate"] == 1.0 and not judgeability["unexpected_gaps"],
         f"{judgeability['judgeable']}/{judgeability['total']}")

    controls = {case.id: resolve_case_controls(case) for case in expansion}
    bad_controls = {case_id: value for case_id, value in controls.items() if value != ["C7"]}
    matrix = coverage_matrix(projected)
    _add(checks, "控制映射", not bad_controls and matrix["unmapped_count"] == 0,
         f"40 题均映射 C7={not bad_controls} | 投影未映射 {matrix['unmapped_count']} 条")

    smoke_pool, smoke_dedupe = dedupe_cases_detailed([*curated, *expansion])
    projected_smoke = select_smoke(
        smoke_pool,
        max_cases=18,
        must_tags=list(REQUIRED_SMOKE_TAGS),
        per_tag_limit=3,
        once_tags=list(ONCE_SMOKE_TAGS),
    )
    projected_ids = {case.id for case in projected_smoke}
    old_smoke_ids = {case.id for case in smoke}
    # 18 个槽位中加入两条新能力探针，必然替换两条旧题；只允许替换现有排序末尾的
    # 两条低优先级题，任何高分/高风险旧探针被挤掉都判失败。
    replaceable = {case.id for case in smoke[-2:]}
    lost = sorted(old_smoke_ids - projected_ids)
    once_counts = {
        tag: sum(tag in case.tags for case in projected_smoke)
        for tag in ONCE_SMOKE_TAGS
    }
    front = projected_smoke[: len(REQUIRED_SMOKE_TAGS) + len(ONCE_SMOKE_TAGS)]
    front_tags = {tag for case in front for tag in case.tags}
    smoke_ok = (
        smoke_dedupe["duplicates"] == 0
        and len(projected_smoke) == 18
        and set(lost) <= replaceable
        and all(count == 1 for count in once_counts.values())
        and set(REQUIRED_SMOKE_TAGS) <= front_tags
    )
    _add(checks, "smoke 投影保留旧探针", smoke_ok,
         f"18 条 | 仅替换末尾低优先级旧题 {lost} | 新能力单例 {once_counts} | 前 7 条覆盖全部必备 tag")
    metrics["smoke_projection"] = {
        "ids": [case.id for case in projected_smoke],
        "replaced_low_priority": lost,
        "once_counts": once_counts,
    }

    manifest_path = root / WINDOW_PATH
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        declared = {
            case_id
            for window in manifest.get("windows", [])
            for case_id in window.get("declared", [])
        }
        leaked = sorted(expansion_ids & declared)
        _add(checks, "旧窗口未声明新题", not leaked,
             f"协议 {manifest.get('protocol')} | 新题交集 {leaked}")
    except (OSError, json.JSONDecodeError) as exc:
        _add(checks, "旧窗口未声明新题", False, f"窗口清单不可读: {exc}")

    if include_oracle:
        oracle = selfcheck_suite(LayeredJudge(), expansion)
        positive_ok = all(
            row["verdict"] in {"PASS", "NOT_DETERMINISTIC"}
            for result in oracle["results"]
            for row in result["rows"]
            if row["mut_id"] == "__positive__"
        )
        negatives_ok = (
            oracle["mutation_total"] > 0
            and oracle["mutation_leaky"] == 0
            and oracle["mutation_caught"] == oracle["mutation_total"]
        )
        ok = not oracle["dead_cases"] and not oracle["leaky_cases"] and positive_ok and negatives_ok
        _add(checks, "oracle 确定性闭环", ok,
             f"DEAD {len(oracle['dead_cases'])} | LEAKY {len(oracle['leaky_cases'])} | "
             f"反面捕获 {oracle['mutation_caught']}/{oracle['mutation_total']} | "
             f"正面待语义 {len(oracle['undetermined_cases'])}")
        metrics["oracle"] = {
            key: value for key, value in oracle.items() if key != "results"
        }

    after = _artifact_hashes(root)
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    _add(checks, "正式产物零污染", not changed, f"变化文件 {changed or '无'}")
    metrics["formal_artifacts_after"] = after
    return {"passed": all(item["ok"] for item in checks), "checks": checks, "metrics": metrics}


def main() -> int:
    parser = argparse.ArgumentParser(description="只读验收 T43 的 40 条能力题扩展集")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    parser.add_argument("--skip-oracle", action="store_true", help="只跑结构/投影检查（完整验收不得使用）")
    args = parser.parse_args()
    report = check_t43_expansion(include_oracle=not args.skip_oracle)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            print(f"[{'OK' if item['ok'] else 'FAIL'}] {item['name']} | {item['detail']}")
        print(f"\n{'PASS' if report['passed'] else 'FAIL'} | {sum(item['ok'] for item in report['checks'])}/{len(report['checks'])} 项通过")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
