"""裁判校准：把机器判定和人工标注对齐，给出可解释的一致性指标。

用法：
    python scripts/calibrate_judge.py \
        --labels labels.jsonl \
        --results reports/runs/<run_id>/results.jsonl \
        [--output reports/calibration.json] [--json]

人工标注 JSONL 每行：
    {"case_id": "...", "human_passed": true, "human_stage": "generation"}

指标定义（正类 = passed）：
- 漏判率 miss_rate   = 机器判通过但人工判失败 / 人工判失败总数
- 误伤率 false_alarm = 机器判失败但人工判通过 / 人工判通过总数
- Cohen's kappa：扣除随机一致后的真实一致度，只用标准库实现，不联网。
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.append(str(Path(__file__).resolve().parent.parent))


def read_jsonl(path: str | Path) -> List[dict]:
    records: List[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def load_labels(path: str | Path) -> Dict[str, dict]:
    labels: Dict[str, dict] = {}
    for record in read_jsonl(path):
        case_id = str(record.get("case_id") or "").strip()
        if not case_id:
            continue
        labels[case_id] = {
            "human_passed": bool(record.get("human_passed")),
            "human_stage": str(record.get("human_stage") or "").strip().lower() or None,
        }
    return labels


def load_judgements(path: str | Path) -> Dict[str, dict]:
    """读取 runs/<run_id>/results.jsonl；兼容 {"result": {...}} 与平铺两种写法。"""
    judgements: Dict[str, dict] = {}
    for record in read_jsonl(path):
        payload = record.get("result") if isinstance(record.get("result"), dict) else record
        case_id = str(payload.get("case_id") or "").strip()
        if not case_id:
            continue
        judgements[case_id] = {
            "judge_passed": bool(payload.get("strict_passed", payload.get("passed", False))),
            "judge_stage": (payload.get("failure_stage") or None),
            "judge_layer": payload.get("judge_layer") or "",
            "fail_reason": payload.get("fail_reason") or "",
        }
    return judgements


def confusion(pairs: List[Tuple[bool, bool]]) -> Dict[str, int]:
    tp = sum(1 for judge, human in pairs if judge and human)
    tn = sum(1 for judge, human in pairs if not judge and not human)
    fp = sum(1 for judge, human in pairs if judge and not human)
    fn = sum(1 for judge, human in pairs if not judge and human)
    return {"tp": tp, "tn": tn, "fp": fp, "fn": fn}


def cohens_kappa(pairs: List[Tuple[bool, bool]]) -> Optional[float]:
    """标准库实现；样本为空或期望一致度=1（全部同类）时返回 None。"""
    total = len(pairs)
    if not total:
        return None
    matrix = confusion(pairs)
    observed = (matrix["tp"] + matrix["tn"]) / total
    judge_pos = (matrix["tp"] + matrix["fp"]) / total
    human_pos = (matrix["tp"] + matrix["fn"]) / total
    expected = judge_pos * human_pos + (1 - judge_pos) * (1 - human_pos)
    if expected >= 1.0:
        return None
    return round((observed - expected) / (1 - expected), 4)


def calibrate(labels: Dict[str, dict], judgements: Dict[str, dict]) -> dict:
    matched = sorted(set(labels) & set(judgements))
    pairs = [(judgements[case_id]["judge_passed"], labels[case_id]["human_passed"]) for case_id in matched]
    matrix = confusion(pairs)
    # 分母按人工标签分组：人工判失败 = fp + tn，人工判通过 = fn + tp。
    human_failed = matrix["fp"] + matrix["tn"]
    human_passed = matrix["fn"] + matrix["tp"]
    stage_rows: List[dict] = []
    stage_matrix: Dict[str, Dict[str, int]] = {}
    for case_id in matched:
        human = labels[case_id]
        judge = judgements[case_id]
        if human["human_passed"]:
            continue
        key = human["human_stage"] or "unlabeled"
        bucket = stage_matrix.setdefault(key, {"human": 0, "agree": 0})
        bucket["human"] += 1
        if judge["judge_stage"] == human["human_stage"]:
            bucket["agree"] += 1
    for key, bucket in sorted(stage_matrix.items()):
        stage_rows.append(
            {
                "stage": key,
                "human": bucket["human"],
                "agree": bucket["agree"],
                "stage_accuracy": round(bucket["agree"] / bucket["human"] * 100, 2) if bucket["human"] else None,
            }
        )
    return {
        "matched": len(matched),
        "labels": len(labels),
        "judgements": len(judgements),
        "unmatched_labels": sorted(set(labels) - set(judgements)),
        "unmatched_results": sorted(set(judgements) - set(labels)),
        "confusion": matrix,
        "observed_agreement": round((matrix["tp"] + matrix["tn"]) / len(matched) * 100, 2) if matched else None,
        "miss_rate": round(matrix["fp"] / human_failed * 100, 2) if human_failed else None,
        "false_alarm_rate": round(matrix["fn"] / human_passed * 100, 2) if human_passed else None,
        "kappa": cohens_kappa(pairs),
        "stage_agreement": stage_rows,
    }


def render_text(report: dict) -> str:
    matrix = report["confusion"]
    lines = [
        f"配对样本: {report['matched']}（标注 {report['labels']} / 机器 {report['judgements']}）",
        "",
        "混淆矩阵（正类=通过）:",
        f"                 人工通过   人工失败",
        f"  机器通过        {matrix['tp']:>6}   {matrix['fp']:>6}",
        f"  机器失败        {matrix['fn']:>6}   {matrix['tn']:>6}",
        "",
        f"一致率: {report['observed_agreement']}%",
        f"漏判率（机器放过人工判失败）: {report['miss_rate']}%",
        f"误伤率（机器判失败但人工通过）: {report['false_alarm_rate']}%",
        f"Cohen's kappa: {report['kappa']}",
    ]
    if report["unmatched_labels"]:
        lines.append(f"缺少机器结果的标注: {len(report['unmatched_labels'])} 条")
    if report["unmatched_results"]:
        lines.append(f"缺少人工标注的结果: {len(report['unmatched_results'])} 条")
    if report["stage_agreement"]:
        lines += ["", "失败环节一致性:", "| 人工环节 | 样本 | 阶段一致 | 一致率 |", "|---|---|---|---|"]
        for row in report["stage_agreement"]:
            lines.append(
                f"| {row['stage']} | {row['human']} | {row['agree']} | "
                f"{row['stage_accuracy'] if row['stage_accuracy'] is not None else '-'}% |"
            )
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="裁判校准：混淆矩阵 / 漏判率 / 误伤率 / Cohen's kappa")
    parser.add_argument("--labels", required=True, help="人工标注 JSONL")
    parser.add_argument("--results", required=True, help="runs/<run_id>/results.jsonl")
    parser.add_argument("--output", help="把结果写成 JSON")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    labels = load_labels(args.labels)
    judgements = load_judgements(args.results)
    report = calibrate(labels, judgements)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0 if report["matched"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
