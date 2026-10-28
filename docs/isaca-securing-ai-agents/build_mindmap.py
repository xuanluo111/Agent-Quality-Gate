#!/usr/bin/env python3
"""由 mindmap.json 渲染脑图：mindmap.png（概览）+ mindmap.html（可折叠全量细节）。

用法：python docs/isaca-securing-ai-agents/build_mindmap.py
"""
from __future__ import annotations

import html
import json
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE / "mindmap.json").read_text(encoding="utf-8"))

STATUS_COLOR = {"covered": "#15803d", "partial": "#b45309", "gap": "#b91c1c"}
STATUS_BG = {"covered": "#dcfce7", "partial": "#fef3c7", "gap": "#fee2e2"}
STATUS_LABEL = {"covered": "已覆盖", "partial": "半覆盖", "gap": "缺口"}


def wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for chunk in str(text).split("\n"):
        lines.extend(textwrap.wrap(chunk, width=width, break_long_words=False) or [""])
    return lines


def render_png(out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    from matplotlib.path import Path as MplPath
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("PingFang SC", "Hiragino Sans GB", "Heiti SC", "Arial Unicode MS"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name]
            break
    plt.rcParams["axes.unicode_minus"] = False

    branches = DATA["branches"]
    lh = 1.0
    pad = 0.5
    header_h = 1.5
    gap = 0.6
    detail_rows = []
    for b in branches:
        point_lines = wrap(b["png_point"], 52)
        action_lines = wrap(b["png_action"], 52)
        label_lines = wrap(f'{b["priority"]}  {b["label"]}', 20)
        detail_rows.append((b, label_lines, point_lines, action_lines))
        point_h = header_h + len(point_lines) * lh + pad
        action_h = header_h + len(action_lines) * lh + pad
        b["_block_h"] = point_h + action_h + gap
        b["_label_h"] = len(label_lines) * lh + 2 * pad
        b["_point_h"] = point_h
        b["_action_h"] = action_h

    total_h = sum(b["_block_h"] for b in branches) + (len(branches) + 1) * 1.0 + 11
    fig_h = total_h * 0.175
    fig_w = 17.0
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, 81)
    ax.set_ylim(total_h, 0)
    ax.axis("off")
    fig.subplots_adjust(left=0.005, right=0.995, top=0.995, bottom=0.005)

    def box(x0, x1, y0, y1, fc, ec, lw=1.0, radius=0.5, z=2):
        ax.add_patch(
            FancyBboxPatch(
                (x0, y0), x1 - x0, y1 - y0,
                boxstyle=f"round,pad=0,rounding_size={radius}",
                facecolor=fc, edgecolor=ec, linewidth=lw, zorder=z,
            )
        )

    def link(x_from, y_from, x_to, y_to, color):
        mid = (x_from + x_to) / 2
        path = MplPath(
            [(x_from, y_from), (mid, y_from), (mid, y_to), (x_to, y_to)],
            [1, 4, 4, 4],
        )
        ax.add_patch(matplotlib.patches.PathPatch(path, facecolor="none", edgecolor=color, linewidth=1.1, zorder=1))

    banner_h = 5.6
    root_cy = banner_h + (total_h - banner_h - 4.0) / 2
    box(1.0, 14.0, root_cy - 4.2, root_cy + 4.2, "#0f172a", "#0f172a")
    ax.text(7.5, root_cy, "\n".join(wrap(DATA["root"], 16)), ha="center", va="center",
            fontsize=10.0, color="white", fontweight="bold", linespacing=1.6, zorder=3)

    def labelled_box(x0, x1, y0, y1, title, lines, fc, ec, title_color, text_color):
        box(x0, x1, y0, y1, fc, ec, lw=0.9)
        ax.text(x0 + 0.9, y0 + header_h / 2, title, ha="left", va="center",
                fontsize=7.2, color=title_color, fontweight="bold", zorder=3)
        n = len(lines)
        ax.text(x0 + 0.9, y0 + header_h + n * lh / 2, "\n".join(lines), ha="left", va="center",
                fontsize=7.9, color=text_color, linespacing=1.5, zorder=3)

    y = banner_h + 1.0
    for b, label_lines, point_lines, action_lines in detail_rows:
        h = b["_block_h"]
        cy = y + h / 2
        color = STATUS_COLOR[b["status"]]
        link(14.0, root_cy, 17.0, cy, color)

        bx0, bx1 = 17.0, 40.0
        label_h = b["_label_h"]
        box(bx0, bx1, cy - label_h / 2, cy + label_h / 2, STATUS_BG[b["status"]], color, lw=1.4)
        ax.text(bx0 + 0.9, cy, "\n".join(label_lines), ha="left", va="center",
                fontsize=8.4, fontweight="bold", color="#0f172a", linespacing=1.45, zorder=3)

        cx0, cx1 = 43.0, 80.0
        labelled_box(
            cx0, cx1, y, y + b["_point_h"], "ISACA 控制要点", point_lines,
            "#f8fafc", "#cbd5e1", "#475569", "#1e293b",
        )
        link(bx1, cy, cx0, y + b["_point_h"] / 2, color)

        labelled_box(
            cx0, cx1, y + h - b["_action_h"], y + h, "本项目落地动作", action_lines,
            "#eef2ff", "#818cf8", "#3730a3", "#111827",
        )
        link(bx1, cy, cx0, y + h - b["_action_h"] / 2, "#818cf8")

        y += h + 1.0

    ax.text(1.0, 1.0, DATA["source"], ha="left", va="top", fontsize=8.0, color="#475569")
    legend = "分支状态：" + "   ".join(
        f"{STATUS_LABEL[k]}（{v.split('：', 1)[1]}）" for k, v in DATA["legend"].items()
    )
    ax.text(1.0, 2.6, legend, ha="left", va="top", fontsize=8.0, color="#475569")
    ax.text(1.0, 4.2, "优先级：P0 先做（低成本高说服力，纯离线可实现）· P1 值得做 · P2 有余力再做    ｜    定位：Harness 只能“验证”或“模拟”控制项，不能替被测系统实现真实隔离/身份/密钥托管",
            ha="left", va="top", fontsize=8.0, color="#334155")
    ax.text(1.0, total_h - 0.6, "数据源：mindmap.json ｜ 重新生成：python docs/isaca-securing-ai-agents/build_mindmap.py",
            ha="left", va="bottom", fontsize=7.4, color="#94a3b8")
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"PNG  -> {out}")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ISACA Securing AI Agents → Agent-Quality-Gate 优化脑图</title>
<style>
:root {{ --bg:#0f172a; --panel:#f8fafc; --line:#cbd5e1; --ink:#0f172a; --muted:#64748b; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:28px 20px 60px; background:#fff; color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif; }}
header h1 {{ margin:0 0 6px; font-size:22px; }}
header .src {{ color:var(--muted); font-size:12.5px; margin:0 0 14px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:16px; font-size:12.5px; }}
.legend span {{ padding:3px 10px; border-radius:999px; border:1px solid var(--line); }}
.p-covered {{ background:#dcfce7; color:#15803d; }}
.p-partial {{ background:#fef3c7; color:#b45309; }}
.p-gap {{ background:#fee2e2; color:#b91c1c; }}
.toolbar {{ margin-bottom:18px; }}
.toolbar button {{ font:inherit; font-size:12.5px; padding:5px 12px; border:1px solid var(--line);
  background:var(--panel); border-radius:8px; cursor:pointer; margin-right:8px; }}
.branch {{ border:1px solid var(--line); border-radius:12px; margin-bottom:10px; overflow:hidden; }}
.branch > summary {{ list-style:none; cursor:pointer; padding:12px 14px; display:flex; align-items:center;
  gap:10px; background:var(--panel); font-weight:600; font-size:14px; }}
.branch > summary::-webkit-details-marker {{ display:none; }}
.tag {{ font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; white-space:nowrap; }}
.prio {{ font-size:11px; font-weight:700; padding:2px 7px; border-radius:6px; color:#fff; }}
.P0 {{ background:#dc2626; }} .P1 {{ background:#7c3aed; }} .P2 {{ background:#64748b; }}
.short {{ font-weight:500; font-size:12px; color:var(--muted); margin-left:auto; text-align:right; }}
.body {{ padding:6px 14px 14px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px; margin-top:8px; }}
.col h3 {{ margin:6px 0 8px; font-size:12.5px; color:#334155; text-transform:none; letter-spacing:.2px; }}
.col ul {{ margin:0; padding-left:18px; }}
.col li {{ font-size:12.5px; line-height:1.62; margin-bottom:5px; }}
.now {{ background:#f1f5f9; border-radius:10px; padding:10px 12px; }}
.act {{ background:#eef2ff; border-radius:10px; padding:10px 12px; }}
code {{ background:#e2e8f0; border-radius:4px; padding:1px 5px; font-size:11.5px; }}
footer {{ margin-top:22px; color:var(--muted); font-size:11.5px; }}
details.sub {{ margin-top:8px; font-size:12.5px; color:#334155; }}
</style>
</head>
<body>
<header>
  <h1>ISACA《Securing AI Agents》→ Agent-Quality-Gate 优化脑图</h1>
  <p class="src">{source}</p>
</header>
<div class="legend">{legend}</div>
<div class="toolbar">
  <button onclick="toggleAll(true)">全部展开</button>
  <button onclick="toggleAll(false)">全部折叠</button>
  <button onclick="onlyGap()">只看缺口 / 半覆盖</button>
</div>
{branches}
<footer>数据源：<code>mindmap.json</code>（本文件与 mindmap.png 由 <code>build_mindmap.py</code> 生成）。定位说明：Agent-Quality-Gate 是评测 Harness 与发布门禁，对每条控制项只能「验证被测 Agent 有没有」或「自身用上」，不声称实现真实 OS 隔离、网络分段、密钥托管或企业 IAM。</footer>
<script>
function toggleAll(open) {{
  document.querySelectorAll('details').forEach(function (d) {{ d.open = open; }});
}}
function onlyGap() {{
  document.querySelectorAll('.branch').forEach(function (b) {{
    var s = b.dataset.status;
    b.style.display = (s === 'covered') ? 'none' : '';
    b.open = true;
  }});
}}
</script>
</body>
</html>
"""


def render_html(out: Path) -> None:
    legend = "".join(
        f'<span class="tag p-{k}">{STATUS_LABEL[k]}：{html.escape(v.split("：", 1)[1])}</span>'
        for k, v in DATA["legend"].items()
    )

    def items(rows):
        return "".join(f"<li>{html.escape(r)}</li>" for r in rows)

    blocks = []
    for b in DATA["branches"]:
        blocks.append(
            f"""<details class="branch" data-status="{b["status"]}" open>
<summary>
  <span class="tag p-{b["status"]}">{STATUS_LABEL[b["status"]]}</span>
  <span class="prio {b["priority"]}">{b["priority"]}</span>
  <span>{html.escape(b["label"])}</span>
  <span class="short">{html.escape(b["png_action"])}</span>
</summary>
<div class="body">
  <div class="grid">
    <div class="col"><h3>ISACA 控制要点</h3><ul>{items(b["points"])}</ul></div>
    <div class="col now"><h3>本项目现状（含证据）</h3><ul>{items(b["current"])}</ul></div>
    <div class="col act"><h3>落地动作</h3><ul>{items(b["actions"])}</ul></div>
  </div>
</div>
</details>"""
        )

    out.write_text(
        HTML_TEMPLATE.format(source=html.escape(DATA["source"]), legend=legend, branches="\n".join(blocks)),
        encoding="utf-8",
    )
    print(f"HTML -> {out}")


def main() -> int:
    render_png(HERE / "mindmap.png")
    render_html(HERE / "mindmap.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
