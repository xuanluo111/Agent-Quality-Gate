import json
import os
import time
from dotenv import load_dotenv
from src.models.schema import TestCase
from src.engine.runner import AgentRunner
from src.engine.judge import LayeredJudge
from src.engine.report import generate_markdown

load_dotenv()

BASE_URL = "https://chatapi.weixin.qq.com/openai/v1"
MODEL_NAME = "GLM-5.2"

# ================= 可调参数 =================
RUN_LIMIT = 300            # 本次跑多少条（建议 200-500，跑完再全量）
PARTIAL_FILE = "reports/partial_results.jsonl"  # 增量保存文件
# ============================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="smoke", choices=["smoke", "stress"])
    args = parser.parse_args()

    filepath = f"cases/{args.suite}.jsonl"

    # ---------- 数据加载 ----------
    cases = []
    skipped = 0
    required_fields = ["id", "category", "input", "expected_behavior", "severity"]

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith("{") or not line.endswith("}"):
                skipped += 1
                continue
            try:
                obj = json.loads(line)
                missing = [k for k in required_fields if k not in obj]
                if missing:
                    skipped += 1
                    continue
                obj.setdefault("allowed_tools", [])
                obj.setdefault("forbidden_actions", [])
                cases.append(TestCase(**obj))
            except Exception:
                skipped += 1
                continue

    print(f"✅ 成功加载 {len(cases)} 条用例 | ⚠️ 跳过 {skipped} 条脏数据")

    # 限制本次跑多少条
    cases = cases[:RUN_LIMIT]
    print(f"🎯 本次评测条数: {len(cases)} (可通过 RUN_LIMIT 调整)\n")

    # ---------- API Key 检查 ----------
    api_key = os.environ.get("LK_DS_API_KEY")
    if not api_key:
        print("❌ 错误: 环境变量 LK_DS_API_KEY 未配置！")
        exit(1)

    runner = AgentRunner(BASE_URL, api_key, MODEL_NAME)
    judge = LayeredJudge()
    results = []
    total_token_used = 0

    # ---------- 增量保存文件准备 ----------
    os.makedirs("reports", exist_ok=True)
    open(PARTIAL_FILE, "w").close()  # 清空旧文件

    start_time = time.time()
    print(f"🚀 开始评测 | 数据集: {args.suite} | 用例数: {len(cases)}\n")

    for idx, case in enumerate(cases, 1):
        step_start = time.time()
        try:
            # 1. 靶子模型真实执行
            trace = runner.run_case(case)
            # 2. 裁判模型打分
            result = judge.evaluate(case, trace)
        except Exception as e:
            print(f"⚠️ 第 {idx} 条 ({case.id}) 执行失败: {e}")
            continue

        results.append(result)
        step_tokens = result.token_cost + trace.token_cost
        total_token_used += step_tokens

        # ---------- 增量保存，随时中断不丢数据 ----------
        try:
            with open(PARTIAL_FILE, "a", encoding="utf-8") as pf:
                pf.write(json.dumps(result.model_dump(), ensure_ascii=False) + "\n")
                pf.flush()
        except Exception:
            pass

        # ---------- 实时进度 & 剩余时间估算 ----------
        elapsed = time.time() - start_time
        avg_per_case = elapsed / idx
        remaining = avg_per_case * (len(cases) - idx)
        eta_min = remaining / 60

        print(f"[{idx}/{len(cases)}] [{result.risk_level.upper()}] (L-{result.judge_layer}) "
              f"靶子:{trace.token_cost} 裁判:{result.token_cost} | "
              f"累计:{total_token_used:,} tokens | "
              f"用时:{elapsed/60:.1f}min | 预计剩余:{eta_min:.1f}min")

    # ---------- 生成最终报告 ----------
    if len(results) > 0:
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        print(f"\n评测完成: 总用例 {total}, 通过 {passed}, 通过率 {passed/total*100:.1f}%")
        generate_markdown(results, "reports/latest.md")

        print("\n" + "="*50)
        print("🏁 评测完成！最终 Token 消耗战报")
        print("="*50)
        print(f"总用例数:   {len(results)}")
        print(f"通过率:     {passed/total*100:.2f}%")
        print(f"本次评测消耗总 Token: {total_token_used:,}")
        print(f"单个用例平均消耗:     {total_token_used//total:,} tokens")
        print(f"总耗时:     {(time.time()-start_time)/60:.1f} 分钟")
        print("="*50)
        print(f"📄 完整报告: reports/latest.md")
        print(f"📄 增量备份: {PARTIAL_FILE}")
    else:
        print("❌ 没有任何用例成功完成，请检查 API 和网络。")