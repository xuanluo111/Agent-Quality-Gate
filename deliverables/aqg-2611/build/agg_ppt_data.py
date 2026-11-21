#!/usr/bin/env python3
"""从 reports/runs 的 9 场权威 run 程序化聚合 PPT 数据（数字不手抄）。

用法：.venv/bin/python deliverables/aqg-2611/build/agg_ppt_data.py
输出：deliverables/aqg-2611/build/ppt_data.json
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "reports" / "runs"
OUT = Path(__file__).resolve().parent / "ppt_data.json"

# 9 场权威 run：11-18 批 8 场 + spark-4b 取 T18 修复场（旧 invalid 场被取代）
AUTH = {
    "qwen4-max-preview": "20261118-131428-qwen4-max-preview",
    "deepseek-flash": "20261118-131428-deepseek-flash",
    "ling-3.1-flash": "20261118-131428-tokendance-ling-3.1-flash",
    "mimo-v2.6-flash": "20261118-131428-tokendance-mimo-v2.6-flash",
    "spark-x2.5-4b": "20261121-0147-tokendance-spark-x2.5-4b",
    "step-3.5-flash": "20261118-131428-tokendance-step-3.5-flash",
    "dots3-note-preview": "20261118-131428-diandian-dots3-note-pre",
    "glm-4.5-flash": "20261118-131428-zhipu-glm-4.5-flash",
    "spark-x2.5-1.7b": "20261118-131428-tokendance-spark-x2.5-1.7b",
}

models_cfg = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
kappa_by_name = {}
default_mtok = models_cfg["defaults"].get("max_tokens")
for spec in models_cfg["models"]:
    kappa_by_name[spec["name"]] = spec.get("judge_kappa")

NAME = {
    "qwen4-max-preview": "qwen4-max-preview",
    "deepseek-flash": "deepseek-flash",
    "ling-3.1-flash": "tokendance-ling-3.1-flash",
    "mimo-v2.6-flash": "tokendance-mimo-v2.6-flash",
    "spark-x2.5-4b": "tokendance-spark-x2.5-4b",
    "step-3.5-flash": "tokendance-step-3.5-flash",
    "dots3-note-preview": "diandian-dots3-note-pre",
    "glm-4.5-flash": "zhipu-glm-4.5-flash",
    "spark-x2.5-1.7b": "tokendance-spark-x2.5-1.7b",
}

rows = []
for short, run in AUTH.items():
    d = json.loads((RUNS / run / "summary.json").read_text(encoding="utf-8"))
    cfg = json.loads((RUNS / run / "config.json").read_text(encoding="utf-8"))
    g = d["gate"]
    m = g.get("metrics", {})
    cname = NAME[short]
    kappa = kappa_by_name.get(cname)
    rows.append({
        "short": short,
        "model_id": d.get("model", ""),
        "run_id": run,
        "started_at": d.get("started_at", ""),
        "strict": d.get("strict_pass_rate"),
        "strict_valid_only": d.get("strict_pass_rate_valid_only"),
        "std": d.get("std_pass_rate"),
        "valid_rate": d.get("valid_rate"),
        "critical": d.get("critical_count"),
        "high": d.get("high_count"),
        "pass_at_k": d.get("pass_at_k"),
        "pass_pow_k": d.get("pass_pow_k"),
        "stability": d.get("stability_rate"),
        "flaky": d.get("flaky_count"),
        "harness_errors": d.get("harness_error_count"),
        "output_truncated": d.get("output_truncated_count"),
        "token_cost": d.get("total_token_cost"),
        "avg_latency_ms": d.get("avg_latency_ms"),
        "gate_status": g.get("status"),
        "gate_reasons": g.get("reasons", []),
        "kappa": kappa,
        "kappa_authority": "block" if (kappa is not None and kappa >= 0.8) else ("unverified" if kappa is None else "report"),
        "max_tokens": cfg.get("max_tokens", default_mtok),
        "judge_model": d.get("judge_model", ""),
        "by_category": d.get("by_category", {}),
        "category_failures": d.get("category_failures", {}),
        "fail_reasons": d.get("fail_reasons", {}),
        "ci": [d.get("strict_ci_low"), d.get("strict_ci_high")],
    })

rows.sort(key=lambda r: -(r["strict"] or 0))

# 全体题域分布（同题库，取任一场）+ 跨 9 场聚合
cat_total = rows[0]["by_category"]
reason_sum = {}
for r in rows:
    for k, v in r["fail_reasons"].items():
        reason_sum[k] = reason_sum.get(k, 0) + v

cat_stats = {}
for cat, n in cat_total.items():
    fails = [r["category_failures"].get(cat, 0) for r in rows]
    worst = max(fails)
    cat_stats[cat] = {
        "cases": n,
        "total_fail": sum(fails),
        "avg_fail": round(sum(fails) / len(rows), 2),
        "worst_model": rows[fails.index(worst)]["short"] if worst > 0 else "-",
        "worst_fail": worst,
    }

out = {
    "dataset": "core 207 条（数据集代际 deff7501007cff61）",
    "models": rows,
    "category_stats": cat_stats,
    "fail_reason_totals": dict(sorted(reason_sum.items(), key=lambda x: -x[1])),
    "gate": {"strict_baseline": 80.0, "min_valid_rate": 90.0, "max_high": 3},
}
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("OK rows:", len(rows), "->", OUT)
for r in rows:
    print(f"{r['short']:22s} strict={r['strict']:6.2f} valid={r['valid_rate']:6.2f} crit={r['critical']:3d} high={r['high']:3d} "
          f"kappa={str(r['kappa']):5s} mtok={r['max_tokens']} tok={r['token_cost']} gate={r['gate_status']}")
print("fail_reasons:", json.dumps(out["fail_reason_totals"], ensure_ascii=False))
print("cat_stats:", json.dumps({k: (v['cases'], v['total_fail'], v['worst_model'], v['worst_fail']) for k, v in cat_stats.items()}, ensure_ascii=False))
