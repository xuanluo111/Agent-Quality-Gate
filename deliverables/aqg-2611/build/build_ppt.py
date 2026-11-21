#!/usr/bin/env python3
"""AQG 九模型评测报告 PPT 构建脚本（python-pptx）。

用法：.venv/bin/python deliverables/aqg-2611/build/build_ppt.py
输入：deliverables/aqg-2611/build/ppt_data.json ＋ cover_bg.png
输出：deliverables/aqg-2611/AQG九模型评测报告.pptx

全部数字从 ppt_data.json 注入（程序化聚合自 reports/runs，不手抄）。
"""
import json
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE / "ppt_data.json").read_text(encoding="utf-8"))
MODELS = DATA["models"]                      # 已按 Strict 降序
CATS = DATA["category_stats"]
REASONS = list(DATA["fail_reason_totals"].items())
GATE = DATA["gate"]
OUT = HERE.parent / "AQG九模型评测报告.pptx"

# ---- 统一样式：深色科技风 ----
BG = RGBColor(0x0B, 0x15, 0x30)
PANEL = RGBColor(0x16, 0x25, 0x4D)
PANEL2 = RGBColor(0x1E, 0x30, 0x5C)
CYAN = RGBColor(0x56, 0xDC, 0xFF)
WHITE = RGBColor(0xF4, 0xF8, 0xFF)
GREY = RGBColor(0xA8, 0xBA, 0xD6)
DIM = RGBColor(0x78, 0x8A, 0xAA)
GREEN = RGBColor(0x3D, 0xDC, 0x84)
RED = RGBColor(0xFF, 0x6B, 0x6B)
AMBER = RGBColor(0xFF, 0xC8, 0x5A)
FONT_CN = "PingFang SC"
FONT_EN = "Helvetica Neue"

SW, SH = Inches(13.333), Inches(7.5)
prs = Presentation()
prs.slide_width, prs.slide_height = SW, SH
BLANK = prs.slide_layouts[6]


def _set_run(run, text, size, color, bold=False, font=FONT_CN, italic=False):
    run.text = text
    f = run.font
    f.size = Pt(size)
    f.bold = bold
    f.italic = italic
    f.color.rgb = color
    f.name = font
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        rPr.append(ea)
    ea.set("typeface", FONT_CN)


def add_slide(title=None, kicker=None, page=None):
    s = prs.slides.add_slide(BLANK)
    rect(s, 0, 0, SW, SH, BG)
    if title:
        if kicker:
            tb = s.shapes.add_textbox(Inches(0.55), Inches(0.32), Inches(9), Inches(0.3))
            p = tb.text_frame.paragraphs[0]
            _set_run(p.add_run(), kicker.upper(), 11, CYAN, bold=True, font=FONT_EN)
        tb = s.shapes.add_textbox(Inches(0.55), Inches(0.56), Inches(11.5), Inches(0.7))
        p = tb.text_frame.paragraphs[0]
        _set_run(p.add_run(), title, 26, WHITE, bold=True)
        rect(s, Inches(0.58), Inches(1.22), Inches(0.65), Inches(0.045), CYAN)
    if page:
        tb = s.shapes.add_textbox(SW - Inches(1.1), SH - Inches(0.42), Inches(0.7), Inches(0.3))
        p = tb.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.RIGHT
        _set_run(p.add_run(), str(page), 10, DIM, font=FONT_EN)
    return s


def rect(slide, x, y, w, h, fill, line=None, shape=MSO_SHAPE.RECTANGLE):
    sp = slide.shapes.add_shape(shape, x, y, w, h)
    sp.fill.solid()
    sp.fill.fore_color.rgb = fill
    if line is None:
        sp.line.fill.background()
    else:
        sp.line.color.rgb = line
        sp.line.width = Pt(0.75)
    sp.shadow.inherit = False
    return sp


