#!/usr/bin/env python3
"""生成 ISACA 控制项覆盖度矩阵，可选校验已落盘矩阵是否漂移。

矩阵是「规则 + 数据集」的确定性产物，所以刻意不写生成时间：
带时间戳的产物每重跑一次就多一个无关 diff，工作区永远扫不干净。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.engine.controls import ALL_FACETS, ALL_CONTROL_IDS, coverage_matrix, normalize_control_ids
from src.engine.dataset import dataset_sha256, load_cases


def load_overrides(path: Path) -> dict:
    """读取人工修订表：{case_id: [control_id, ...]}。

    文件不存在不算错误——自动推导本来就是主通道，override 只用来记录例外。
    """
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 顶层需要是 用例ID -> 控制项列表 的对象，实际是 {type(raw).__name__}")
    # 逐条校验控制项编号：写错一个编号就会静默改变覆盖度统计，比缺数据更难发现。
    return {case_id: normalize_control_ids(values) for case_id, values in raw.items()}


def build_matrix(suite: Path, overrides: dict) -> dict:
    cases, report = load_cases(suite)
    if report["validation_errors"] or report["parse_errors"]:
        raise SystemExit(
            f"数据集存在 {len(report['parse_errors'])} 条解析失败、"
            f"{len(report['validation_errors'])} 条校验失败，先修完再出矩阵"
        )
    matrix = coverage_matrix(cases, overrides)
    # override 里的用例 ID 写错不会报错，只会“修订没生效”，所以在这里点名。
    known_ids = {case.id for case in cases}
    stale_keys = sorted(key for key in overrides if key not in known_ids)
    matrix.update(
        {
            "suite": str(suite),
            "suite_sha256": dataset_sha256(suite),
            "override_cases": len(overrides),
            "override_unknown_ids": stale_keys,
            "control_total": len(ALL_CONTROL_IDS),
            "facet_total": len(ALL_FACETS),
        }
    )
    return matrix


def print_summary(matrix: dict) -> None:
    covered = matrix["covered_ids"]
    print(
        f"数据集 {matrix['suite']}：{matrix['total_cases']} 条用例"
        f"（sha256 {matrix['suite_sha256'][:16]}），override {matrix['override_cases']} 条"
    )
    print(f"控制项覆盖 {len(covered)}/{matrix['control_total']}，子控制点覆盖 "
          f"{matrix['facet_total'] - len(matrix['facet_gaps'])}/{matrix['facet_total']}")
    print()
    print("控制项                         姿势          用例数")
    for control_id in ALL_CONTROL_IDS:
        row = matrix["controls"][control_id]
        # label 形如「C4 网络分段·隔离·沙箱执行」，只取编号后面的正文部分。
        print(f"  {control_id:<4} {row['label'].split(' ', 1)[-1]:<26} {row['posture']:<12} {row['cases']:>4}")
    print()
    if matrix["case_expected_gap_ids"]:
        print("真正要补的控制项（姿势是用例侧、却零覆盖）：" + ", ".join(matrix["case_expected_gap_ids"]))
    if matrix["framework_only"]:
        print("框架自证、本就不该有对应用例：" + ", ".join(matrix["framework_only"]))
    if matrix["facet_gaps"]:
        print(f"零覆盖的子控制点 {len(matrix['facet_gaps'])} 个：")
        for facet_id in matrix["facet_gaps"]:
            print(f"  - {facet_id}")
    if matrix["unmapped_count"]:
        print(f"未落到任何控制项的用例 {matrix['unmapped_count']} 条：{matrix['unmapped_case_ids']}")
    if matrix["override_unknown_ids"]:
        print(
            "WARN: override 里以下用例 ID 不在本数据集中，修订未生效："
            + ", ".join(matrix["override_unknown_ids"])
        )


def check_fresh(matrix: dict, output: Path) -> int:
    """比较重算结果与已落盘矩阵，用于 CI 与自检发现「规则改了、产物没跟着改」。"""
    if not output.is_file():
        print(f"ERROR: {output} 不存在，先跑一次本脚本生成")
        return 1
    stored = json.loads(output.read_text(encoding="utf-8"))
    if stored == matrix:
        print(f"OK: {output} 与当前规则和数据集一致")
        return 0
    print(f"DRIFT: {output} 已过期，控制项或子控制点覆盖发生变化，请重跑本脚本并一起提交")
    for control_id in ALL_CONTROL_IDS:
        old = stored.get("controls", {}).get(control_id, {}).get("cases")
        new = matrix["controls"][control_id]["cases"]
        if old != new:
            print(f"  - {control_id}: {old} -> {new}")
    if stored.get("facet_gaps") != matrix["facet_gaps"]:
        print(f"  - facet_gaps: {stored.get('facet_gaps')} -> {matrix['facet_gaps']}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 ISACA 控制项覆盖度矩阵")
    parser.add_argument("--suite", default="cases/core_suite.jsonl")
    parser.add_argument("--overrides", default="cases/isaca_controls_override.json")
    parser.add_argument("--output", default="cases/coverage_matrix.json")
    parser.add_argument("--check", action="store_true", help="只校验已落盘矩阵是否漂移，不写文件")
    parser.add_argument("--stdout", action="store_true", help="把矩阵打到标准输出，不落盘")
    args = parser.parse_args()

    suite, output = Path(args.suite), Path(args.output)
    matrix = build_matrix(suite, load_overrides(Path(args.overrides)))
    print_summary(matrix)

    if args.check:
        return check_fresh(matrix, output)
    if args.stdout:
        print(json.dumps(matrix, ensure_ascii=False, indent=2))
        return 0
    output.write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写入 {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
