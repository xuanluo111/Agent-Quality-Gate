import json
import os
from dotenv import load_dotenv
from src.models.schema import TestCase
from src.engine.runner import AgentRunner
from src.engine.judge import LayeredJudge
from src.engine.report import generate_markdown


load_dotenv()
BASE_URL = "https://chatapi.weixin.qq.com/openai/v1"
MODEL_NAME = "GLM-5.2"

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="smoke", choices=["smoke", "stress"])
    args = parser.parse_args()

    filepath = f"cases/{args.suite}.jsonl"
    cases = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(TestCase(**json.loads(line)))
    
    runner = AgentRunner(BASE_URL, os.environ["LK_DS_API_KEY"], MODEL_NAME)
    judge = LayeredJudge()
    results = []

    # 全局 Token 消耗记账本
    total_token_used = 0
    
    print(f"🚀 开始评测 | 数据集: {args.suite} | 用例数: {len(cases)}\n")

    for idx, case in enumerate(cases, 1):
        trace = runner.run_case(case)
        result = judge.evaluate(case, trace)
        results.append(result)
        print(f"[{result.risk_level.upper()}] {case.id} (L-{result.judge_layer}) - {result.judge_reason}")
        # 累加消耗
        total_token_used += result.token_cost
        
        # 终端实时打印进度和Token消耗
        print(f"[{idx}/{len(cases)}] [{result.risk_level.upper()}] (L-{result.judge_layer}) "
              f"消耗: {result.token_cost} tokens | 累计: {total_token_used} tokens")
    
    # 生成报告
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    print(f"\n评测完成: 总用例 {total}, 通过 {passed}, 通过率 {passed/total*100:.1f}%")
    generate_markdown(results, "reports/latest.md")
    # ================= 最终战报打印 =================
    print("\n" + "="*50)
    print("🏁 评测完成！最终 Token 消耗战报")
    print("="*50)
    print(f"总用例数:   {len(results)}")
    print(f"通过率:     {sum(1 for r in results if r.passed)/len(results)*100:.2f}%")
    print(f"本次评测消耗总 Token: {total_token_used:,}")
    print(f"单个用例平均消耗:     {total_token_used//len(results) if len(results)>0 else 0:,} tokens")
    print("="*50)
    print("📄 详细报告已生成: reports/latest.md")
