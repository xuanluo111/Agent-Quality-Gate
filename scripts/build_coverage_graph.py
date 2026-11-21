#!/usr/bin/env python3
"""由 cases/coverage_matrix.json 渲染 ISACA 控制覆盖图（实心/半覆盖/空心）。

复用 docs/isaca-securing-ai-agents/build_mindmap.py 的 covered/partial/gap 三色机制，
把 AQG 的「规则＋数据集」确定性覆盖度画成一张图：

- 实心（绿）  = 该控制有用例、其下所有子控制点也都命中；
- 半覆盖（琥珀）= 该控制整体有用例，但底下有子控制点零命中（画成空心子节点）；
- 空心（红）  = 真缺口——姿势是「用例侧」却一条都没覆盖（case_expected_gap）；
- 中性（灰）  = 框架自证 / 明确不做，本就不该有对应用例，绝不画成空心，避免把「设计如此」误读成「漏测」。

核心红线：**无「未登记的空心」**。图上每个空心节点都必须能反查到一个已注册的缺口——
即 facet_gaps 里的项必须是 controls.FACETS 声明过的真实子控制点、case_expected_gap 里的项
必须是 CONTROL_CATALOG 声明过的真实控制项。validate_hollow_registration() 把这条钉成断言，
verify.py 的「控制覆盖矩阵」步骤会调用它；图不会凭白多出一个来路不明的空心。

矩阵是确定性产物，所以刻意不写生成时间戳（带时间戳每重跑就多一个无关 diff）。

用法：
  python scripts/build_coverage_graph.py                       # 生成 PNG
  python scripts/build_coverage_graph.py --check-invariant     # 只校验空心是否全部已登记
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.engine.controls import (
    ALL_FACETS,
    CONTROL_CATALOG,
    CONTROL_LABELS,
    POSTURE_CASE,
    POSTURE_HARNESS,
    POSTURE_NONE,
    FACETS,
)

# 三色沿用 build_mindmap.py；中性色区分「设计如此」与「真缺口」，防止误读。
COLOR = {
    "covered": "#15803d",     # 实心绿：有用例且子点齐全
    "partial": "#b45309",     # 半覆盖琥珀：有用例但底下有空心子点
    "gap": "#b91c1c",         # 空心红：真缺口（该有对应用例却零覆盖）
    "self_proof": "#475569",  # 框架自证：姿势为 harness，不计入用例覆盖
    "out_of_scope": "#94a3b8",  # 明确不做：out_of_scope，也不测
}
LABEL = {
    "covered": "已覆盖",
    "partial": "半覆盖",
    "gap": "缺口（待补用例）",
    "self_proof": "框架自证（不测）",
    "out_of_scope": "明确不做（不测）",
}
HOLLOW_STATUSES = ("gap",)  # 画成「空心」的控制级节点状态；子点空心另计。


def control_status(matrix: dict, control_id: str) -> str:
    """把单个控制项归到五态之一（覆盖／半覆盖／缺口／自证／不做）。"""
    covered = set(matrix.get("covered_ids", []))
    case_gap = set(matrix.get("case_expected_gap_ids", []))
    facet_gaps = set(matrix.get("facet_gaps", []))
    posture = matrix["controls"][control_id]["posture"]

    if control_id in covered:
        has_hollow_facet = any(f.split(".", 1)[0] == control_id for f in facet_gaps)
        return "partial" if has_hollow_facet else "covered"
    if control_id in case_gap:
        return "gap"
    if posture == POSTURE_NONE:
        return "out_of_scope"
    if posture == POSTURE_HARNESS:
        return "self_proof"
    # POSTURE_CASE／POSTURE_BOTH 却零覆盖却又不该有对应用例——理论不该出现；
    # 保守归为缺口，宁可高估待补，也不把漏网藏进中性色。
    return "gap"


def hollow_facets(matrix: dict) -> list[str]:
    """图上画成空心的子控制点（来自 facet_gaps）。"""
    return list(matrix.get("facet_gaps", []))


def hollow_controls(matrix: dict) -> list[str]:
    """图上画成空心的控制级节点（真缺口）。"""
    return [cid for cid in matrix.get("case_expected_gap_ids", [])]


def registered_facet_ids() -> set[str]:
    """代码里已注册的子控制点全集——空心节点的合法反查目标之一。"""
    return {facet.facet_id for facet in ALL_FACETS}


def registered_control_ids() -> set[str]:
    return {item.control_id for item in CONTROL_CATALOG}


def validate_hollow_registration(matrix: dict) -> list[str]:
    """红线断言：每个空心节点都必须能反查到一个已注册的缺口。

    返回被检出的「未登记空心」列表（空即通过），不抛异常——便于 verify 汇总所有违规再统一判红，
    而不是遇到第一个就中断。空心节点来自 hollow_facets ＋ hollow_controls；
    合法当且仅当：子点 ∈ controls.FACETS 注册过的 facet_id，控制 ∈ CONTROL_CATALOG 注册过的 id。
    """
    facet_ids = registered_facet_ids()
    control_ids = registered_control_ids()
    unregistered: list[str] = []
    for facet_id in hollow_facets(matrix):
        if facet_id not in facet_ids:
            unregistered.append(facet_id)
    for control_id in hollow_controls(matrix):
        if control_id not in control_ids:
            unregistered.append(control_id)
    return unregistered


def build_summary(matrix: dict) -> dict:
    """把每个控制项的分类与空心子点整理成可序列化结构，供渲染与测试断言共用。"""
    controls = []
    for control_id in matrix.get("all_control_ids", CONTROL_LABELS.keys()):
        status = control_status(matrix, control_id)
        controls.append(
            {
                "control_id": control_id,
                "label": CONTROL_LABELS.get(control_id, control_id),
                "posture": matrix["controls"][control_id]["posture"],
                "status": status,
                "hollow_facets": [f for f in hollow_facets(matrix) if f.split(".", 1)[0] == control_id],
            }
        )
    return {
        "controls": controls,
        "hollow_facets": hollow_facets(matrix),
        "hollow_controls": hollow_controls(matrix),
        "unregistered_hollows": validate_hollow_registration(matrix),
    }


def _facet_note(facet_id: str) -> str:
    for facets in FACETS.values():
        for facet in facets:
            if facet.facet_id == facet_id:
                return facet.note or "（缺登记说明）"
    return "（未注册的子控制点）"


def render_png(matrix: dict, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("PingFang SC", "Hiragino Sans GB", "Heiti SC", "Arial Unicode MS"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name]
            break
    plt.rcParams["axes.unicode_minus"] = False

    summary = build_summary(matrix)
    # 红线在渲染前再校验一次：宁可拒绝出图，也不画出来路不明的空心。
    if summary["unregistered_hollows"]:
        raise ValueError("存在未登记的空心节点，拒绝出图：" + ", ".join(summary["unregistered_hollows"]))

    row_h = 1.0
    pad = 0.4
    n = len(summary["controls"])
    total_h = n * (row_h + pad) + 8
    fig, ax = plt.subplots(figsize=(13.0, total_h * 0.32))
    ax.set_xlim(0, 74)
    ax.set_ylim(total_h, 0)
    ax.axis("off")
    fig.subplots_adjust(left=0.01, right=0.99, top=0.9, bottom=0.05)

    ax.text(1, 1.2, "ISACA 控制覆盖图（实心／半覆盖／空心）", fontsize=15, fontweight="bold", color="#0f172a", va="top")
    ax.text(1, 3.0, f"数据源：cases/coverage_matrix.json ｜ 用例 {matrix.get('total_cases', '?')} 条 ｜ "
                    f"控制项 {len(matrix.get('covered_ids', []))}/{matrix.get('control_total', 0)} ｜ "
                    f"子控制点 {matrix.get('facet_total', 0) - len(matrix.get('facet_gaps', []))}/{matrix.get('facet_total', 0)}",
            fontsize=9, color="#475569", va="top")

    # 控制框加宽到 30，状态字移到框外（x=box_w+1.5），空心子点从 x=40 起白底红框逐个画，
    # 避免长标签压住状态字、斜纹盖住子点编号。
    box_w = 30
    y = 5.0
    for item in summary["controls"]:
        color = COLOR[item["status"]]
        hollow = item["status"] in HOLLOW_STATUSES
        face = "white" if hollow else color
        ax.add_patch(
            FancyBboxPatch(
                (1, y), box_w, row_h,
                boxstyle="round,pad=0,rounding_size=0.3",
                facecolor=face, edgecolor=color, linewidth=2.0,
                hatch="///" if hollow else "",
            )
        )
        # 中性/实心用白字，空心（白底）用深色字，保证对比度。
        text_color = "#0f172a" if hollow else "white"
        ax.text(2, y + row_h / 2, item["label"].split(" ", 1)[-1], fontsize=8.6,
                color=text_color, va="center", fontweight="bold")
        ax.text(box_w + 1.5, y + row_h / 2, LABEL[item["status"]], fontsize=8.0, color=color, va="center")

        cx = 40
        for facet_id in item["hollow_facets"]:
            ax.add_patch(
                FancyBboxPatch(
                    (cx, y + 0.12), 15, row_h - 0.24,
                    boxstyle="round,pad=0,rounding_size=0.2",
                    facecolor="white", edgecolor=COLOR["gap"], linewidth=1.6,
                )
            )
            ax.text(cx + 0.6, y + row_h / 2, f"○ {facet_id}", fontsize=7.6, color=COLOR["gap"], va="center")
            cx += 16
        if hollow and not item["hollow_facets"]:
            ax.text(40, y + row_h / 2, "○ 真缺口：该有对应用例却零覆盖", fontsize=7.8, color=COLOR["gap"], va="center")

        y += row_h + pad

    ax.text(1, total_h - 0.6,
            "图例：实心=已覆盖；斜纹空心=真缺口；琥珀行右侧的红框=该控制下未命中的子控制点（空心）；灰=框架自证或明确不做（非漏测）。"
            "｜ 每个空心节点均可反查 controls.FACETS 登记项（无「未登记空心」）。",
            fontsize=8, color="#64748b", va="bottom")

    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"PNG -> {out}")


def load_matrix(path: Path) -> dict:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    # all_control_ids 由 catalog 顺序补全，保证图里 12 项齐全、顺序稳定。
    matrix.setdefault("all_control_ids", [item.control_id for item in CONTROL_CATALOG])
    return matrix


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染 ISACA 控制覆盖图（实心/半覆盖/空心）")
    parser.add_argument("--matrix", default="cases/coverage_matrix.json")
    parser.add_argument(
        "--output", default="docs/isaca-securing-ai-agents/aqg_coverage_graph.png"
    )
    parser.add_argument("--check-invariant", action="store_true", help="只校验空心是否全部已登记，不出图")
    parser.add_argument("--stdout-summary", action="store_true", help="把分类结果打到标准输出（JSON），不出图")
    args = parser.parse_args()

    matrix = load_matrix(Path(args.matrix))

    if args.stdout_summary:
        print(json.dumps(build_summary(matrix), ensure_ascii=False, indent=2))
        return 0

    unregistered = validate_hollow_registration(matrix)
    if unregistered:
        print("FAIL: 存在未登记的空心节点：" + ", ".join(unregistered))
        return 1
    if args.check_invariant:
        print(f"OK: 空心节点全部已登记（子控制点 {len(hollow_facets(matrix))} 个 + 控制 {len(hollow_controls(matrix))} 个）")
        return 0

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    render_png(matrix, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
