#!/usr/bin/env python3
"""T32 跨窗口协议的可执行入口（纯本地、零 API、零写入，除非显式 --out）。

五个子命令：
    propose   从历史批次的落盘证据生成窗口 manifest（每题首次被评测的日期）
    validate  硬校验 manifest（子集 / 序号连续 / 新增集不相交 / 时点单调）
    plan      输出某个窗口该喂的题集与增量清单（未来窗口用例一律不出现）
    view      把语料原文裁成某窗口的喂题视图（默认落 workspace/，字节保真）
    delta     用两场的 results.jsonl 算 ΔJ，并用实测噪声带判"是否超出抖动"

为什么 cutoff 只能来自落盘证据：207 条语料没有任何时间字段，git 历史仅 8 次且经
11-17 私料清除重写——没有任何可靠依据能自动确定每题的**入库**时点。所以本脚本用
每题**首次被评测**的批次日期（从 reports/runs 与归档目录名日期复算）作为窗口依据：
它是可观测、可重算的下界时点，而不是对历史的猜测。从未在任何历史批次出现过的题
一律不进窗口，在 manifest 里列为 `unassigned`——没有证据就不替它编时间。

用法示例：
    python scripts/cross_window.py propose --out cases/windows/manifest.json
    python scripts/cross_window.py validate --manifest cases/windows/manifest.json
    python scripts/cross_window.py plan     --manifest cases/windows/manifest.json --window 1 --ids
    python scripts/cross_window.py view     --manifest cases/windows/manifest.json --window 0 \\
        --out workspace/windows/window-0.jsonl          # 真跑窗口 0 需要它（直接喂全量语料会被拒）
    python scripts/cross_window.py delta    --manifest cases/windows/manifest.json --window 1 \\
        --baseline-run reports/runs/<窗口0的run> --current-run reports/runs/<窗口1的run>
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.engine.dataset import case_ids, read_jsonl
from src.engine.noise_calibration import flip_band_pp
from src.engine.runload import (
    case_bits_from_run,
    case_final_observations,
    case_id_of,
    iter_result_rows,
)
from src.engine.window import (
    ALL_SAMPLES,
    DEFAULT_METRIC,
    ENVIRONMENT_EXCLUDED,
    PROTOCOL_ID,
    WindowProtocolError,
    build_window_view,
    delta_j,
    increment_ids,
    select_window_cases,
    validate_manifest,
    visible_ids,
)

CORPUS = "cases/core_suite.jsonl"
ROOT = Path(__file__).resolve().parent.parent
RUN_GLOBS = (
    "reports/runs/*",
    "workspace/*",
    ".reports-archive-*/runs/*",
    ".workspace-archive-*/*",
)
_STAMP_RE = re.compile(r"(20\d{6})")
DEFAULT_MIN_GAP_DAYS = 7


def _day_of(name: str) -> str | None:
    """从目录名里取批次日期（YYYYMMDD）。取不到、或只是“形状像”而不是合法日期，都当无时间证据。

    模型名/实验名里偶然的 8 位数字（如版本号、13 月 99 日）不能当成入库时点，
    否则会造出一个不存在的窗口，而整个时间切片就建在一个解析错误上。
    """
    match = _STAMP_RE.search(Path(name).name)
    if not match:
        return None
    day = match.group(1)
    try:
        _as_date(day)
    except ValueError:
        return None
    return day


def _as_date(day: str) -> date:
    return date(int(day[:4]), int(day[4:6]), int(day[6:8]))


def scan_first_seen(corpus_ids: set[str], root: Path) -> tuple[dict[str, str], int]:
    """每条题**首次被真实评测**的批次日期：{case_id: YYYYMMDD}。

    口径必须说清：这是「首次出现在落盘结果里」的日期，**不是入库日期**——题目可能
    入库后隔几天才被跑。作为窗口划分依据，它是一个可复算的**下界时点**：
    窗口 N 的题目在该 cutoff 前一定已经存在。把下界当准点就是伪造事实。

    返回 (first_seen, 扫过的 run 场数)。任何外部 agent 拿同一仓产物重跑应该得到同一结果。
    """
    first_seen: dict[str, str] = {}
    scanned = 0
    for pattern in RUN_GLOBS:
        for run_dir in sorted(glob.glob(str(root / pattern))):
            results = Path(run_dir) / "results.jsonl"
            if not results.is_file():
                continue
            day = _day_of(run_dir)
            if not day:
                continue
            scanned += 1
            for record, bad in iter_result_rows(results):
                if bad:
                    continue
                case_id = case_id_of(record)
                if case_id not in corpus_ids:
                    continue
                if case_id not in first_seen or day < first_seen[case_id]:
                    first_seen[case_id] = day
    return first_seen, scanned


def cluster_windows(first_seen: dict[str, str], min_gap_days: int) -> list[list[str]]:
    """按首次评测日期聚簇：相邻日差 ≥ min_gap_days 就开新窗口。

    不预先把窗口数写死：题目实际是分几批进来的，就只能按实际跳出的跳变切。
    """
    days = sorted(set(first_seen.values()), key=_as_date)
    if not days:
        return []
    groups: list[list[str]] = [[days[0]]]
    for day in days[1:]:
        if (_as_date(day) - _as_date(groups[-1][-1])).days >= min_gap_days:
            groups.append([])
        groups[-1].append(day)
    return groups


def build_manifest_from_evidence(
    rows: list[dict], first_seen: dict[str, str], min_gap_days: int
) -> tuple[dict, list[str]]:
    """从首次评测证据构造窗口 manifest。返回 (manifest, 无证据题号)。"""
    src_of = {str(row.get("id")): str(row.get("source") or "?") for row in rows}
    groups = cluster_windows(first_seen, min_gap_days)
    unassigned = sorted(case for case in src_of if case not in first_seen)
    windows = []
    for number, days in enumerate(groups):
        members = sorted(case for case, day in first_seen.items() if day in set(days))
        windows.append(
            {
                "window": number,
                # cutoff 取簇内最晚日：保证窗口内每题在 cutoff 前都已经可观测。
                "cutoff": f"{days[-1][:4]}-{days[-1][4:6]}-{days[-1][6:8]}",
                "declared": members,
                "observed_days": days,
            }
        )
    manifest = {
        "protocol": PROTOCOL_ID,
        "corpus": CORPUS,
        "metric": DEFAULT_METRIC,
        "min_gap_days": min_gap_days,
        "basis": (
            "各题首次被真实评测的批次日期（从 reports/runs 与归档的 results.jsonl 目录名日期复算）；"
            "它是入库时点的下界，不是准点"
        ),
        "verified": True,
        "windows": windows,
    }
    if unassigned:
        manifest["unassigned"] = unassigned
    return manifest, unassigned


def _load_rows(path: str) -> list[dict]:
    records, _ = read_jsonl(path)
    return records


def cmd_propose(args: argparse.Namespace) -> int:
    rows = _load_rows(args.corpus)
    ids = {str(row.get("id")) for row in rows}
    first_seen, scanned = scan_first_seen(ids, ROOT)
    print(f"扫描落盘批次 {scanned} 场，取到 {len(first_seen)}/{len(ids)} 题的首次评测日期")
    manifest, unassigned = build_manifest_from_evidence(rows, first_seen, args.min_gap_days)
    if unassigned:
        print(
            f"⚠ {len(unassigned)} 条从未在任何历史批次出现过，**不进任何窗口**"
            f"（样例 {unassigned[:3]}）：没有观测证据就不能替它编时点"
        )
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"窗口 manifest 已写: {args.out}")
    else:
        print(text)
    for window in manifest["windows"]:
        print(
            f"  窗口 {window['window']}: cutoff={window['cutoff']} "
            f"新增={len(window['declared'])} 观测日={','.join(window['observed_days'])}"
        )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    audit = validate_manifest(manifest, case_ids(args.corpus))
    print(
        f"✅ 协议校验通过 | protocol={audit['protocol']} metric={audit['metric']} "
        f"窗口数={audit['windows']} 语料={audit['corpus_ids']} 已声明={audit['declared_ids']} "
        f"覆盖={audit['covered_ids']}"
    )
    if not manifest.get("verified", False):
        print("⚠ manifest 标了 verified=false：cutoff 未经确认，可校验但不得用于对外结论")
    for window in manifest["windows"]:
        number = window["window"]
        print(
            f"  窗口 {number}: cutoff={window.get('cutoff') or '未填'} "
            f"新增={len(increment_ids(manifest, number))} 可见={len(visible_ids(manifest, number))}"
        )
    uncovered = set(case_ids(args.corpus)) - {
        str(value) for window in manifest["windows"] for value in window.get("declared") or []
    }
    if uncovered:
        print(f"  语料中未被任何窗口声明: {len(uncovered)} 条（不报错，但覆盖率要在报告里如实写明）")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    validate_manifest(manifest, case_ids(args.corpus))
    rows = _load_rows(args.corpus)
    # plan 的职责是“从全量语料裁出合规喂题集”，所以这里不拦——裁掉未来题就是它的正常工作。
    # 真正的夹带检测发生在 run_eval 接线处（enforce=True）：题集已经选定后还含未来题，
    # 那才是协议被违反，必须失败关闭而不是默默少跑几道。
    selected, audit = select_window_cases(rows, manifest, args.window, enforce=False)
    novel = set(increment_ids(manifest, args.window))
    print(
        f"窗口 {args.window} 计划 | 待跑 {audit['selected']} 条 "
        f"（其中新增 {sum(1 for row in selected if str(row.get('id')) in novel)} 条） | "
        f"被隔离的未来题 {audit['dropped']} 条"
    )
    if args.ids:
        for row in selected:
            mark = "+" if str(row.get("id")) in novel else " "
            print(f"  {mark} {row.get('id')}")
    print("（本命令不调任何 API；真正跑批用 run_eval.py --window-manifest ... --window N）")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    rows, audit = build_window_view(args.corpus, manifest, args.window)

    out = Path(args.out) if args.out else ROOT / "workspace" / "windows" / f"window-{args.window}.jsonl"
    if not out.is_absolute():
        out = ROOT / out
    # 视图不是语料：写进 cases/ 就会多出一个可入库的 dataset 文件，下一场用错文件
    # 就是一次真的换指纹。视图只允许落运行产物区（workspace/），由 .gitignore 隔住。
    inside_cases = ROOT / "cases" in out.parents
    if inside_cases:
        print(f"❌ 拒写视图到 cases/ 下: {out}（视图是喂题工具，不是数据集；落 workspace/）")
        return 1
    if out.exists() and not args.force:
        print(f"❌ {out} 已存在，未覆盖（加 --force 才重生成）")
        print("   视图是可以被复核的产物：静默覆盖会让“上一轮究竟喂了什么”失去可比性")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(rows)
    if not text.endswith("\n"):
        text += "\n"
    out.write_text(text, encoding="utf-8")

    corpus_digest = hashlib.sha256(Path(args.corpus).read_bytes()).hexdigest()[:16]
    view_digest = hashlib.sha256(out.read_bytes()).hexdigest()[:16]
    print(
        f"✅ 窗口 {args.window} 视图 | 写出 {audit['emitted']}/{audit['visible']} 可见题 | "
        f"被隔离的未来题 {audit['blocked']} 条"
    )
    print(f"   语料 {corpus_digest}（未动）| 视图 {view_digest} | {out}")
    print(
        f"   下一步: python scripts/run_eval.py --dataset {out} "
        f"--window-manifest {args.manifest} --window {args.window} --limit 0 --repeat 3"
    )
    print("   （视图只引用语料子集，代际会自动锚回语料 epoch，不会误触切代）")
    return 0


def cmd_delta(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    validate_manifest(manifest, case_ids(args.corpus))
    # ΔJ 用合并后的最终结论，判定维度同样取 Strict；逐次 attempts 只服务于噪声标定。
    # 分母不冒充发布门禁 strict_rate：后者始终剔除 precondition_failed，T50 两口径另列。
    # 同一份 observation 同时供全样本与剔环境类口径使用，避免两套解析器给出不同结论。
    baseline_observations, baseline_audit = case_final_observations(Path(args.baseline_run))
    current_observations, current_audit = case_final_observations(Path(args.current_run))
    baseline_bits = {
        case_id: observation["strict_passed"]
        for case_id, observation in baseline_observations.items()
    }
    current_bits = {
        case_id: observation["strict_passed"]
        for case_id, observation in current_observations.items()
    }
    if not baseline_bits or not current_bits:
        print("❌ 任一场没有可读结果，ΔJ 不予计算（失败关闭）")
        return 1
    result = delta_j(
        baseline_bits, current_bits, manifest, args.window,
        baseline_observations=baseline_observations,
        current_observations=current_observations,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"基线场读取: {baseline_audit} | 当期场读取: {current_audit}")

    retained = result["delta_retained"]
    if retained is None or result["window"] == 0:
        print("\n窗口 0 是基线轮，或同题样本不足，无 ΔJ 可判。")
        return 0
    band = args.band_pp
    if band is None:
        # 没有实测带就用当期批的翻转率现推一个——但绝不静默，用户看得见它是临时算的。
        attempts_by_case, _ = case_bits_from_run(Path(args.current_run))
        flips, total = _flips_of(attempts_by_case)
        measured = flip_band_pp(flips, total)
        if not measured["available"]:
            # 无 repeat 数据就没有带宽：此时只能报数，不得下一个“超出/未超出”的判定。
            print("\n⚠ 当期场没有 repeat 数据，无噪声带可用：只报 ΔJ 数值，不作判定。")
            print("   没有带宽的 ΔJ 不能声称“增益”，也不能声称“未劣化”。")
            return 0
        band = measured["band_pp"]
        print(f"\n（未指定 --band-pp，用当期实测翻转率临时推得带宽 {band:.2f}pp）")
    profiles = result["denominator_profiles"]
    all_profile = profiles[ALL_SAMPLES]
    valid_profile = profiles[ENVIRONMENT_EXCLUDED]

    def _verdict(value: float) -> str:
        return "超出噪声带" if abs(value) > band else "差异未证实"

    print(
        f"\n跨窗口门禁主口径（全样本）ΔJ_retained = {retained:+.2f}pp"
        f"（基线/当期可计分 {all_profile['n_retained_baseline_scored']}/"
        f"{all_profile['n_retained_current_scored']} 条同题复测）"
        f" vs 噪声带 {band:.2f}pp → {_verdict(retained)}"
    )
    retained_valid = result["delta_retained_valid_only"]
    if retained_valid is None or not result["attribution_valid_only"]:
        print("能力归因口径（剔环境类）ΔJ_retained 不可算：两期没有共同的可信同题样本，不予判定。")
    else:
        print(
            f"能力归因口径（剔环境类）ΔJ_retained = {retained_valid:+.2f}pp"
            f"（基线/当期可计分 {valid_profile['n_retained_baseline_scored']}/"
            f"{valid_profile['n_retained_current_scored']} 条，两期共同 {valid_profile['n_retained_paired_scored']} 条）"
            f" vs 同一噪声带 {band:.2f}pp → {_verdict(retained_valid)}"
        )
    if result["delta_total"] is None:
        print("ΔJ_total 不可算（任一期无可用结论位），不予报数。")
    else:
        print(
            f"ΔJ_total = {result['delta_total']:+.2f}pp —— 含题目覆盖扩张，"
            "**不得**引用为能力增益（归因字段只有 delta_retained）"
        )
    if result["delta_total_valid_only"] is not None:
        print(
            f"ΔJ_total_valid_only = {result['delta_total_valid_only']:+.2f}pp"
            " —— 仍含题目覆盖扩张，同样**不得**引用为能力增益"
        )
    print("两种分母口径并列披露；全样本用于跨窗口门禁账面，剔环境类只用于能力归因，不互相替换。")
    return 0


def _flips_of(bits_by_case: dict) -> tuple[int, int]:
    """(翻转题数, 可测题数)。只有两期都有可用结论位的题才进分母。

    把“两期都是环境故障”的题算进分母会人为拉低翻转率、把带宽往窄了推，
    那是把“没测到”当成“很稳定”。
    """
    total = flips = 0
    for bits in bits_by_case.values():
        usable = [bit for bit in bits[:2] if bit is not None]
        if len(usable) < 2:
            continue
        total += 1
        if len(set(usable)) > 1:
            flips += 1
    return flips, total


def main() -> int:
    parser = argparse.ArgumentParser(description="T32 跨窗口协议入口")
    sub = parser.add_subparsers(dest="command", required=True)

    p_propose = sub.add_parser("propose", help="从历史批次落盘证据生成窗口 manifest")
    p_propose.add_argument("--corpus", default=CORPUS)
    p_propose.add_argument("--out", default="", help="不填则只打印")
    p_propose.add_argument(
        "--min-gap-days", type=int, default=DEFAULT_MIN_GAP_DAYS,
        help="首次评测日相差这么多天就开新窗口",
    )
    p_propose.set_defaults(func=cmd_propose)

    for name, help_text in (
        ("validate", "硬校验 manifest"),
        ("plan", "输出某窗口的喂题计划"),
        ("view", "生成某窗口的喂题视图（字节保真，默认落 workspace/）"),
        ("delta", "算跨窗口 ΔJ"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--manifest", required=True)
        item.add_argument("--corpus", default=CORPUS)
        if name != "validate":
            item.add_argument("--window", type=int, required=True)
        if name == "plan":
            item.add_argument("--ids", action="store_true", help="逐条打印题号，+ 为新增")
        if name == "view":
            item.add_argument("--out", default="", help="不填则写 workspace/windows/window-<N>.jsonl")
            item.add_argument("--force", action="store_true", help="允许覆盖已存在的视图")
        if name == "delta":
            item.add_argument("--baseline-run", required=True, help="上一窗口的 run 目录")
            item.add_argument("--current-run", required=True, help="本窗口的 run 目录")
            item.add_argument("--band-pp", type=float, default=None, help="噪声带（pp）；不填则现推")
        item.set_defaults(
            func={
                "validate": cmd_validate,
                "plan": cmd_plan,
                "view": cmd_view,
                "delta": cmd_delta,
            }[name]
        )

    args = parser.parse_args()
    try:
        return args.func(args)
    except WindowProtocolError as exc:
        print(f"❌ 跨窗口协议违规: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