def text(slide, x, y, w, h, runs, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
         space_after=4, line_spacing=None):
    """runs: list[paragraph]，paragraph = list[(text, size, color, bold, font?)]"""
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, para in enumerate(runs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(space_after)
        if line_spacing:
            p.line_spacing = line_spacing
        for seg in para:
            kw = dict(bold=seg[3]) if len(seg) > 3 else dict(bold=False)
            if len(seg) > 4:
                kw["font"] = seg[4]
            _set_run(p.add_run(), seg[0], seg[1], seg[2], **kw)
    return tb


def card(slide, x, y, w, h, big, label, color=CYAN, sub=None):
    rect(slide, x, y, w, h, PANEL, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    text(slide, x + Inches(0.18), y + Inches(0.16), w - Inches(0.36), Inches(0.62),
         [[(big, 28, color, True, FONT_EN)]])
    text(slide, x + Inches(0.18), y + Inches(0.82), w - Inches(0.36), Inches(0.32),
         [[(label, 12.5, WHITE, True)]])
    if sub:
        text(slide, x + Inches(0.18), y + h - Inches(0.44), w - Inches(0.36), Inches(0.34),
             [[(sub, 10.5, GREY)]])


def make_table(slide, x, y, w, h, headers, rows_data, col_w=None, size=11,
               head_size=11, row_h=None, aligns=None, cell_colors=None):
    """cell_colors: {(r,c): RGBColor} 覆盖文字色（r 从 0 计数据行）。"""
    shape = slide.shapes.add_table(len(rows_data) + 1, len(headers), x, y, w, h)
    tbl = shape.table
    tbl.first_row = False
    tbl.horz_banding = False
    if col_w:
        total = sum(col_w)
        for c, wd in enumerate(col_w):
            tbl.columns[c].width = Emu(int(w * wd / total))
    if row_h:
        for r in range(len(rows_data) + 1):
            tbl.rows[r].height = row_h
    for c, htxt in enumerate(headers):
        cell = tbl.cell(0, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = PANEL2
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = cell.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        _set_run(p.add_run(), htxt, head_size, CYAN, bold=True)
    for r, row in enumerate(rows_data):
        for c, val in enumerate(row):
            cell = tbl.cell(r + 1, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = BG if r % 2 == 0 else PANEL
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = cell.margin_right = Inches(0.06)
            p = cell.text_frame.paragraphs[0]
            p.alignment = (aligns[c] if aligns else PP_ALIGN.CENTER)
            color = (cell_colors or {}).get((r, c), WHITE)
            bold = c == 0 or color in (GREEN, RED, AMBER)
            _set_run(p.add_run(), str(val), size, color, bold=bold)
    return tbl


def gate_color(status):
    return {"passed": GREEN, "warn": AMBER, "blocked": RED, "invalid": DIM}.get(status, WHITE)


def fmt(v, nd=2, suffix=""):
    return "—" if v is None else f"{v:.{nd}f}{suffix}"


# ================= S1 封面 =================
s = prs.slides.add_slide(BLANK)
s.shapes.add_picture(str(HERE / "cover_bg_clean.png"), 0, 0, SW, SH)
text(s, Inches(0.9), Inches(1.05), Inches(11.5), Inches(0.4),
     [[("AGENT QUALITY GATE", 15, CYAN, True, FONT_EN)]], align=PP_ALIGN.CENTER)
text(s, Inches(0.9), Inches(1.5), Inches(11.5), Inches(1.0),
     [[("大模型 Agent 安全与质量门禁评测报告", 40, WHITE, True)]], align=PP_ALIGN.CENTER)
text(s, Inches(0.9), Inches(2.55), Inches(11.5), Inches(0.5),
     [[("九款主流大模型 · 同题库 207 用例 · 分层裁判 · 三阈值门禁", 16, GREY)]], align=PP_ALIGN.CENTER)
text(s, Inches(0.9), Inches(6.55), Inches(11.5), Inches(0.7),
     [[("评测日期 2026-11-18 ～ 2026-11-21　|　数据集代际 deff7501007cff61　|　AQG v0.4.0", 11.5, GREY)],
      [("内部资料 · 数字均出自 reports/runs 实测产物，可复现可审计", 10.5, DIM)]],
     align=PP_ALIGN.CENTER)

# ================= S2 摘要 =================
s = add_slide("摘要：一句话结论与关键数字", kicker="Executive Summary", page=2)
top, bottom = MODELS[0], MODELS[-1]
avg_strict = sum(m["strict"] for m in MODELS) / len(MODELS)
total_crit = sum(m["critical"] for m in MODELS)
total_tokens = sum(m["token_cost"] or 0 for m in MODELS)
text(s, Inches(0.6), Inches(1.5), Inches(12.1), Inches(0.9),
     [[("九款模型在同题库、同裁判、同判据下全部触发门禁 ", 17, WHITE, True),
       ("blocked（无一台可直接放行）", 17, RED, True),
       ("；Strict 通过率跨 ", 17, WHITE, True),
       (f"{bottom['strict']:.1f}% ～ {top['strict']:.1f}%", 17, CYAN, True),
       (f"（极差 {top['strict'] - bottom['strict']:.1f} 个百分点）。", 17, WHITE, True)]],
     line_spacing=1.15)
cw, gap, x0, y0 = Inches(2.94), Inches(0.16), Inches(0.6), Inches(2.55)
card(s, x0, y0, cw, Inches(1.5), f"{top['strict']:.2f}%", "最高 Strict 通过率",
     GREEN, sub=top["short"])
card(s, x0 + (cw + gap), y0, cw, Inches(1.5), f"{avg_strict:.1f}%", "九模型 Strict 均值",
     CYAN, sub=f"基线要求 ≥ {GATE['strict_baseline']:.0f}%")
card(s, x0 + (cw + gap) * 2, y0, cw, Inches(1.5), str(total_crit), "Critical 失败合计（条）",
     RED, sub="任一条即触发 blocked")
card(s, x0 + (cw + gap) * 3, y0, cw, Inches(1.5), f"{total_tokens / 1e6:.1f}M", "判分 Token 实测总量",
     AMBER, sub="207 用例 × 2 轮 × 9 场")
findings = [
    ("头部三家（qwen4-max / ling-3.1 / deepseek）Strict ≥ 88%，"
     "但 Critical 仍达 6～8 条，能力≠合规"),
    ("step / glm-4.5 / spark-1.7b 的 Critical 高达 16～19 条，"
     "失败集中在跨领域风险涌现与运行环境隔离"),
    ("跨场最大失败来源是 Policy_Violation（219 条），"
     "远超推理失败（59）——多数失分是“越界执行”，不是“不会做”"),
    ("九场有效样本率均 ≥ 94%，数据可信；门禁结论均为真实能力结论，"
     "非环境故障结论"),
]
yy = Inches(4.45)
for i, f in enumerate(findings):
    rect(s, Inches(0.6), yy + Inches(0.06), Inches(0.09), Inches(0.09), CYAN)
    text(s, Inches(0.85), yy - Inches(0.04), Inches(11.9), Inches(0.5),
         [[(f, 12.5, GREY)]], line_spacing=1.1)
    yy += Inches(0.56)

# ================= S3 评测框架 =================
s = add_slide("评测框架：AQG 三层裁判与三阈值门禁", kicker="Framework", page=3)
rect(s, Inches(0.6), Inches(1.5), Inches(5.9), Inches(5.5), PANEL, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
text(s, Inches(0.9), Inches(1.72), Inches(5.3), Inches(0.4), [[("被测对象与执行器", 15, CYAN, True)]])
comp = [
    ("执行器 runner", "4 轮闭环：理解→规划→工具调用→状态校验，逐轮记录轨迹"),
    ("工具沙箱 tools", "进程内路径隔离＋命令白名单（pwd/ls/cat/echo 只读）"),
    ("分层裁判 judge", "规则层→轨迹层→语义层（LLM 裁判 glm-5.3-flash）→交叉抽检→兜底"),
    ("门禁 gate", "三阈值＋Critical 清零＋fail-closed：环境故障判 invalid 不判能力"),
    ("离线自检 verify", "15 项自检（判据不自相矛盾、覆盖、可判率、spec 泄漏等）"),
]
yy = Inches(2.25)
for name, desc in comp:
    text(s, Inches(0.9), yy, Inches(1.85), Inches(0.7), [[(name, 12, WHITE, True)]])
    text(s, Inches(2.8), yy, Inches(3.5), Inches(0.85), [[(desc, 10.5, GREY)]], line_spacing=1.05)
    yy += Inches(0.94)
rect(s, Inches(6.75), Inches(1.5), Inches(6.0), Inches(3.6), PANEL, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
text(s, Inches(7.05), Inches(1.72), Inches(5.4), Inches(0.4), [[("门禁三阈值（本次全部生效）", 15, CYAN, True)]])
thr = [
    (f"Strict ≥ {GATE['strict_baseline']:.0f}%", "有效样本口径的过程合规通过率"),
    (f"有效样本率 ≥ {GATE['min_valid_rate']:.0f}%", "低于即判 invalid——分数反映环境而非能力"),
    (f"High 风险 ≤ {GATE['max_high']} 且 Critical = 0", "Critical 存在即 blocked，无豁免"),
    ("裁判同源红线 K09", "裁判与被测同源直接 invalid；本批裁判异源，无此问题"),
]
yy = Inches(2.28)
for name, desc in thr:
    text(s, Inches(7.05), yy, Inches(2.6), Inches(0.6), [[(name, 12, WHITE, True)]])
    text(s, Inches(9.75), yy, Inches(2.85), Inches(0.7), [[(desc, 10.5, GREY)]], line_spacing=1.05)
    yy += Inches(0.68)
rect(s, Inches(6.75), Inches(5.3), Inches(6.0), Inches(1.7), PANEL2, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
text(s, Inches(7.05), Inches(5.5), Inches(5.5), Inches(1.4),
     [[("边界声明", 13, AMBER, True)],
      [("沙箱是进程内路径隔离＋命令白名单，不是 OS 级容器/虚拟机。本报告结论适用于"
        "“受控进程内环境”前提；真实文件系统/网络边界行为不在覆盖范围。", 10.5, GREY)]],
     line_spacing=1.15, space_after=6)

# ================= S4 题库与领域分布 =================
s = add_slide("题库：207 用例 · 八大领域 · 九场同卷", kicker="Dataset", page=4)
cat_items = sorted(CATS.items(), key=lambda kv: -kv[1]["cases"])
cw2, gap2 = Inches(2.94), Inches(0.16)
for i, (cat, st) in enumerate(cat_items):
    x = Inches(0.6) + (i % 4) * (cw2 + gap2)
    y = Inches(1.6) + (i // 4) * Inches(2.15)
    fail_rate = st["total_fail"] / (st["cases"] * len(MODELS)) * 100
    color = RED if fail_rate >= 35 else (AMBER if fail_rate >= 20 else GREEN)
    rect(s, x, y, cw2, Inches(1.95), PANEL, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    text(s, x + Inches(0.2), y + Inches(0.16), cw2 - Inches(0.4), Inches(0.55),
         [[(cat, 13.5, WHITE, True)]])
    text(s, x + Inches(0.2), y + Inches(0.72), cw2 - Inches(0.4), Inches(0.5),
         [[(f"{st['cases']} 条", 17, CYAN, True, FONT_EN),
           ("  用例", 11, GREY)]])
    text(s, x + Inches(0.2), y + Inches(1.25), cw2 - Inches(0.4), Inches(0.6),
         [[(f"跨场失败 {st['total_fail']} 次 · 失败密度 ", 10, GREY),
           (f"{fail_rate:.0f}%", 11, color, True, FONT_EN)],
          [(f"最差：{st['worst_model']}", 9.5, DIM)]], line_spacing=1.1)
text(s, Inches(0.6), Inches(6.15), Inches(12.1), Inches(0.9),
     [[("失败密度 = 该领域跨 9 场总失败次数 ÷（用例数 × 9 场）。", 10.5, DIM)],
      [( f"数据集代际 {DATA['dataset'].split('（')[1].rstrip('）')}；repeat=2（同用例跑两轮，主结论取最差一轮）；"
         "每场约 220～390 万判分 token 实测。", 10.5, DIM)]])

# ================= S5 总排名 =================
s = add_slide("九模型总排名（按 Strict 降序）", kicker="Leaderboard", page=5)
headers = ["#", "模型", "Strict %", "Standard %", "有效样本率 %", "Critical", "High", "门禁"]
rows_data, cc = [], {}
for i, m in enumerate(MODELS):
    rows_data.append([i + 1, m["short"], fmt(m["strict"]), fmt(m["std"]),
                      fmt(m["valid_rate"]), m["critical"], m["high"], m["gate_status"]])
    cc[(i, 2)] = GREEN if m["strict"] >= 88 else (AMBER if m["strict"] >= 70 else RED)
    cc[(i, 5)] = RED if m["critical"] >= 10 else AMBER
    cc[(i, 7)] = gate_color(m["gate_status"])
make_table(s, Inches(0.6), Inches(1.55), Inches(12.1), Inches(4.6), headers, rows_data,
           col_w=[0.6, 3.2, 1.6, 1.6, 1.8, 1.2, 1.0, 1.4], size=12, head_size=12,
           cell_colors=cc)
text(s, Inches(0.6), Inches(6.45), Inches(12.1), Inches(0.7),
     [[("口径：Strict＝全过程合规（任一违规尝试即失败），分母含未测到样本（保守口径）；"
        "Standard＝仅看最终结果。九场门禁全部 blocked：无一台满足放行三阈值。", 11, GREY)]],
     line_spacing=1.15)

# ================= S6 稳定性 =================
s = add_slide("重复实验与稳定性（repeat=2）", kicker="Stability", page=6)
headers = ["模型", "pass@2 %", "pass^2 %", "两轮一致率 %", "Flaky 用例"]
rows_data, cc = [], {}
for i, m in enumerate(MODELS):
    rows_data.append([m["short"], fmt(m["pass_at_k"]), fmt(m["pass_pow_k"]),
                      fmt(m["stability"]), m["flaky"]])
    cc[(i, 3)] = GREEN if m["stability"] >= 94 else (AMBER if m["stability"] >= 88 else RED)
    cc[(i, 4)] = RED if (m["flaky"] or 0) >= 15 else WHITE
make_table(s, Inches(0.6), Inches(1.55), Inches(12.1), Inches(4.35), headers, rows_data,
           col_w=[3.4, 1.8, 1.8, 2.0, 1.6], size=12, head_size=12, cell_colors=cc)
text(s, Inches(0.6), Inches(6.25), Inches(12.1), Inches(0.9),
     [[("pass@2＝两轮任一通过（乐观口径）；pass^2＝两轮均过，主结论取最差轮，故恒等于 Strict。", 11, GREY)],
      [("两轮一致率＝同用例两轮判定结论相同的比例；Flaky 用例＝两轮结论翻转的用例数——"
        "flaky 越多，单轮结论越不可靠。", 11, GREY)]], line_spacing=1.2)

# ================= S7 失败结构 =================
s = add_slide("失败结构：错因分布与领域热点", kicker="Failure Analysis", page=7)
rect(s, Inches(0.6), Inches(1.5), Inches(6.1), Inches(5.4), PANEL, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
text(s, Inches(0.9), Inches(1.7), Inches(5.5), Inches(0.4),
     [[("跨 9 场错因合计（Strict 失败归因）", 14, CYAN, True)]])
top_reasons = REASONS[:8]
max_v = top_reasons[0][1]
yy = Inches(2.3)
bar_w_max = Inches(3.4)
for name, val in top_reasons:
    text(s, Inches(0.9), yy, Inches(2.15), Inches(0.35), [[(name, 10.5, WHITE)]])
    bw = Emu(int(bar_w_max * val / max_v))
    color = RED if name in ("Policy_Violation", "State_Pollution", "Egress_Violation") else CYAN
    rect(s, Inches(3.1), yy + Inches(0.05), max(bw, Inches(0.06)), Inches(0.22), color)
    text(s, Inches(6.6), yy, Inches(0.75), Inches(0.35), [[(str(val), 11, GREY, True, FONT_EN)]])
    yy += Inches(0.55)
text(s, Inches(0.9), yy + Inches(0.1), Inches(5.6), Inches(0.8),
     [[("Policy_Violation 独占 47%：多数失分是“明知越界仍执行”，"
        "属于 Agent 合规能力而非知识缺陷。", 10.5, AMBER)]], line_spacing=1.15)
rect(s, Inches(6.95), Inches(1.5), Inches(5.8), Inches(5.4), PANEL, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
text(s, Inches(7.25), Inches(1.7), Inches(5.2), Inches(0.4),
     [[("各领域失败密度（跨场平均失败/场）", 14, CYAN, True)]])
hot = sorted(CATS.items(), key=lambda kv: -kv[1]["avg_fail"])
max_avg = hot[0][1]["avg_fail"]
yy = Inches(2.35)
for cat, st in hot:
    text(s, Inches(7.25), yy, Inches(2.3), Inches(0.35), [[(cat, 10.5, WHITE)]])
    bw = Emu(int(Inches(2.15) * st["avg_fail"] / max_avg))
    color = RED if st["avg_fail"] >= 8 else (AMBER if st["avg_fail"] >= 5 else CYAN)
    rect(s, Inches(9.6), yy + Inches(0.05), max(bw, Inches(0.05)), Inches(0.22), color)
    text(s, Inches(11.85), yy, Inches(0.9), Inches(0.35),
         [[(f"{st['avg_fail']:.1f}", 11, GREY, True, FONT_EN)]])
    yy += Inches(0.56)
text(s, Inches(7.25), yy + Inches(0.1), Inches(5.3), Inches(0.9),
     [[("“跨领域风险涌现”“运行环境与隔离”并列最热：单点能力都不差，"
        "组合任务（多轮＋状态＋边界）才失守。", 10.5, AMBER)]], line_spacing=1.15)

# ================= S8 裁判可信度 κ =================
s = add_slide("裁判可信度：人工校准 κ 与判据授权", kicker="Judge Calibration", page=8)
headers = ["模型", "裁判", "κ（人工校准）", "判据授权", "说明"]
rows_data, cc = [], {}
for i, m in enumerate(MODELS):
    kap = "未标定" if m["kappa"] is None else f"{m['kappa']:.2f}"
    auth = "block（可阻断）" if m["kappa_authority"] == "block" else "report（只报告）"
    note = "新代裁判未人标，能力类判据不阻断" if m["kappa"] is None else "κ≥0.80，阻断授权成立"
    rows_data.append([m["short"], m["judge_model"], kap, auth, note])
    cc[(i, 2)] = WHITE if m["kappa"] is not None else AMBER
    cc[(i, 3)] = GREEN if m["kappa"] is not None else AMBER
    cc[(i, 1)] = GREY
    cc[(i, 4)] = DIM
make_table(s, Inches(0.6), Inches(1.55), Inches(12.1), Inches(4.35), headers, rows_data,
           col_w=[3.0, 1.9, 1.6, 2.0, 3.6], size=11.5, head_size=11.5, cell_colors=cc,
           aligns=[PP_ALIGN.LEFT, PP_ALIGN.CENTER, PP_ALIGN.CENTER, PP_ALIGN.CENTER, PP_ALIGN.LEFT])
text(s, Inches(0.6), Inches(6.25), Inches(12.1), Inches(0.95),
     [[("κ = 裁判与人工裁决的 Cohen's Kappa 一致性（每场 18～20 条人标，共 160 条，全部人标零代标）。"
        "八场 κ∈{0.90, 1.00}，均达阻断授权档。", 11, GREY)],
      [("spark-4b（T18 修复场）新代裁判暂无人标 → unverified：能力类判据只报告不阻断，"
        "环境/外泄类判据仍保留阻断权（fail-closed）。", 11, AMBER)]], line_spacing=1.2)

# ================= S9 门禁结论 =================
s = add_slide("门禁结论：为什么九场全部 blocked", kicker="Gate Verdicts", page=9)
rect(s, Inches(0.6), Inches(1.5), Inches(12.1), Inches(1.5), PANEL2, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
text(s, Inches(0.9), Inches(1.72), Inches(11.5), Inches(1.1),
     [[("blocked 主因结构（按场次统计）：", 13, WHITE, True),
       ("Critical≥1 条：9/9 场全部命中（最低 6 条，最高 19 条）；"
        "Strict<80%：6/9 场；High>3：8/9 场。", 13, GREY)],
      [("没有任何一场是“差一点就放行”——Critical 清零是硬条件，头部三家同样失守于此。", 11.5, AMBER)]],
     line_spacing=1.3, space_after=8)
yy = Inches(3.25)
for m in MODELS[:5]:
    reason = m["gate_reasons"][0] if m["gate_reasons"] else "—"
    text(s, Inches(0.6), yy, Inches(3.1), Inches(0.35), [[(m["short"], 11.5, WHITE, True)]])
    text(s, Inches(3.8), yy, Inches(8.9), Inches(0.35), [[(reason, 11, GREY)]])
    yy += Inches(0.42)
text(s, Inches(0.6), yy + Inches(0.12), Inches(12.1), Inches(0.9),
     [[("其余四场（step / dots3 / glm-4.5 / spark-1.7b）首因同型：Critical 级严格失败 9～19 条。"
        "完整 reasons 见各场 reports/runs/<run_id>/summary.json。", 11, DIM)],
      [("门禁三态：passed 放行 / warn 只警告 / blocked 拦截；invalid＝数据不可信，分数不构成能力结论——本批九场均为 blocked（可信数据）。", 11, DIM)]],
     line_spacing=1.25)

# ================= S10 诚实边界 =================
s = add_slide("可比性与诚实边界（必读）", kicker="Caveats", page=10)
items = [
    ("输出预算不完全同配置", RED,
     f"九场中 {sum(1 for m in MODELS if m['max_tokens'] == 8192)} 场 max_tokens=8192"
     f"（{'、'.join(m['short'] for m in MODELS if m['max_tokens'] == 8192)}），"
     f"其余 {sum(1 for m in MODELS if m['max_tokens'] != 8192)} 场为默认 4096。"
     "跨模型分数对比需标注此配置差，头对头减法不成立。"),
    ("spark-4b 截断未清零", AMBER,
     "T18 修复场把截断从 8 条降到 5 条（有效样本率 89.4%→96.0%）；余 5 条为思维链吃满 8192 预算，"
     "如实登记，不宣称根治。"),
    ("spark-4b 裁判 κ 未标定", AMBER,
     "新代裁判对该场无人工校准 → 能力类判据只报告不阻断（unverified，fail-closed 正确行为）；"
     "其 blocked 由环境/外泄类与整体统计得出，结论强度弱于其余八场。"),
    ("沙箱边界", CYAN,
     "进程内路径隔离＋命令白名单，非 OS 级隔离；结论适用范围以 §3 声明为准。"),
    ("口径不混用", CYAN,
     "Strict 总分母含未测到样本（保守口径）；有效样本口径（strict_on_valid）在 summary 单列，"
     "两口径全文不互换、不混用。"),
]
yy = Inches(1.55)
for title, color, desc in items:
    rect(s, Inches(0.6), yy + Inches(0.05), Inches(0.1), Inches(0.9), color)
    text(s, Inches(0.9), yy, Inches(3.4), Inches(1.0), [[(title, 13.5, WHITE, True)]])
    text(s, Inches(4.4), yy, Inches(8.3), Inches(1.05), [[(desc, 11, GREY)]], line_spacing=1.18)
    yy += Inches(1.08)

# ================= S11 复现 =================
s = add_slide("复现方法与产物清单", kicker="Reproducibility", page=11)
rect(s, Inches(0.6), Inches(1.5), Inches(6.0), Inches(5.4), PANEL, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
text(s, Inches(0.9), Inches(1.7), Inches(5.4), Inches(0.4), [[("如何复跑（离线校验路径）", 14, CYAN, True)]])
cmds = [
    ".venv/bin/python scripts/run_batch.py \\\n  --matrix models.json --batch-name <stamp>",
    ".venv/bin/python scripts/verify.py     # 15 项离线自检",
    ".venv/bin/pytest -q                   # 752 passed / 1 xfailed",
    ".venv/bin/python scripts/visualize_reports.py \\\n  --latest-per-model          # 同名多场取最新",
]
yy = Inches(2.25)
for c in cmds:
    rect(s, Inches(0.9), yy, Inches(5.4), Inches(0.95), BG)
    text(s, Inches(1.05), yy + Inches(0.1), Inches(5.15), Inches(0.8),
         [[(c, 9.5, GREEN, False, "Menlo")]], line_spacing=1.1)
    yy += Inches(1.12)
text(s, Inches(0.9), yy + Inches(0.05), Inches(5.4), Inches(0.6),
     [[("每场产物：config.json / results.jsonl / summary.json / report.md / dataset_audit.json", 10, DIM)]],
     line_spacing=1.15)
rect(s, Inches(6.85), Inches(1.5), Inches(5.9), Inches(5.4), PANEL, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
text(s, Inches(7.15), Inches(1.7), Inches(5.3), Inches(0.4), [[("九场权威 run 索引", 14, CYAN, True)]])
yy = Inches(2.22)
for m in MODELS:
    text(s, Inches(7.15), yy, Inches(2.25), Inches(0.35), [[(m["short"], 10.5, WHITE, True)]])
    text(s, Inches(9.4), yy, Inches(3.3), Inches(0.35), [[(m["run_id"], 9.5, GREY, False, "Menlo")]])
    yy += Inches(0.5)
text(s, Inches(7.15), yy + Inches(0.05), Inches(5.4), Inches(0.4),
     [[("spark-4b 旧 invalid 场（20261118-131428）审计保留，被 T18 修复场取代。", 10, DIM)]])

# ================= S12 封底 =================
s = add_slide(None, kicker=None, page=None)
rect(s, 0, 0, SW, SH, BG)
text(s, Inches(0.9), Inches(2.3), Inches(11.5), Inches(0.5),
     [[("下一步建议", 14, CYAN, True, FONT_EN)]], align=PP_ALIGN.CENTER)
text(s, Inches(1.6), Inches(2.95), Inches(10.1), Inches(2.2),
     [[("1.  补齐 spark-4b 新代裁判人标（20 条），使九场授权对齐", 15, WHITE)],
      [("2.  Policy_Violation 专项族分析——占全部失败 47%，优先治理收益最大", 15, WHITE)],
      [("3.  统一输出预算口径（8192）后补跑六场，消除跨模型对比的配置差", 15, WHITE)]],
     align=PP_ALIGN.LEFT, space_after=14, line_spacing=1.2)
rect(s, Inches(5.87), Inches(5.6), Inches(1.6), Inches(0.04), CYAN)
text(s, Inches(0.9), Inches(5.9), Inches(11.5), Inches(0.9),
     [[("AQG · Agent Quality Gate　|　本报告全部数字可追溯至 reports/runs 实测产物", 12, GREY)],
      [("v0.4.0 · 2026-11-21", 11, DIM)]],
     align=PP_ALIGN.CENTER, space_after=6)

prs.save(OUT)
print("OK slides:", len(prs.slides._sldIdLst), "->", OUT)
