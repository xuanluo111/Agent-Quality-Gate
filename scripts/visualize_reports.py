#!/usr/bin/env python3
import argparse
import ast
import json
import re
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

PASS_RATE_RE = re.compile(
    r"\|\s*\*\*通过率\*\*\s*\|\s*\*{0,2}([\d.]+)%\*{0,2}\s*\|\s*\*{0,2}([\d.]+)%\*{0,2}"
)
TOTAL_RE = re.compile(r"\|\s*\*\*用例总数\*\*\s*\|\s*(\d+)\s*\|\s*(\d+)")
CRITICAL_RE = re.compile(r"\|\s*\*\*Critical 漏洞\*\*\s*\|\s*(\d+)\s*\|\s*(\d+)")
TIME_RE = re.compile(r"生成时间\*\*[:：]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
FAIL_REASON_RE = re.compile(r"^- \*\*(.*?)\*\*\s*[:：]\s*(\d+)\s*次", re.M)
CATEGORY_HEADER_RE = re.compile(r"^###\s+(\S+)\s+(\S+)\s+(.+?)\s*$", re.M)
FAIL_COUNT_RE = re.compile(r"失败总数\*\*[:：]\s*(\d+)")
CRIT_HIGH_RE = re.compile(r"Critical\s*/\s*High\*\*[:：]\s*(\d+)\s*/\s*(\d+)")
DICT_LINE_RE = re.compile(r"-\s*\*\*(类别分布|裁判层级分布)\*\*[:：]\s*`([^`]+)`")

ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"
# 自检运行（scripts/verify.py 生成的 run_id）默认不进对外看板：样本只有几条。
SELFCHECK_PREFIX = "verify-"
JUDGE_LAYER_ORDER = ["rule", "trajectory", "jcv_audit", "llm", "local_fallback"]
JUDGE_LAYER_COLORS = {
    "rule": "#2EC4B6",
    "trajectory": "#7C6FEA",
    "jcv_audit": "#F5A524",
    "llm": "#2E5BFF",
    "local_fallback": "#E5484D",
}
PIE_COLORS = ["#2E5BFF", "#2EC4B6", "#7C6FEA", "#F5A524", "#E5484D", "#6B7280", "#E879F9", "#84CC16"]
BLUE = "#2E5BFF"
GRAY = "#C7CEDB"
RED = "#E5484D"
AMBER = "#F5A524"

# 三原色图板：红=安全（Standard）、绿=合规（Strict）、蓝=可信（数据有效率）。
# 三圆叠加处按 RGB 加色混合，中心越白三项越全达标，偏色即短板。
VENN_GAMMA = 1.6  # 线性叠加时 82% 与 100% 几乎同色，提亮高分区间才看得出差距
VENN_DIMENSIONS = (
    ("safety", "安全", "#FF0000"),
    ("compliance", "合规", "#00FF00"),
    ("trust", "可信", "#0000FF"),
)
VENN_BACKGROUND = "#0B1220"


def slice_between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    end = text.find(end_marker, start + len(start_marker))
    return text[start:end] if end >= 0 else text[start:]


def model_label(path: Path) -> str:
    label = path.stem.strip()
    if label.startswith("观猹"):
        label = label[2:]
    return label


def parse_report(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if "SLO 总览与双轨制门禁" not in text or "错因分类分布" not in text:
        raise ValueError("非标准格式（缺少 SLO 总览或错因分类章节）")

    pass_match = PASS_RATE_RE.search(text)
    total_match = TOTAL_RE.search(text)
    critical_match = CRITICAL_RE.search(text)
    if not (pass_match and total_match and critical_match):
        raise ValueError("核心指标表格解析失败")

    time_match = TIME_RE.search(text)
    generated_at = time_match.group(1) if time_match else ""
    gate_line = next((line for line in text.splitlines() if "发布门禁判定" in line), "")

    fail_reasons = {}
    fail_section = slice_between(text, "二、错因分类分布", "---")
    for name, count in FAIL_REASON_RE.findall(fail_section):
        label = name.strip() or "Unlabeled"
        fail_reasons[label] = fail_reasons.get(label, 0) + int(count)

    category_failures = {}
    category_section = slice_between(text, "三、", "四、")
    headers = list(CATEGORY_HEADER_RE.finditer(category_section))
    for index, header in enumerate(headers):
        body_end = headers[index + 1].start() if index + 1 < len(headers) else len(category_section)
        body = category_section[header.end():body_end]
        count_match = FAIL_COUNT_RE.search(body)
        crit_high_match = CRIT_HIGH_RE.search(body)
        category_failures[header.group(3).strip()] = {
            "count": int(count_match.group(1)) if count_match else 0,
            "critical": int(crit_high_match.group(1)) if crit_high_match else 0,
            "high": int(crit_high_match.group(2)) if crit_high_match else 0,
        }

    case_categories, judge_layers = {}, {}
    for name, raw in DICT_LINE_RE.findall(text):
        try:
            parsed = ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError):
            continue
        if not isinstance(parsed, dict):
            continue
        if name == "类别分布":
            case_categories = parsed
        else:
            judge_layers = parsed

    total = int(total_match.group(1))
    fallback = int(judge_layers.get("local_fallback", 0))
    blocked = any(token in gate_line for token in ("Blocked", "拦截", "数据无效", "Invalid"))
    return {
        "file": path.name,
        "model": model_label(path),
        "generated_at": generated_at,
        "std_rate": float(pass_match.group(1)),
        "strict_rate": float(pass_match.group(2)),
        "total": total,
        "critical": int(critical_match.group(1)),
        "blocked": blocked,
        "gate_status": "blocked" if blocked else "passed",
        "mode": "",
        "judge_model": "",
        "sample_size": total,
        "population": total,
        "run_id": "",
        "ci_low": 0.0,
        "ci_high": 0.0,
        "harness_errors": 0,
        "fail_reasons": fail_reasons,
        "category_failures": {key: value["count"] for key, value in category_failures.items()},
        "judge_layers": {key: int(value) for key, value in judge_layers.items()},
        "fallback": fallback,
        "fallback_rate": round(fallback / total * 100, 2) if total else 0.0,
        "case_categories": {key: int(value) for key, value in case_categories.items()},
    }


def parse_summary(path: Path, reports_dir: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "strict_pass_rate" not in data or "gate" not in data:
        raise ValueError("非标准 summary.json（缺少 strict_pass_rate 或 gate）")
    gate = data.get("gate") or {}
    judge_layers = data.get("by_judge_layer") or data.get("judge_layers") or {}
    fail_reasons = data.get("fail_reasons") or {}
    category_failures = data.get("category_failures") or {}
    total = int(data.get("total_cases", data.get("total", 0)) or 0)
    fallback = int(data.get("fallback_count", data.get("fallback", judge_layers.get("local_fallback", 0))) or 0)
    try:
        relative = path.relative_to(reports_dir)
    except ValueError:
        relative = Path(path.parent.name) / path.name
    return {
        "file": str(relative),
        "model": str(data.get("model") or "unknown"),
        # started_at 是这次运行真正开始的时间；generated_at 只代表报告生成那一刻。
        "started_at": str(data.get("started_at") or ""),
        "generated_at": str(data.get("generated_at") or data.get("started_at") or ""),
        "std_rate": float(data.get("std_pass_rate", data.get("std_rate", 0.0)) or 0.0),
        "strict_rate": float(data.get("strict_pass_rate", data.get("strict_rate", 0.0)) or 0.0),
        "total": total,
        "critical": int(data.get("critical_count", data.get("critical", 0)) or 0),
        "blocked": bool(data.get("blocked", gate.get("status") == "blocked")),
        "gate_status": str(gate.get("status") or ("blocked" if data.get("blocked") else "passed")),
        "mode": str(data.get("mode") or ""),
        "judge_model": str(data.get("judge_model") or ""),
        "sample_size": int(data.get("sample_size", total) or 0),
        "population": int(data.get("population", total) or 0),
        "run_id": str(data.get("run_id") or ""),
        "ci_low": float(data.get("strict_ci_low", 0.0) or 0.0),
        "ci_high": float(data.get("strict_ci_high", 0.0) or 0.0),
        "harness_errors": int((gate.get("metrics") or {}).get("harness_errors", 0) or 0),
        "untrusted_samples": int(data.get("untrusted_samples", 0) or 0),
        "output_truncated_count": int(data.get("output_truncated_count", 0) or 0),
        "provider_blocked_count": int(data.get("provider_blocked_count", 0) or 0),
        "measurable_cases": int(data.get("measurable_cases", total) or 0),
        "valid_rate": float(data.get("valid_rate", 100.0) or 0.0),
        "strict_rate_on_valid_only": float(data.get("strict_pass_rate_valid_only", 0.0) or 0.0),
        "fail_reasons": {str(key): int(value) for key, value in fail_reasons.items()},
        "category_failures": {str(key): int(value) for key, value in category_failures.items()},
        "judge_layers": {str(key): int(value) for key, value in judge_layers.items()},
        "fallback": fallback,
        "fallback_rate": round(fallback / total * 100, 2) if total else 0.0,
        "case_categories": {str(key): int(value) for key, value in (data.get("by_category") or {}).items()},
    }


def sort_stamp(report: dict) -> str:
    """时间轴与排序一律用真实运行开始时间。

    `generated_at` 是报告重生成那一刻：重判一次就把不同日期跑的批次压到同一刻，
    趋势图会退化成一条竖线。旧报告没有 `started_at` 时才退回生成时间。
    """
    return str(report.get("started_at") or report.get("generated_at") or "")


def load_reports(reports_dir: Path, include_selfcheck: bool = False) -> tuple[list[dict], list[dict]]:
    """读取 runs/*/summary.json。

    默认排除 `verify-*` 自检运行：那几份是 6 条能力用例的冒烟结果，画进对比图会把
    “某个模型在 120 条上的得分”和“某个模型在 6 条上的得分”摆在同一个坐标轴里。
    """
    reports, skipped = [], []
    summaries = sorted((reports_dir / "runs").glob("*/summary.json"))
    if summaries:
        for path in summaries:
            if not include_selfcheck and path.parent.name.startswith(SELFCHECK_PREFIX):
                skipped.append(
                    {
                        "file": str(path.relative_to(reports_dir)),
                        "reason": "自检运行（run_id 以 verify- 开头），默认不计入对外看板；用 --include-selfcheck 纳入",
                        "kind": "selfcheck",
                    }
                )
                continue
            try:
                reports.append(parse_summary(path, reports_dir))
            except Exception as exc:
                skipped.append({"file": str(path.relative_to(reports_dir)), "reason": str(exc)})
    else:
        for path in sorted(reports_dir.glob("*.md")):
            try:
                reports.append(parse_report(path))
            except Exception as exc:
                skipped.append({"file": path.name, "reason": str(exc)})
    reports.sort(key=sort_stamp)
    counts = Counter(report["model"] for report in reports)
    for report in reports:
        if counts[report["model"]] == 1:
            report["display"] = report["model"]
        else:
            stamp = sort_stamp(report)[5:16] if sort_stamp(report) else report["file"]
            report["display"] = f'{report["model"]} · {stamp}'
    return reports, skipped


def build_venn(reports: list[dict]) -> list[dict]:
    """三原色图板：三个维度直接取 summary 现有字段，不新增口径。

    可信率低于 90% 的场次标记为不可信，图上给警示且不参与排名结论。
    """
    rows = []
    for report in sorted(reports, key=lambda item: item["strict_rate"], reverse=True):
        # Markdown 回退解析的报告没有 valid_rate，按“数据完整”处理，与 parse_summary 默认值一致。
        valid_rate = report.get("valid_rate")
        valid_rate = 100.0 if valid_rate is None else float(valid_rate)
        scores = {
            "safety": round(report["std_rate"] / 100, 4),
            "compliance": round(report["strict_rate"] / 100, 4),
            "trust": round(valid_rate / 100, 4),
        }
        weak = min(scores, key=scores.get)
        gap = round((max(scores.values()) - min(scores.values())) * 100, 2)
        rows.append(
            {
                "display": report["display"],
                "model": report["model"],
                "run_id": report["run_id"],
                "scores": scores,
                "weak": weak if gap >= 5 else "",
                "gap": gap,
                "reliable": valid_rate >= 90,
            }
        )
    return rows


def build_aggregates(reports: list[dict]) -> dict:
    fail_reasons = Counter()
    category_failures = Counter()
    judge_layers = Counter()
    for report in reports:
        fail_reasons.update(report["fail_reasons"])
        category_failures.update(report["category_failures"])
        judge_layers.update(report["judge_layers"])
    return {
        "fail_reasons": dict(fail_reasons.most_common()),
        "category_failures": dict(category_failures.most_common()),
        "judge_layers": {key: judge_layers.get(key, 0) for key in JUDGE_LAYER_ORDER if judge_layers.get(key)},
    }


def build_warnings(reports: list[dict], skipped: list[dict]) -> list[str]:
    warnings = []
    if reports and all(abs(report["std_rate"] - 100.0) < 1e-9 for report in reports):
        warnings.append(
            "所有报告的 Standard 通过率均为 100.00%：Standard 只衡量最终是否造成实际危害，"
            "不能替代 Strict 的全过程合规指标，请以 Strict 作为门禁主指标。"
        )
    fallback_total = sum(report["fallback"] for report in reports)
    fallback_reports = sum(1 for report in reports if report["fallback"] > 0)
    if fallback_total:
        warnings.append(
            f"{fallback_reports} 份报告出现 local_fallback，共 {fallback_total} 条判定由兜底逻辑完成；"
            "这些结果会削弱跨模型可比性，且已触发门禁失败关闭。"
        )
    harness_total = sum(report.get("harness_errors", 0) for report in reports)
    truncated_total = sum(report.get("output_truncated_count", 0) for report in reports)
    blocked_total = sum(int(report.get("provider_blocked_count") or 0) for report in reports)
    if truncated_total:
        warnings.append(
            f"共有 {truncated_total} 条用例输出被 max_tokens 截断（推理链吃满输出上限，正文与工具调用都没拿到）；"
            "这是配置问题不是模型能力问题，已按不可信样本处理，调大 max_tokens 后需重跑。"
        )
    if blocked_total:
        detail = "、".join(
            f'{report["display"]} {int(report.get("provider_blocked_count") or 0)} 条'
            for report in reports
            if int(report.get("provider_blocked_count") or 0)
        )
        warnings.append(
            f"共有 {blocked_total} 条用例被云厂商内容安全审核拦截（{detail}）：prompt 没送到模型，"
            "既不是模型失败也不是网关故障，已从有效率分母剔除并单列；横比请看“可测样本”口径。"
        )
    if harness_total - truncated_total - blocked_total > 0:
        warnings.append(
            f"共有 {harness_total - truncated_total - blocked_total} 条用例因 Harness/网关异常未完成评测，已按失败关闭处理。"
        )
    invalid_reports = [report for report in reports if report.get("gate_status") == "invalid"]
    if invalid_reports:
        names = "、".join(report["display"] for report in invalid_reports)
        warnings.append(
            f"{len(invalid_reports)} 份报告被门禁判定为数据无效（{names}）：多数样本因 Key/配额/网关/裁判异常或 max_tokens 过小"
            "根本没测到模型，其通过率不代表模型能力，不能参与横向对比。"
        )
    low_valid = [
        report for report in reports
        if report.get("gate_status") != "invalid" and report.get("valid_rate", 100.0) < 100.0
    ]
    if low_valid:
        detail = "、".join(
            f'{report["display"]} 有效率 {report["valid_rate"]:.1f}%'
            f'（仅可信样本 Strict {report["strict_rate_on_valid_only"]:.1f}%）'
            for report in low_valid
        )
        warnings.append(f"以下报告混入了不可信样本，看分请同时看仅可信样本口径：{detail}。")
    unlabeled = sum(report["fail_reasons"].get(label, 0) for report in reports for label in ("Unlabeled", "None"))
    if unlabeled:
        warnings.append(
            f"错因分布中有 {unlabeled} 条失败记录未标注 fail_reason（空值或 None），归因分析存在缺口。"
        )
    missing_meta = [
        report["file"]
        for report in reports
        if not report.get("mode") or not report.get("judge_model")
    ]
    if missing_meta:
        warnings.append(
            f"{len(missing_meta)} 份报告缺少运行模式或裁判模型元数据（{', '.join(missing_meta[:3])}）；"
            "旧版 Markdown 报告无法确认裁判一致性，建议改用 reports/runs/*/summary.json。"
        )
    shadow_reports = [report["file"] for report in reports if report.get("mode") == "shadow"]
    if shadow_reports:
        warnings.append(f"{len(shadow_reports)} 份报告运行在影子模式，门禁未实际拦截发布。")
    small_samples = [
        report["file"]
        for report in reports
        if report.get("sample_size", report["total"]) < 30
    ]
    if small_samples:
        warnings.append(
            f"{len(small_samples)} 份报告的样本量小于 30，Wilson 置信区间较宽，结论易受单条用例影响。"
        )
    for item in skipped:
        if item.get("kind") == "selfcheck":
            warnings.append(f"已排除自检运行 {item['file']}：{item['reason']}。")
        else:
            warnings.append(f"已跳过 {item['file']}：{item['reason']}。")
    return warnings


def build_payload(reports: list[dict], skipped: list[dict]) -> dict:
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reports": reports,
        "aggregates": build_aggregates(reports),
        "warnings": build_warnings(reports, skipped),
        "venn": build_venn(reports),
    }


def resolve_echarts(out_dir: Path) -> str:
    target = out_dir / "echarts.min.js"
    if target.exists() and target.stat().st_size > 100_000:
        return "./echarts.min.js"
    try:
        request = urllib.request.Request(ECHARTS_CDN, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            content = response.read()
        if len(content) > 100_000:
            target.write_bytes(content)
            return "./echarts.min.js"
    except Exception:
        pass
    return ECHARTS_CDN


def render_html(payload: dict, out_dir: Path) -> Path:
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    echarts_src = resolve_echarts(out_dir)
    html = HTML_TEMPLATE.replace("__ECHARTS_SRC__", echarts_src).replace("__DATA__", data_json)
    path = out_dir / "dashboard.html"
    path.write_text(html, encoding="utf-8")
    return path


def setup_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    available = {font.name for font in font_manager.fontManager.ttflist}
    for name in ("PingFang SC", "Hiragino Sans GB", "Heiti SC", "Arial Unicode MS", "Noto Sans CJK SC", "Microsoft YaHei", "SimHei"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name]
            break
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def compact_name(report: dict) -> str:
    name = report["display"].replace("DeepSeek-V4.1-Flash-1017", "DeepSeek-V4.1").replace("微信小程序", "wx·")
    return name[:18]


def save_figure(plt, fig, path: Path, facecolor: str = "white") -> None:
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor=facecolor)
    plt.close(fig)


def venn_image(scores: dict, size: int = 420):
    """用加色模型渲染三个圆：R/G/B 通道各存一个维度，叠加处自然混色。"""
    import numpy as np

    yy, xx = np.mgrid[0:size, 0:size]
    scale = size / 420

    def disk(cx: float, cy: float, radius: float, score: float):
        dist = np.sqrt((xx - cx * scale) ** 2 + (yy - cy * scale) ** 2)
        edge = np.clip((radius * scale - dist) / 1.5 + 0.5, 0.0, 1.0)
        return edge * (max(score, 0.0) ** VENN_GAMMA)

    red = disk(150, 235, 142, scores["safety"])
    green = disk(210, 135, 142, scores["compliance"])
    blue = disk(270, 235, 142, scores["trust"])
    background = np.array([11, 18, 32], dtype=float) / 255
    return np.clip(np.dstack([red, green, blue]) + background, 0.0, 1.0)


def render_venn_png(plt, rows: list[dict], out_dir: Path) -> Path:
    import numpy as np

    names = {key: label for key, label, _ in VENN_DIMENSIONS}
    # 4 个以内单行铺开；更多时按接近方形排布，避免最后一行大块留黑
    columns = len(rows) if len(rows) <= 4 else min(4, max(2, round((len(rows) * 1.8) ** 0.5)))
    lines = (len(rows) + columns - 1) // columns
    fig, axes = plt.subplots(lines, columns, figsize=(3.4 * columns, 3.9 * lines), facecolor=VENN_BACKGROUND)
    axes = np.atleast_1d(axes).ravel()
    for ax, row in zip(axes, rows):
        scores = row["scores"]
        ax.imshow(venn_image(scores))
        ax.set_axis_off()
        if not row["reliable"]:
            hint = "数据不可信，结论不成立"
        elif row["weak"]:
            hint = f'短板：{names[row["weak"]]} {scores[row["weak"]] * 100:.1f}%'
        else:
            hint = "三项均衡"
        ax.set_title(
            f'{row["display"]}\n'
            f'安全 {scores["safety"] * 100:.1f} · 合规 {scores["compliance"] * 100:.1f} · 可信 {scores["trust"] * 100:.1f}\n'
            f"{hint}",
            color="#E2E8F0",
            fontsize=9.5,
            pad=9,
            linespacing=1.55,
        )
    for ax in axes[len(rows):]:
        ax.set_axis_off()
    fig.suptitle("三原色图板 · 中心越白三项越全达标，偏色即短板", color="#F8FAFC", fontsize=13, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    path = out_dir / "07_tri_color_venn.png"
    save_figure(plt, fig, path, facecolor=VENN_BACKGROUND)
    return path


def render_pngs(reports: list[dict], aggregates: dict, out_dir: Path) -> list[Path]:
    import numpy as np

    plt = setup_matplotlib()
    outputs = []

    ordered = sorted(reports, key=lambda item: item["strict_rate"], reverse=True)
    labels = [item["display"] for item in ordered]
    std_rates = [item["std_rate"] for item in ordered]
    strict_rates = [item["strict_rate"] for item in ordered]
    positions = np.arange(len(labels))
    width = 0.38

    fig, ax = plt.subplots(figsize=(11, 6))
    bars_std = ax.bar(positions - width / 2, std_rates, width, label="Standard", color=GRAY)
    bars_strict = ax.bar(positions + width / 2, strict_rates, width, label="Strict", color=BLUE)
    for bars in (bars_std, bars_strict):
        for bar in bars:
            ax.annotate(f"{bar.get_height():.1f}%", (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                        ha="center", va="bottom", fontsize=8, color="#334155")
    ax.axhline(80, color=AMBER, linestyle="--", linewidth=1.2, label="SLO 80%")
    ax.set_xticks(positions)
    ax.set_xticklabels(labels, rotation=25, ha="right", fontsize=9)
    ax.set_ylim(0, 112)
    ax.set_ylabel("通过率 (%)")
    ax.set_title("Standard vs Strict 通过率（按 Strict 降序）", fontsize=14, pad=34)
    ax.legend(frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    outputs.append(out_dir / "01_pass_rate_bar.png")
    save_figure(plt, fig, outputs[-1])

    critical_order = sorted(reports, key=lambda item: item["critical"])
    critical_labels = [item["display"] for item in critical_order]
    critical_values = [item["critical"] for item in critical_order]
    fig, ax = plt.subplots(figsize=(10, 6))
    bars = ax.barh(critical_labels, critical_values, color=RED, height=0.62)
    for bar in bars:
        ax.annotate(str(int(bar.get_width())), (bar.get_width(), bar.get_y() + bar.get_height() / 2),
                    va="center", ha="left", fontsize=9, color="#7F1D1D", xytext=(4, 0), textcoords="offset points")
    ax.set_xlabel("Critical 失败用例数")
    ax.set_title("各模型 Critical 漏洞数", fontsize=14, pad=14)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    outputs.append(out_dir / "02_critical_failures_bar.png")
    save_figure(plt, fig, outputs[-1])

    timeline = sorted(reports, key=sort_stamp)
    tick_labels = [f'{sort_stamp(item)[5:16]}\n{compact_name(item)}' for item in timeline]
    strict_series = [item["strict_rate"] for item in timeline]
    critical_series = [item["critical"] for item in timeline]
    ax_positions = np.arange(len(timeline))
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(ax_positions, strict_series, marker="o", color=BLUE, linewidth=2, label="Strict 通过率")
    for x, y in zip(ax_positions, strict_series):
        ax.annotate(f"{y:.1f}%", (x, y), textcoords="offset points", xytext=(0, 8), ha="center", fontsize=8, color=BLUE)
    ax.axhline(80, color=AMBER, linestyle="--", linewidth=1.2, label="SLO 80%")
    ax.set_xticks(ax_positions)
    ax.set_xticklabels(tick_labels, fontsize=8)
    ax.set_ylim(0, 105)
    ax.set_ylabel("Strict 通过率 (%)")
    ax.spines[["top"]].set_visible(False)
    ax2 = ax.twinx()
    ax2.plot(ax_positions, critical_series, marker="s", color=RED, linewidth=1.5, linestyle="--", label="Critical 数")
    ax2.set_ylabel("Critical 失败用例数", color=RED)
    ax2.tick_params(axis="y", colors=RED)
    lines, legend_labels = ax.get_legend_handles_labels()
    lines2, legend_labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, legend_labels + legend_labels2, frameon=False,
              loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3)
    ax.set_title("严格通过率与 Critical 漏洞的时间趋势", fontsize=14, pad=34)
    fig.tight_layout()
    outputs.append(out_dir / "03_strict_rate_timeline.png")
    save_figure(plt, fig, outputs[-1])

    for filename, data, title in (
        ("04_fail_reason_pie.png", aggregates["fail_reasons"], "错因分布（全部报告汇总）"),
        ("05_category_failure_pie.png", aggregates["category_failures"], "失败类别分布（全部报告汇总）"),
    ):
        keys = list(data.keys())
        values = list(data.values())
        fig, ax = plt.subplots(figsize=(10, 6))
        wedges, _ = ax.pie(
            values,
            colors=PIE_COLORS[:len(values)],
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 0.42, "edgecolor": "white"},
        )
        total = sum(values) or 1
        legend_labels = [f"{key}  {value} ({value / total * 100:.1f}%)" for key, value in zip(keys, values)]
        ax.legend(wedges, legend_labels, loc="center left", bbox_to_anchor=(1.02, 0.5), frameon=False, fontsize=9)
        ax.set_title(title, fontsize=14, pad=14)
        ax.axis("equal")
        fig.tight_layout()
        outputs.append(out_dir / filename)
        save_figure(plt, fig, outputs[-1])

    layer_names = [name for name in JUDGE_LAYER_ORDER if aggregates["judge_layers"].get(name)]
    layer_reports = sorted(reports, key=lambda item: item["strict_rate"], reverse=True)
    layer_labels = [item["display"] for item in layer_reports]
    layer_positions = np.arange(len(layer_reports))
    fig, ax = plt.subplots(figsize=(11, 6))
    bottom = np.zeros(len(layer_reports))
    for name in layer_names:
        values = np.array([item["judge_layers"].get(name, 0) for item in layer_reports])
        ax.bar(layer_positions, values, bottom=bottom, label=name, color=JUDGE_LAYER_COLORS.get(name, "#94A3B8"), width=0.62)
        bottom += values
    ax.set_xticks(layer_positions)
    ax.set_xticklabels(layer_labels, rotation=25, ha="right", fontsize=9)
    ax.set_ylabel("判定用例数")
    ax.set_title("裁判层级分布（local_fallback 为关键词兜底）", fontsize=14, pad=14)
    ax.legend(frameon=False, ncol=len(layer_names), loc="upper center", bbox_to_anchor=(0.5, -0.18))
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    outputs.append(out_dir / "06_judge_layers_stacked_bar.png")
    save_figure(plt, fig, outputs[-1])

    venn_rows = build_venn(reports)
    if venn_rows:
        outputs.append(render_venn_png(plt, venn_rows, out_dir))

    return outputs


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent-Quality-Gate 评测总览</title>
<script src="__ECHARTS_SRC__"></script>
<style>
  :root {
    --blue: #2E5BFF;
    --red: #E5484D;
    --amber: #F5A524;
    --ink: #0F172A;
    --muted: #64748B;
    --line: #E2E8F0;
    --bg: #F4F6FB;
    --card: #FFFFFF;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  }
  .page { max-width: 1440px; margin: 0 auto; padding: 32px 28px 48px; }
  header h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: 0.2px; }
  header .sub { margin: 0; color: var(--muted); font-size: 13px; }
  .kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin: 24px 0 16px; }
  .kpi { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; }
  .kpi .label { color: var(--muted); font-size: 12px; margin-bottom: 10px; }
  .kpi .value { font-size: 30px; font-weight: 700; line-height: 1; }
  .kpi .value small { font-size: 14px; font-weight: 500; color: var(--muted); margin-left: 4px; }
  .kpi .note { margin-top: 8px; font-size: 12px; color: var(--muted); min-height: 16px; }
  .warnings { display: grid; gap: 10px; margin-bottom: 20px; }
  .warning {
    background: #FFF8E6; border: 1px solid #F7DFA5; color: #6B4E00;
    border-radius: 12px; padding: 12px 16px; font-size: 13px; line-height: 1.6;
  }
  .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 18px 20px 12px; }
  .card h2 { margin: 0 0 6px; font-size: 15px; font-weight: 600; }
  .card.wide { grid-column: span 2; }
  .chart { width: 100%; height: 360px; }
  .chart.tall { height: 400px; }
  .table-card { margin-top: 20px; }
  .table-wrap { overflow-x: auto; margin-top: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; background: #F8FAFC; }
  td.num { font-variant-numeric: tabular-nums; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; line-height: 18px; }
  .badge.blocked { background: #FDE8E8; color: #B42318; }
  .badge.passed { background: #E7F8F0; color: #067647; }
  .badge.warn { background: #FFF4E0; color: #B54708; }
  .venn-card { background: #0B1220; border-color: #1E293B; }
  .venn-card h2 { color: #E2E8F0; }
  .venn-board { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-top: 12px; }
  .venn-item { text-align: center; }
  .venn-svg { width: 100%; height: auto; isolation: isolate; background: #0B1220; border-radius: 10px; }
  .venn-svg circle { mix-blend-mode: screen; }
  .venn-name { color: #E2E8F0; font-size: 12px; font-weight: 600; margin-top: 6px; }
  .venn-scores { color: #94A3B8; font-size: 11px; margin-top: 2px; }
  .venn-hint { color: #F5A524; font-size: 11px; margin-top: 2px; }
  .venn-hint.danger { color: #E5484D; }
  .venn-note { color: #94A3B8; font-size: 12px; margin: 14px 0 2px; line-height: 1.6; }
  footer { margin-top: 24px; color: var(--muted); font-size: 12px; }
  @media (max-width: 980px) {
    .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .grid { grid-template-columns: 1fr; }
    .card.wide { grid-column: span 1; }
    .venn-board { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
</style>
</head>
<body>
<div class="page">
  <header>
    <h1>Agent-Quality-Gate 评测总览</h1>
    <p class="sub">数据源 reports/runs/*/summary.json（缺失时回退 reports/*.md）· 生成于 <span id="generated-at"></span> · 共 <span id="report-count"></span> 份报告</p>
  </header>
  <section class="kpis" id="kpis"></section>
  <section class="warnings" id="warnings"></section>
  <section class="grid">
    <article class="card wide venn-card">
      <h2>三原色图板（红 = 安全 · 绿 = 合规 · 蓝 = 可信）</h2>
      <div class="venn-board" id="venn-board"></div>
      <p class="venn-note">圆的亮度等于该维度得分，三圆叠加处按 RGB 加色混合：中心越白三项越全达标，偏色即短板。可信率低于 90% 的场次标红，不参与排名结论。</p>
    </article>
    <article class="card wide"><h2>Standard vs Strict 通过率</h2><div id="chart-pass" class="chart"></div></article>
    <article class="card"><h2>Critical 漏洞数</h2><div id="chart-critical" class="chart"></div></article>
    <article class="card"><h2>错因分布（全部报告汇总）</h2><div id="chart-fail" class="chart"></div></article>
    <article class="card wide"><h2>严格通过率与 Critical 漏洞的时间趋势</h2><div id="chart-timeline" class="chart tall"></div></article>
    <article class="card"><h2>失败类别分布（全部报告汇总）</h2><div id="chart-category" class="chart"></div></article>
    <article class="card"><h2>裁判层级分布</h2><div id="chart-judge" class="chart"></div></article>
  </section>
  <section class="card table-card">
    <h2>报告明细</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>报告文件</th><th>模型</th><th>模式</th><th>裁判</th><th>运行开始</th><th>样本</th>
            <th>Standard</th><th>Strict</th><th>Critical</th><th>门禁</th><th>local_fallback</th>
          </tr>
        </thead>
        <tbody id="report-rows"></tbody>
      </table>
    </div>
  </section>
  <footer>本页由 scripts/visualize_reports.py 自动生成 · Standard 衡量最终危害，Strict 衡量全过程合规，门禁以 Strict 为准</footer>
</div>
<script>
const DATA = __DATA__;
const REPORTS = DATA.reports || [];
const COLORS = ["#2E5BFF", "#2EC4B6", "#7C6FEA", "#F5A524", "#E5484D", "#6B7280", "#E879F9", "#84CC16"];
const JUDGE_COLORS = { rule: "#2EC4B6", trajectory: "#7C6FEA", jcv_audit: "#F5A524", llm: "#2E5BFF", local_fallback: "#E5484D" };
const JUDGE_ORDER = ["rule", "trajectory", "jcv_audit", "llm", "local_fallback"];

function shortLabel(name) {
  return String(name)
    .replace("DeepSeek-V4.1-Flash-1017", "DeepSeek-V4.1")
    .replace("微信小程序", "wx·")
    .slice(0, 14);
}

function esc(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

// 排序与时间轴用真实运行开始时间：generated_at 是报告重生成那一刻，
// 重判一次就会把不同日期的批次压到同一刻。
function stampOf(item) { return item.started_at || item.generated_at || ""; }

function chart(id) {
  if (!window.echarts) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '<p style="color:#B42318;padding:16px">ECharts 加载失败，请检查网络或同目录下的 echarts.min.js</p>';
    return null;
  }
  return echarts.init(document.getElementById(id));
}

function renderHeader() {
  document.getElementById("generated-at").textContent = DATA.generated_at;
  document.getElementById("report-count").textContent = REPORTS.length;
}

function renderKpis() {
  const count = REPORTS.length;
  const avg = count ? REPORTS.reduce((sum, item) => sum + item.strict_rate, 0) / count : 0;
  const best = count ? REPORTS.reduce((a, b) => (a.strict_rate >= b.strict_rate ? a : b)) : null;
  const criticalTotal = REPORTS.reduce((sum, item) => sum + item.critical, 0);
  const cards = [
    { label: "报告数", value: count, unit: "份", note: "优先读取 runs/*/summary.json" },
    { label: "平均 Strict 通过率", value: avg.toFixed(2), unit: "%", note: "仅统计可解析报告" },
    { label: "最高 Strict 通过率", value: best ? best.strict_rate.toFixed(2) : "-", unit: "%", note: best ? best.display : "" },
    { label: "Critical 漏洞合计", value: criticalTotal, unit: "个", note: "所有报告累计" }
  ];
  document.getElementById("kpis").innerHTML = cards.map(card => `
    <div class="kpi">
      <div class="label">${esc(card.label)}</div>
      <div class="value">${esc(card.value)}<small>${esc(card.unit)}</small></div>
      <div class="note">${esc(card.note)}</div>
    </div>`).join("");
}

function renderWarnings() {
  const box = document.getElementById("warnings");
  if (!DATA.warnings || !DATA.warnings.length) { box.style.display = "none"; return; }
  box.innerHTML = DATA.warnings.map(text => `<div class="warning">${esc(text)}</div>`).join("");
}

function renderPassChart() {
  const instance = chart("chart-pass");
  if (!instance) return;
  const ordered = [...REPORTS].sort((a, b) => b.strict_rate - a.strict_rate);
  instance.setOption({
    color: ["#C7CEDB", "#2E5BFF"],
    tooltip: { trigger: "axis", valueFormatter: value => value + "%" },
    legend: { top: 0 },
    grid: { left: 48, right: 24, top: 44, bottom: 92 },
    xAxis: { type: "category", data: ordered.map(item => item.display), axisLabel: { rotate: 28, interval: 0, fontSize: 11 } },
    yAxis: { type: "value", max: 100, name: "通过率 (%)", axisLabel: { formatter: "{value}%" } },
    series: [
      { name: "Standard", type: "bar", data: ordered.map(item => item.std_rate), barMaxWidth: 28, label: { show: true, position: "top", formatter: "{c}%", fontSize: 10 } },
      {
        name: "Strict", type: "bar", data: ordered.map(item => item.strict_rate), barMaxWidth: 28,
        label: { show: true, position: "top", formatter: "{c}%", fontSize: 10 },
        markLine: { symbol: "none", lineStyle: { color: "#F5A524", type: "dashed" }, data: [{ yAxis: 80, label: { formatter: "SLO 80%", position: "insideEndTop" } }] }
      }
    ]
  });
}

function renderCriticalChart() {
  const instance = chart("chart-critical");
  if (!instance) return;
  const ordered = [...REPORTS].sort((a, b) => a.critical - b.critical);
  instance.setOption({
    color: ["#E5484D"],
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 24, right: 40, top: 20, bottom: 32, containLabel: true },
    xAxis: { type: "value", name: "个" },
    yAxis: { type: "category", data: ordered.map(item => item.display), axisLabel: { fontSize: 11 } },
    series: [{ type: "bar", data: ordered.map(item => item.critical), barMaxWidth: 22, label: { show: true, position: "right", fontSize: 10 } }]
  });
}

function renderFailPie() {
  const instance = chart("chart-fail");
  if (!instance) return;
  const data = Object.entries(DATA.aggregates.fail_reasons || {}).map(([name, value]) => ({ name, value }));
  instance.setOption({
    color: COLORS,
    tooltip: { trigger: "item", formatter: "{b}<br/>{c} 次 ({d}%)" },
    legend: { type: "plain", bottom: 0, left: "center", width: "92%", itemWidth: 10, itemHeight: 10, itemGap: 14, icon: "circle", textStyle: { fontSize: 11, color: "#475569" } },
    series: [{
      type: "pie", radius: ["34%", "58%"], center: ["50%", "40%"], data,
      label: { show: false }, emphasis: { label: { show: true, formatter: "{b}\\n{c} ({d}%)", fontWeight: "bold" } }
    }]
  });
}

function renderTimeline() {
  const instance = chart("chart-timeline");
  if (!instance) return;
  const ordered = [...REPORTS].sort((a, b) => stampOf(a).localeCompare(stampOf(b)));
  const labels = ordered.map(item => stampOf(item).slice(5, 16) + "\\n" + shortLabel(item.display));
  instance.setOption({
    color: ["#2E5BFF", "#E5484D"],
    tooltip: { trigger: "axis" },
    legend: { top: 0 },
    grid: { left: 56, right: 56, top: 44, bottom: 92 },
    xAxis: { type: "category", data: labels, axisLabel: { interval: 0, rotate: 20, fontSize: 9, lineHeight: 12, margin: 14 } },
    yAxis: [
      { type: "value", name: "Strict (%)", min: 0, max: 100, axisLabel: { formatter: "{value}%" } },
      { type: "value", name: "Critical" }
    ],
    series: [
      {
        name: "Strict 通过率", type: "line", data: ordered.map(item => item.strict_rate),
        smooth: true, symbolSize: 8, label: { show: true, formatter: "{c}%", position: "top", fontSize: 10 },
        markLine: { symbol: "none", lineStyle: { color: "#F5A524", type: "dashed" }, data: [{ yAxis: 80 }] }
      },
      { name: "Critical 数", type: "line", yAxisIndex: 1, data: ordered.map(item => item.critical), symbol: "square", symbolSize: 7, lineStyle: { type: "dashed" } }
    ],
    media: [{
      query: { maxWidth: 700 },
      option: {
        grid: { bottom: 110 },
        xAxis: { axisLabel: { rotate: 35, fontSize: 8, lineHeight: 11 } }
      }
    }]
  });
}

function renderCategoryPie() {
  const instance = chart("chart-category");
  if (!instance) return;
  const data = Object.entries(DATA.aggregates.category_failures || {}).map(([name, value]) => ({ name, value }));
  instance.setOption({
    color: COLORS,
    tooltip: { trigger: "item", formatter: "{b}<br/>{c} 条 ({d}%)" },
    legend: { type: "plain", bottom: 0, left: "center", width: "92%", itemWidth: 10, itemHeight: 10, itemGap: 14, icon: "circle", textStyle: { fontSize: 11, color: "#475569" } },
    series: [{
      type: "pie", radius: ["34%", "58%"], center: ["50%", "40%"], data,
      label: { show: false }, emphasis: { label: { show: true, formatter: "{b}\\n{c} ({d}%)", fontWeight: "bold" } }
    }]
  });
}

function renderJudgeChart() {
  const instance = chart("chart-judge");
  if (!instance) return;
  const ordered = [...REPORTS].sort((a, b) => b.strict_rate - a.strict_rate);
  const layers = JUDGE_ORDER.filter(name => ordered.some(item => (item.judge_layers || {})[name]));
  instance.setOption({
    color: layers.map(name => JUDGE_COLORS[name]),
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    legend: { top: 0 },
    grid: { left: 24, right: 24, top: 44, bottom: 76, containLabel: true },
    xAxis: { type: "category", data: ordered.map(item => item.display), axisLabel: { rotate: 28, interval: 0, fontSize: 11 } },
    yAxis: { type: "value", name: "判定用例数" },
    series: layers.map(name => ({
      name, type: "bar", stack: "judge", barMaxWidth: 34,
      data: ordered.map(item => (item.judge_layers || {})[name] || 0)
    }))
  });
}

function gateBadge(item) {
  const status = item.gate_status || (item.blocked ? "blocked" : "passed");
  const labels = { blocked: "拦截", warn: "警告", passed: "放行", shadow: "影子" };
  const classes = { blocked: "blocked", warn: "warn", passed: "passed", shadow: "warn" };
  return `<span class="badge ${classes[status] || "passed"}">${labels[status] || esc(status)}</span>`;
}

function renderVenn() {
  const board = document.getElementById("venn-board");
  if (!board) return;
  const rows = DATA.venn || [];
  if (!rows.length) {
    board.innerHTML = "";
    return;
  }
  const names = { safety: "安全", compliance: "合规", trust: "可信" };
  const gamma = 1.6;
  const circle = (cx, cy, r, color, score) =>
    `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${color}" style="opacity:${Math.pow(score, gamma).toFixed(3)}"></circle>`;
  board.innerHTML = rows.map(row => {
    const s = row.scores;
    let hint = '<div class="venn-hint">三项均衡</div>';
    if (!row.reliable) {
      hint = '<div class="venn-hint danger">数据不可信，结论不成立</div>';
    } else if (row.weak) {
      hint = `<div class="venn-hint">短板：${names[row.weak]} ${(s[row.weak] * 100).toFixed(1)}%</div>`;
    }
    return `<div class="venn-item">
      <svg class="venn-svg" viewBox="0 0 420 420">
        ${circle(150, 235, 142, "#FF0000", s.safety)}
        ${circle(210, 135, 142, "#00FF00", s.compliance)}
        ${circle(270, 235, 142, "#0000FF", s.trust)}
      </svg>
      <div class="venn-name">${esc(row.display)}</div>
      <div class="venn-scores">安全 ${(s.safety * 100).toFixed(1)} · 合规 ${(s.compliance * 100).toFixed(1)} · 可信 ${(s.trust * 100).toFixed(1)}</div>
      ${hint}
    </div>`;
  }).join("");
}

function renderTable() {
  const rows = [...REPORTS].sort((a, b) => stampOf(b).localeCompare(stampOf(a))).map(item => {
    const fallback = item.fallback > 0
      ? `<span class="badge warn">${item.fallback} (${item.fallback_rate.toFixed(1)}%)</span>`
      : '<span class="badge passed">0</span>';
    const ci = item.ci_high || item.ci_low
      ? ` title="Strict 95% CI: [${item.ci_low.toFixed(2)}%, ${item.ci_high.toFixed(2)}%]"`
      : "";
    const sample = item.population && item.population !== item.sample_size
      ? `${item.sample_size}/${item.population}`
      : String(item.sample_size || item.total);
    return `<tr>
      <td>${esc(item.file)}</td>
      <td>${esc(item.display)}</td>
      <td>${esc(item.mode || "-")}</td>
      <td>${esc(item.judge_model || "-")}</td>
      <td class="num">${esc(stampOf(item))}</td>
      <td class="num">${sample}</td>
      <td class="num">${item.std_rate.toFixed(2)}%</td>
      <td class="num"${ci}>${item.strict_rate.toFixed(2)}%</td>
      <td class="num">${item.critical}</td>
      <td>${gateBadge(item)}</td>
      <td>${fallback}</td>
    </tr>`;
  });
  document.getElementById("report-rows").innerHTML = rows.join("");
}

renderHeader();
renderKpis();
renderWarnings();
renderPassChart();
renderCriticalChart();
renderFailPie();
renderTimeline();
renderCategoryPie();
renderJudgeChart();
renderTable();
renderVenn();
window.addEventListener("resize", () => {
  ["chart-pass", "chart-critical", "chart-fail", "chart-timeline", "chart-category", "chart-judge"].forEach(id => {
    const el = document.getElementById(id);
    if (el && window.echarts) {
      const instance = echarts.getInstanceByDom(el);
      if (instance) instance.resize();
    }
  });
});
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="解析 Agent-Quality-Gate 报告并生成 HTML/PNG 图表")
    parser.add_argument("--reports-dir", default="reports", help="报告目录，默认 reports")
    parser.add_argument("--output-dir", default="reports/charts", help="图表输出目录，默认 reports/charts")
    parser.add_argument("--include-selfcheck", action="store_true", help="把 verify-* 自检运行也画进图表（默认排除）")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    out_dir = Path(args.output_dir)
    if not reports_dir.is_dir():
        print(f"报告目录不存在: {reports_dir}")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    reports, skipped = load_reports(reports_dir, include_selfcheck=args.include_selfcheck)
    if not reports:
        print("没有解析到可用的新格式报告。")
        return 1
    payload = build_payload(reports, skipped)

    html_path = render_html(payload, out_dir)
    print(f"已解析 {len(reports)} 份报告，跳过 {len(skipped)} 份")
    for item in skipped:
        print(f"  跳过 {item['file']}: {item['reason']}")
    print(f"HTML: {html_path}")

    try:
        png_paths = render_pngs(reports, payload["aggregates"], out_dir)
    except ImportError:
        print("未安装 matplotlib，跳过 PNG 输出。安装命令: pip install matplotlib")
        return 0
    for path in png_paths:
        print(f"PNG : {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
