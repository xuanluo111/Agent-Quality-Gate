from collections import Counter

def generate_markdown(results: list, output_path: str):
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    by_cat = Counter(r.category for r in results)
    
    md = f"""# Agent-Quality-Gate 评测报告

## 总览
- 总用例：{total}
- 通过率：{passed/total*100:.2f}%
- 类别分布：{dict(by_cat)}

## 高危用例
"""
    for r in results:
        if not r.passed and r.risk_level == "high":
            md += f"- [{r.case_id}] {r.category}: {r.judge_reason}\n"
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md)