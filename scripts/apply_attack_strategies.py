#!/usr/bin/env python3
"""按 attack_logic 前缀回填「攻击策略叠加」标签（G7）。

为什么要回填：攻击策略原本只写在 `attack_logic` 的自由文本里，人能读、机器算不了，
于是回答不了“单策略直给 0/7、叠加策略 n/7”这种问题——下一轮选策略只能靠拍脑袋。
把策略变成受控词表里的结构化字段，才能让“哪种组合真的有判别力”被算出来。

规则：
- 只处理 `source` 以 `mutation-` 开头的用例（它们才是攻击用例）；
- 已有 `attack_strategies` 的不动（幂等，可重复跑）；
- 按 `attack_logic` 前缀匹配受控词表；**匹配不到就报错退出**——
  新增变异用例必须显式标策略，不能悄悄漏掉，否则矩阵里就少了一格。

刻意不标策略的用例：改口 / 草稿这类“机制陷阱”（`反复改口`、`改口+草稿陷阱`）
不是攻击策略，混进策略矩阵会把两件事搅在一起。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.models.schema import ATTACK_STRATEGIES

# attack_logic 前缀 -> 受控词表 key。前缀匹配（不是全文匹配）保证回填是确定性的：
# 同一份文件跑几次，结果都一样。
STRATEGY_PREFIXES = {
    "话术换装": "voice",
    "角色伪装": "role",
    "编码混淆": "encoding",
    "编码换装": "encoding",
    "多轮渐进": "gradual",
    "紧急施压": "urgent",
    "文件载体注入": "carrier",
    "调试伪装": "plausible",
    "外发诱导": "plausible",
    "探测前置": "recon",
}
# 这些是机制陷阱而非攻击策略，明确不标（见模块 docstring）。
MECHANISM_PREFIXES = ("反复改口", "改口+草稿陷阱")


def is_mechanism_trap(record: dict) -> bool:
    """改口 / 草稿这类机制陷阱：刻意不标攻击策略。"""
    attack_logic = str(record.get("attack_logic") or "")
    return any(attack_logic.startswith(prefix) for prefix in MECHANISM_PREFIXES)


def strategies_for(record: dict) -> list:
    """按前缀推出策略标签；推不出来返回空列表。"""
    attack_logic = str(record.get("attack_logic") or "")
    matched = []
    for prefix, key in STRATEGY_PREFIXES.items():
        if attack_logic.startswith(prefix) and key not in matched:
            matched.append(key)
    # 按词表顺序输出，避免同一组策略在不同用例里顺序不同。
    return [key for key in ATTACK_STRATEGIES if key in matched]


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 G7 攻击策略叠加标签")
    parser.add_argument("--input", default="cases/curated_cases.jsonl")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要改什么，不写文件")
    parser.add_argument(
        "--allow-unlabeled",
        action="store_true",
        help="允许存在匹配不到前缀的变异用例（默认报错退出）",
    )
    args = parser.parse_args()

    path = Path(args.input)
    if not path.is_file():
        print(f"数据集不存在: {path}")
        return 1

    lines = path.read_text(encoding="utf-8").splitlines()
    out_lines = []
    labeled = []
    unlabeled = []
    skipped = 0
    mechanism = 0

    for index, line in enumerate(lines, 1):
        if not line.strip():
            out_lines.append(line)
            continue
        record = json.loads(line)
        source = str(record.get("source") or "")
        if not source.startswith("mutation-"):
            out_lines.append(line)
            continue
        if record.get("attack_strategies"):
            out_lines.append(line)
            skipped += 1
            continue
        strategies = strategies_for(record)
        if not strategies:
            if is_mechanism_trap(record):
                # 机制陷阱不是攻击策略：矩阵里不占格子，但也不是“没登记”。
                mechanism += 1
            else:
                unlabeled.append((index, record.get("id"), record.get("attack_logic", "")[:30]))
            out_lines.append(line)
            continue
        record["attack_strategies"] = strategies
        labeled.append((record.get("id"), strategies))
        out_lines.append(json.dumps(record, ensure_ascii=False))

    print(f"输入: {path}（{len(lines)} 行）")
    print(f"已标策略、跳过: {skipped} 条")
    print(f"机制陷阱（刻意不标策略）: {mechanism} 条")
    print(f"本次回填: {len(labeled)} 条")
    for case_id, strategies in labeled:
        names = "/".join(f"{key}({ATTACK_STRATEGIES[key]})" for key in strategies)
        print(f"  {case_id}: {names}")
    if unlabeled:
        print(f"\n匹配不到前缀的变异用例: {len(unlabeled)} 条")
        for index, case_id, logic in unlabeled:
            print(f"  第 {index} 行 {case_id}: {logic}")
        if not args.allow_unlabeled:
            print(
                "\nERROR: 这些用例的攻击策略没有显式登记。"
                "\n请给 attack_logic 加上受控前缀、或直接在用例里写 attack_strategies，"
                "\n确认无误再加 --allow-unlabeled 重跑。"
            )
            return 1

    if args.dry_run:
        print("\n(dry-run，未写文件)")
        return 0
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"\n已写入: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
