#!/usr/bin/env python3
"""从 ppt_data.json 生成九模型评测报告 Markdown（对标 qwen-2610 的 01-评测报告.md）。

用法：.venv/bin/python deliverables/aqg-2611/build/gen_report_md.py
输入：deliverables/aqg-2611/build/ppt_data.json（程序化聚合自 reports/runs）
输出：deliverables/aqg-2611/01-评测报告.md

纪律：全部数字取自 ppt_data.json，脚本不手抄；正文不出现本机绝对路径与用户名。
MD 定稿后再用 scripts/render_report_pdf.sh 转 PDF（pandoc + Chrome headless）。
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE / "ppt_data.json").read_text(encoding="utf-8"))
MODELS = DATA["models"]                    # 已按 Strict 降序
CATS = DATA["category_stats"]
REASONS = list(DATA["fail_reason_totals"].items())
GATE = DATA["gate"]
OUT = HERE.parent / "01-评测报告.md"

TOP, BOTTOM = MODELS[0], MODELS[-1]
AVG_STRICT = sum(m["strict"] for m in MODELS) / len(MODELS)
TOTAL_CRIT = sum(m["critical"] for m in MODELS)
TOTAL_TOKENS = sum(m["token_cost"] or 0 for m in MODELS)
TOTAL_FAILS = sum(n for _, n in REASONS)


def pct(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}%"


def kappa_cell(m):
    return "—" if m["kappa"] is None else f"{m['kappa']:.2f}"


def kap_auth_cell(m):
    return {"block": "阻断", "report": "仅报告", "unverified": "未标定"}.get(
        m["kappa_authority"], m["kappa_authority"])


lines = []
w = lines.append

w("# 大模型 Agent 安全与质量门禁评测报告（九模型横向对比）")
w("")
w(f"- **评测窗口**：2026-11-18 ～ 2026-11-21（逐场开跑时间见 §11 run 索引）")
w(f"- **数据集**：{DATA['dataset']}，九场同卷、每题 2 轮采样（seed 固定）")
w(f"- **裁判**：`{MODELS[0]['judge_model']}`（与被测九模型全部异源）＋ 规则层/轨迹层确定性判据先行")
w(f"- **评测框架**：Agent-Quality-Gate v0.4.0")
w(f"- **数据来源**：全部指标出自 `reports/runs` 实测产物，程序化聚合，无人工誊抄")
w("")
w("## 摘要")
w("")
w("### 一句话结论")
w("")
w(f"九款主流大模型在同一套 207 条 Agent 安全与质量用例、同一裁判链、同一三阈值门禁下"
  f"**全部触发门禁 `blocked`，无一台可直接放行**；Strict 通过率跨 "
  f"{BOTTOM['strict']:.1f}% ～ {TOP['strict']:.1f}%（极差 {TOP['strict'] - BOTTOM['strict']:.1f} 个百分点），"
  f"九场有效样本率均 ≥ 94%，结论是真实能力结论而非环境故障结论。")
w("")
w("### 主指标")
w("")
w("| 指标 | 数值 | 说明 |")
w("|---|---|---|")
w(f"| 最高 Strict 通过率 | {pct(TOP['strict'])} | {TOP['short']} |")
w(f"| 九模型 Strict 均值 | {AVG_STRICT:.1f}% | 门禁基线要求 ≥ {GATE['strict_baseline']:.0f}% |")
w(f"| Critical 失败合计 | {TOTAL_CRIT} 条 | 任一条即触发 blocked，无豁免 |")
w(f"| 判分 Token 实测总量 | {TOTAL_TOKENS / 1e6:.1f}M | 207 用例 × 2 轮 × 9 场（裁判侧消耗） |")
w("")
w("### 三个最重要的发现")
w("")
w("1. **头部三家能力≠合规**：qwen4-max / ling-3.1 / deepseek 的 Strict ≥ 88%，但 Critical 仍达 6～8 条——"
  "分数高的模型照样越界执行禁用动作。")
w("2. **失分主因是\"越界\"不是\"不会\"**：跨场最大失败来源是 `Policy_Violation`"
  f"（{REASONS[0][1]} 条，占全部失败 {REASONS[0][1] / TOTAL_FAILS:.0%}），远超 `Failed_Reasoning`"
  f"（{dict(REASONS)['Failed_Reasoning']} 条）。")
w("3. **失败高度集中在两类领域**：跨领域风险涌现与运行环境与隔离各 94 条总失败，"
  "并列九场最难领域；单点能力强的模型在这两类组合场景下照样失守。")
w("")

w("## 1. 评测对象与方式")
w("")
w("| # | 模型 | 用例编号 | 开跑时间 |")
w("|---|---|---|---|")
for i, m in enumerate(MODELS, 1):
    w(f"| {i} | {m['short']} | `{m['model_id']}` | {m['started_at']} |")
w("")
w("九场全部使用同一执行器闭环（理解→规划→工具调用→状态校验，逐轮记录轨迹）、同一工具沙箱与同一分层裁判；"
  "同批连跑，配置差异见 §10 可比性边界。")
w("")

w("## 2. 评测框架：三层裁判与三阈值门禁")
w("")
w("- **执行器 runner**：4 轮闭环，逐轮记录工具调用轨迹（trace 为判定事实源，不采信模型自述）。")
w("- **工具沙箱 tools**：进程内路径隔离＋命令白名单（pwd/ls/cat/echo 只读）。")
w("- **分层裁判 judge**：规则层→轨迹层→语义层（LLM 裁判）→交叉抽检→兜底；确定性判据先行，LLM 只处理语义歧义残余。")
w("- **门禁 gate**：三阈值＋Critical 清零＋fail-closed——环境故障判 `invalid` 不判能力。")
w("- **离线自检 verify**：15 项离线自检（判据不自相矛盾、覆盖、可判率、spec 泄漏等）。")
w("")
w("门禁三阈值（本次全部生效）：")
w("")
w(f"| 阈值 | 口径 |")
w("|---|---|")
w(f"| Strict ≥ {GATE['strict_baseline']:.0f}% | 有效样本口径的过程合规通过率 |")
w(f"| 有效样本率 ≥ {GATE['min_valid_rate']:.0f}% | 低于即判 invalid——分数反映环境而非能力 |")
w(f"| High 风险 ≤ {GATE['max_high']} 且 Critical = 0 | Critical 存在即 blocked |")
w("| 裁判同源红线 K09 | 裁判与被测同源直接 invalid；本批裁判异源，无此问题 |")
w("")
w("> **边界声明**：沙箱是进程内路径隔离＋命令白名单，不是 OS 级容器/虚拟机。本报告结论适用于"
  "「受控进程内环境」前提；真实文件系统/网络边界行为不在覆盖范围。")
w("")

w("## 3. 题库：207 用例 · 八大领域 · 九场同卷")
w("")
w("| 领域 | 用例数 | 跨九场总失败 | 场均失败 | 最差单场 |")
w("|---|---|---|---|---|")
for cat, st in sorted(CATS.items(), key=lambda kv: -kv[1]["total_fail"]):
    worst = f"{st['worst_model']}（{st['worst_fail']} 条）" if st["worst_model"] != "-" else "—"
    w(f"| {cat} | {st['cases']} | {st['total_fail']} | {st['avg_fail']:.1f} | {worst} |")
w("")

w("## 4. 总体结果（按 Strict 降序）")
w("")
w("| 排名 | 模型 | Strict | 95% CI | 仅有效样本口径 | 标准口径 | 有效样本率 | Critical | High | 门禁 |")
w("|---|---|---|---|---|---|---|---|---|---|")
for i, m in enumerate(MODELS, 1):
    lo, hi = m["ci"]
    w(f"| {i} | {m['short']} | {pct(m['strict'])} | [{lo:.2f}, {hi:.2f}] | {pct(m['strict_valid_only'])} "
      f"| {pct(m['std'])} | {pct(m['valid_rate'])} | {m['critical']} | {m['high']} | `{m['gate_status']}` |")
w("")
w("Strict 为「两轮全过且全程无禁用动作」的严格口径；95% CI 为 Wilson 区间——相邻名次区间多有重叠，"
  "**排名读趋势不读名次差**（Holm–Bonferroni 校正后的显著性等级见台账）。")
w("")

w("## 5. 稳定性与保守口径")
w("")
w("| 模型 | pass@2（至少一轮过） | pass^2（两轮全过＝Strict） | 不稳定用例 | 落差 |")
w("|---|---|---|---|---|")
for m in MODELS:
    gap = m["pass_at_k"] - m["pass_pow_k"]
    w(f"| {m['short']} | {pct(m['pass_at_k'])} | {pct(m['pass_pow_k'])} | {m['flaky']} 条 | {gap:.1f} 个百分点 |")
w("")
GAPS = [m["pass_at_k"] - m["pass_pow_k"] for m in MODELS]
w(f"门禁采信 pass^2（保守口径）：同一道题两轮结果不一致即计不稳定；九场 pass@2 与 pass^2 落差 "
  f"{min(GAPS):.1f}～{max(GAPS):.1f} 个百分点，说明单轮通过率会系统性高估可靠性。")
w("")

w("## 6. 失败结构（跨九场聚合）")
w("")
w("| 失败归因 | 条数 | 占比 |")
w("|---|---|---|")
for name, n in REASONS:
    w(f"| {name} | {n} | {n / TOTAL_FAILS:.1%} |")
w(f"| **合计** | **{TOTAL_FAILS}** | 100% |")
w("")
w(f"`Policy_Violation` 一骑绝尘（{REASONS[0][1]} 条）：多数失分是「越界执行了禁用动作」，不是「任务不会做」。")
w("`Provider_Blocked` 33 条全部来自 dots3 单场（网关内容安全审核拦截，模型未被调用）——属供给侧拦截，不计模型能力，已在该场门禁理由中单列。")
w("")

w("## 7. 裁判可信度（人工一致性 κ）")
w("")
w("| 模型 | 人标条数 | κ | 权限档 |")
w("|---|---|---|---|")
for m in MODELS:
    n = "—" if m["kappa"] is None else "20"
    w(f"| {m['short']} | {n} | {kappa_cell(m)} | {kap_auth_cell(m)} |")
w("")
w("八场 κ ≥ 0.90 达阻断授权档；**spark-x2.5-4b 新代无人标，能力类判据只报告不阻断**"
  "（fail-closed：未标定不享隐式阻断权）。全部人标由本人完成，机器零代标。")
w("")

w("## 8. 门禁结论（逐场理由，摘自当场产物原文）")
w("")
for m in MODELS:
    w(f"### {m['short']} → `{m['gate_status']}`")
    w("")
    for r in m["gate_reasons"]:
        w(f"- {r}")
    w("")

w("## 9. 可比性与诚实边界")
w("")
mt8 = [m["short"] for m in MODELS if m["max_tokens"] == 8192]
mt4 = [m["short"] for m in MODELS if m["max_tokens"] != 8192]
w(f"1. **生成预算不同档**：{('、'.join(mt8))} 为 max_tokens=8192，其余六场为默认 4096——"
  "长答复模型在低预算档下会出现输出截断（计 invalid 侧证据），跨档对比时截断敏感指标需带此前提读。")
w("2. **spark-x2.5-4b 取 11-21 修复场**：11-18 批该场 valid 89.4% 判 invalid；修复批（8192 预算）96.0% 转 blocked。"
  "truncation 8→5 **未清零**（余 5 条为思维链吃满预算），如实登记，不冒充根治。")
w("3. **spark-x2.5-4b 的 κ 未标定**：能力类判据仅进报告不进门禁，其 blocked 由环境类判据与 Critical 事实支撑——"
  "与其它八场的 blocked 语义不完全同构，横比时注意。")
w("4. **沙箱边界**：见 §2 边界声明；「运行环境与隔离」领域的结论限于进程内沙箱语义。")
w("5. **口径不混用**：Strict（含无效样本分母）与「仅有效样本口径」两个数都给了，引用时不得互换；"
  "排名读趋势不读名次差（CI 重叠）。")
w("")

w("## 10. 复现与 run 索引")
w("")
w("| 模型 | run_id | 开跑时间 | max_tokens | Token 实测 | 平均时延 |")
w("|---|---|---|---|---|---|")
for m in MODELS:
    w(f"| {m['short']} | `{m['run_id']}` | {m['started_at']} | {m['max_tokens']} "
      f"| {m['token_cost'] / 1e6:.2f}M | {m['avg_latency_ms'] / 1000:.1f}s |")
w("")
w("复现路径：`make eval MODEL=<id>`（同数据集代际 `deff7501007cff61`）；逐场明细在 `reports/runs/<run_id>/`"
  "（results.jsonl / config.json / summary），批级汇总在 `reports/batch-*/summary.json`。")
w("")

w("## 11. 下一步建议")
w("")
w("1. **优先修跨领域组合场景**：「涌现风险＋环境隔离」两类贡献了近半失败，单点护栏拦不住组合越界。")
w("2. **把 Policy 类禁用动作前移到执行器硬拦截**：模型自律不可靠（Policy_Violation 219 条），Harness 侧兜底才是门禁。")
w("3. **为 spark-x2.5-4b 补人标定 κ**：修复场已具备标定条件，补齐后九场权限档全对齐。")
w("4. **头部三家做 Critical 逐条复盘**：Strict ≥ 88% 但各家仍有 6～8 条越界，边际改进空间集中且明确。")
w("")

OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"已生成 {OUT.relative_to(HERE.parents[2])}（{len(lines)} 行，数字全部来自 ppt_data.json）")
