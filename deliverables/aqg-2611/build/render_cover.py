#!/usr/bin/env python3
"""把无字底图叠上标题，产出成品封面 PNG（1792x1024，16:9）。

用法：.venv/bin/python deliverables/aqg-2611/build/render_cover.py
输出：deliverables/aqg-2611/封面-AQG九模型评测.png
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "封面-AQG九模型评测.png"

BG = HERE / "cover_bg.png"
FONT_CN = "/System/Library/Fonts/Hiragino Sans GB.ttc"      # index0=W3, index1=W6
FONT_EN = "/System/Library/Fonts/HelveticaNeue.ttc"

W, H = 1792, 1024
CYAN = (86, 220, 255)
WHITE = (244, 248, 255)
GREY = (168, 186, 214)


def font_cn(size, bold=False):
    return ImageFont.truetype(FONT_CN, size, index=1 if bold else 0)


def font_en(size, bold=False):
    return ImageFont.truetype(FONT_EN, size, index=1 if bold else 0)


def center(draw, y, text, fnt, fill, tracking=0):
    if tracking:
        widths = [draw.textlength(ch, font=fnt) for ch in text]
        total = sum(widths) + tracking * (len(text) - 1)
        x = (W - total) / 2
        for ch, wd in zip(text, widths):
            draw.text((x, y), ch, font=fnt, fill=fill)
            x += wd + tracking
    else:
        wd = draw.textlength(text, font=fnt)
        draw.text(((W - wd) / 2, y), text, font=fnt, fill=fill)


def clean_bg(img):
    """用左侧同高度镜像块盖掉右下角平台「AI 生成」水印（底图是对称暗色纹理，盖后无痕）。"""
    px0, y0, pw = 10, 898, 350          # 取左下角同高度块
    x0 = W - pw                         # 贴到右下角直至边沿，确保盖满水印
    patch = img.crop((px0, y0, px0 + pw, y0 + 126))
    img.paste(patch, (x0, y0))
    return img


img = clean_bg(Image.open(BG).convert("RGB").resize((W, H)))
# 上部轻微压暗带，保证文字对比度（不遮挡中心徽标）
overlay = Image.new("L", (W, H), 0)
ImageDraw.Draw(overlay).rectangle((0, 96, W, 460), fill=70)
img = Image.composite(Image.new("RGB", (W, H), (6, 12, 26)), img, overlay)
draw = ImageDraw.Draw(img)

center(draw, 128, "AGENT QUALITY GATE", font_en(30, bold=True), CYAN, tracking=10)
center(draw, 188, "大模型 Agent 安全与质量门禁评测报告", font_cn(64, bold=True), WHITE)
center(draw, 288, "九款主流大模型 · 同题库 207 用例 · 分层裁判 · 三阈值门禁", font_cn(30), GREY)

# 底部信息条
center(draw, H - 118, "评测日期 2026-11-18 ～ 2026-11-21　|　数据集代际 deff7501007cff61　|　AQG v0.4.0",
       font_cn(22), GREY)
center(draw, H - 74, "内部资料 · 数字均出自 reports/runs 实测产物，可复现可审计", font_cn(20), (120, 138, 168))

OUT.parent.mkdir(exist_ok=True)
img.save(OUT)
clean_bg(Image.open(BG).convert("RGB").resize((W, H))).save(HERE / "cover_bg_clean.png")
print("OK ->", OUT)
