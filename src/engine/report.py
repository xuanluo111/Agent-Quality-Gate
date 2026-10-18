from collections import Counter

def generate_markdown(results: list, output_path: str):
    total = len(results)
    std_pass = sum(1 for r in results if r.passed)
    strict_pass = sum(1 for r in results if r.strict_passed)
    fail_reasons = Counter(r.fail_reason for r in results if not r.passed)
    critical_bugs = [r for r in results if r.risk_level == "critical" and not r.strict_passed]

    md = f"""# Agent-Quality-Gate 评测报告

## 🚦 质量门禁与双轨制 (Standard vs Strict)
- **总用例数**：{total}
- **🟢 宽松通过率 (Standard)**：{std_pass/total*100:.2f}%
- **🔴 严格通过率 (Strict)**：{strict_pass/total*100:.2f}%
- **致命漏洞 (Critical)**：{len(critical_bugs)} 个

## 🔍 错因分类分布 (Fail Reason)
"""
    for reason, count in fail_reasons.most_common():
        md += f"- {reason}: {count} 次\n"

    md += "\n## 🩸 高危漏洞详细诊断 (因果归因)\n"
    for bug in critical_bugs[:10]:
        md += f"""
### [{bug.case_id}] {bug.category}
- **现象**：{bug.phenomenon}
- **依据**：{bug.evidence}
- **影响**：{bug.impact}
- **修复建议**：{bug.suggestion}
- **判定层级**：{bug.judge_layer}
---
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md)