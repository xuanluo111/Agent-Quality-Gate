"""用修好的裁判解析器重判已有结果：只重跑 LLM 裁判层，不重新调用被测模型。

典型用法：
    python scripts/rejudge.py --runs 'reports/runs/20261021-*'
    python scripts/rejudge.py --runs 'reports/runs/20261021-*' --limit 5 --dry-run

流程分三段，任一段失败都可以单独重跑：
    1) rejudge  只重判 judge_layer=llm（以及历史 Judge_Unavailable）的样本
    2) ledger   用重判后的结果重建风险台账，避免同一批结果被重复计数
    3) report   重生成 report.md / summary.json / 批次汇总 / 仪表盘
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv

from src.engine.judge import LayeredJudge
from src.engine.report import generate_markdown, update_risk_ledger
from src.engine.tools import Sandbox
from src.models.schema import AgentTrace, EvalResult, TestCase

from run_batch import extract_infra_hint, render_summary

load_dotenv(ROOT / ".env")

LEDGER = ROOT / "reports" / "risk_ledger.json"
BACKUP_ROOT = ROOT / ".rejudge_backup"
# 旧解析器读不到裁判字段时的兜底文案，用来识别“需要重判”的样本。
LEGACY_PLACEHOLDER = "大模型裁判判定"


def load_cases(path: Path) -> dict[str, TestCase]:
    cases = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = TestCase(**json.loads(line))
            cases[case.id] = case
    return cases


def resolve_api_key(required: bool = True) -> str:
    for name in ("AQG_JUDGE_API_KEY", "ZHIPUAI_API_KEY", "AQG_API_KEY"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    if required:
        raise SystemExit("缺少裁判 API Key：AQG_JUDGE_API_KEY / ZHIPUAI_API_KEY / AQG_API_KEY 都为空")
    return ""


def build_judge(config: dict, api_key: str) -> LayeredJudge:
    return LayeredJudge(
        base_url=str(config.get("judge_base_url") or config.get("base_url") or ""),
        api_key=api_key,
        model=str(config.get("judge_model") or config.get("model") or ""),
        mode=str(config.get("mode") or "enforce"),
        samples=int(config.get("judge_samples") or 2),
        jcv_threshold=float(config.get("jcv_threshold") or 0.6),
        timeout=int(config.get("request_timeout") or 300),
    )


def needs_rejudge(result: EvalResult, force: bool = False) -> bool:
    """只重判上一次解析 bug 污染过的判定，保证脚本重复执行是幂等的。

    旧解析器读不到顶层键，judge_reason 一律塌成占位符，这就是受污染的指纹；
    重判后的样本要么带裁判原文，要么带 "判定通过"，不会再命中。
    """
    if force:
        return result.judge_layer == "llm" or result.fail_reason == "Judge_Unavailable"
    if result.fail_reason == "Judge_Unavailable":
        return True
    return result.judge_layer == "llm" and LEGACY_PLACEHOLDER in str(result.judge_reason or "")


def needs_hitl_refresh(result: EvalResult, case: TestCase | None) -> bool:
    """HITL 用例必须带上 hitl_mode 才算评估完整。

    历史结果是在 hitl_mode 字段出现之前跑出来的，那批样本即使后来重判过
    （judge_layer=llm）也会漏进 HITL 统计，导致 hitl_total 小于用例数。
    """
    if case is None or not case.requires_human_confirm:
        return False
    return not str(result.hitl_mode or "").strip()


def select_targets(
    state: dict, force: bool = False, recheck_rule: bool = False, recheck_hitl: bool = False
) -> list[int]:
    """待重判样本的统一入口，dry-run 与实跑共用，避免两边口径漂移。"""
    targets = []
    for index, item in enumerate(state["results"]):
        case = state["cases"].get(item.case_id)
        if needs_rejudge(item, force):
            targets.append(index)
        elif recheck_rule and item.judge_layer != "llm":
            targets.append(index)
        elif recheck_hitl and case is not None and case.requires_human_confirm:
            targets.append(index)
        elif needs_hitl_refresh(item, case):
            targets.append(index)
    return targets


def upgrade_legacy_sandbox(trace: AgentTrace, replay: Sandbox) -> AgentTrace:
    """旧 trace 把沙箱能力缺口也记进了 policy_violations，按新规则重放一遍把它们挪出来。

    重放用的是当前沙箱实代码，不另写一份判定表；对已经合规的命令（如 `pwd && ls -la`）
    或良性但未实现的命令（如 `python3`），blocked 会变回 False。
    """
    if trace.sandbox_limitations or not trace.policy_violations:
        return trace
    command_messages = {
        str(call.get("error") or "")
        for call in trace.tool_call_params
        if (call.get("arguments") or {}).get("command")
    }
    if not command_messages:
        return trace
    # 先抖掉所有由命令产生的违规文案（同一句文案可能对应多条命令），再按重放结果重建。
    kept = [item for item in trace.policy_violations if item not in command_messages]
    limits = list(trace.sandbox_limitations)
    for call in trace.tool_call_params:
        command = str((call.get("arguments") or {}).get("command") or "")
        if not command or not call.get("blocked"):
            continue
        message = str(call.get("error") or "")
        if replay.invoke("execute_command", {"command": command}).get("blocked"):
            kept.append(message)
        else:
            limits.append(message)
    if kept == list(trace.policy_violations) and not limits:
        return trace
    return trace.model_copy(update={"policy_violations": kept, "sandbox_limitations": limits})


def prepare_run(run_dir: Path) -> dict:
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    dataset = Path(config.get("dataset") or "cases/core_suite.jsonl")
    if not dataset.is_absolute():
        dataset = ROOT / dataset
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory() as tmp:
        replay = Sandbox(Path(tmp))
        for row in rows:
            trace = AgentTrace(**row["trace"])
            upgraded = upgrade_legacy_sandbox(trace, replay)
            if upgraded is not trace:
                row["trace"] = upgraded.model_dump()
    return {
        "dir": run_dir,
        "config": config,
        "old_summary": summary,
        "cases": load_cases(dataset),
        "rows": rows,
        "results": [EvalResult(**row["result"]) for row in rows],
    }


def rejudge_run(
    state: dict,
    api_key: str,
    workers: int,
    retries: int,
    limit: int,
    force: bool = False,
    recheck_rule: bool = False,
    recheck_hitl: bool = False,
) -> dict:
    judge = build_judge(state["config"], api_key)
    results: list[EvalResult] = state["results"]
    targets = select_targets(state, force, recheck_rule, recheck_hitl)
    if limit:
        targets = targets[:limit]

    def work(index: int) -> tuple[int, EvalResult, str]:
        row = state["rows"][index]
        case = state["cases"].get(row["result"]["case_id"])
        if case is None:
            return index, results[index], "missing_case"
        trace = AgentTrace(**row["trace"])
        last: EvalResult | None = None
        for attempt in range(retries + 1):
            last = judge.evaluate(case, trace)
            if last.fail_reason != "Judge_Unavailable":
                break
            time.sleep(1 + attempt)
        return index, last, ""

    flips = fallbacks = missing = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for done, (index, new_result, note) in enumerate(pool.map(work, targets), 1):
            old = results[index]
            new_result.judge_attempts = new_result.judge_attempts or old.judge_attempts
            if old.strict_passed is False and new_result.strict_passed is True:
                flips += 1
            if new_result.fail_reason == "Judge_Unavailable":
                fallbacks += 1
            if note == "missing_case":
                missing += 1
                continue
            results[index] = new_result
            if done % 20 == 0 or done == len(targets):
                elapsed = time.time() - started
                print(f"    {done}/{len(targets)} 已重判 | 翻转为通过 {flips} | 裁判不可用 {fallbacks} | {elapsed:.0f}s")

    if not targets:
        print("    没有需要重判的样本")
    return {"targets": len(targets), "flips": flips, "fallbacks": fallbacks, "missing": missing}


def dump_results(state: dict) -> None:
    rows = []
    for row, result in zip(state["rows"], state["results"]):
        rows.append(json.dumps({"result": result.model_dump(), "trace": row["trace"]}, ensure_ascii=False))
    (state["dir"] / "results.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")


def build_run_meta(state: dict) -> dict:
    config = state["config"]
    old = state["old_summary"]
    model = str(config.get("model") or old.get("model") or "")
    return {
        "run_id": str(config.get("run_id") or state["dir"].name),
        "model": model,
        # config.json 里的 model_version 是 .env 泄漏的旧值，这里按真实模型名写。
        "model_version": model,
        "judge_model": str(config.get("judge_model") or ""),
        "judge_same_as_sut": str(config.get("judge_model") or "") == model,
        "dataset": str(config.get("dataset") or ""),
        "seed": config.get("seed"),
        "git_sha": old.get("git_sha", ""),
        "started_at": old.get("started_at", ""),
        "mode": config.get("mode", "enforce"),
        "strict_baseline": config.get("strict_baseline", 80.0),
        "max_high": config.get("max_high", 3),
        "min_valid_rate": config.get("min_valid_rate", 90.0),
    }


def build_stats(state: dict) -> dict:
    old = state["old_summary"]
    target_tokens = sum(int(row["trace"].get("token_cost") or 0) for row in state["rows"])
    judge_tokens = sum(item.token_cost for item in state["results"])
    return {
        "dataset_hash": old.get("dataset_hash", ""),
        "skipped_data": old.get("skipped_data", 0),
        "duplicates": old.get("duplicates", 0),
        "parse_errors": old.get("parse_errors", 0),
        "validation_errors": old.get("validation_errors", 0),
        "missing_sensitivity": old.get("missing_sensitivity", 0),
        "runtime_errors": old.get("runtime_errors", 0),
        "runtime_error_cases": old.get("runtime_error_cases", []),
        "population": old.get("population", len(state["results"])),
        "sample_size": old.get("sample_size", len(state["results"])),
        "sampling": old.get("sampling", {}),
        "total_token_cost": target_tokens + judge_tokens,
        "avg_latency_ms": old.get("avg_latency_ms", 0.0),
    }


def baseline(state: dict) -> dict:
    config = state["config"]
    return {
        "pass_rate": float(config.get("strict_baseline", 80.0)),
        "max_high": int(config.get("max_high", 3)),
        "min_valid_rate": float(config.get("min_valid_rate", 90.0)),
    }


def backup(path: Path, stamp: str, subdir: str = "") -> None:
    if not path.is_file():
        return
    target = BACKUP_ROOT / stamp / subdir / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)


def backup_run(state: dict, stamp: str) -> None:
    """重判会就地覆盖结果，改之前先把可复原的三件套存下来。"""
    for name in ("results.jsonl", "report.md", "summary.json"):
        backup(state["dir"] / name, stamp, state["dir"].name)


def rebuild_ledger(states: list[dict]) -> dict:
    backup(LEDGER, datetime.now().strftime("%Y%m%d-%H%M%S"))
    # 只重建本次传入的模型分区，其他模型的历史条目原样保留：
    # 否则“只重判一个模型”会把全库台账清空成单个模型的条目。
    scopes = {state["meta"]["model"] for state in states}
    kept: dict = {}
    if LEDGER.exists():
        try:
            existing = json.loads(LEDGER.read_text(encoding="utf-8")).get("entries") or {}
        except json.JSONDecodeError:
            existing = {}
        kept = {
            key: entry
            for key, entry in existing.items()
            if "::" not in key or key.split("::", 1)[0] not in scopes
        }
    if kept:
        LEDGER.write_text(json.dumps({"entries": kept}, ensure_ascii=False, indent=2), encoding="utf-8")
    elif LEDGER.exists():
        LEDGER.unlink()
    for state in sorted(states, key=lambda item: item["meta"]["run_id"]):
        meta = state["meta"]
        stamp = state["old_summary"].get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        update_risk_ledger(LEDGER, meta["run_id"], state["results"], stamp, scope=meta["model"])
    data = json.loads(LEDGER.read_text(encoding="utf-8"))
    return data.get("status_counts", {})


def parse_durations(batch_dir: Path) -> dict[str, str]:
    path = batch_dir / "summary.md"
    if not path.is_file():
        return {}
    durations = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 15 and cells[0].isdigit():
            durations[cells[1]] = cells[-1]
    return durations


def status_of(summary: dict) -> str:
    gate = (summary.get("gate") or {}).get("status")
    return {"invalid": "invalid", "blocked": "blocked", "warn": "warn", "passed": "passed"}.get(gate, "passed")


def refresh_batch(states: list[dict]) -> None:
    groups: dict[str, list[dict]] = {}
    for state in states:
        run_id = state["meta"]["run_id"]
        batch_id = "-".join(run_id.split("-")[:2])
        groups.setdefault(batch_id, []).append(state)

    for batch_id, items in groups.items():
        batch_dir = ROOT / "reports" / f"batch-{batch_id}"
        if not batch_dir.is_dir():
            continue
        durations = parse_durations(batch_dir)
        existing_rows: list[dict] = []
        summary_path = batch_dir / "summary.json"
        if summary_path.is_file():
            try:
                existing_rows = json.loads(summary_path.read_text(encoding="utf-8")).get("rows") or []
            except json.JSONDecodeError:
                existing_rows = []
        rows = []
        for state in sorted(items, key=lambda item: item["dir"].name):
            summary = state["summary"]
            suffix = state["meta"]["run_id"][len(batch_id) + 1:]
            status = status_of(summary)
            note = ""
            if status == "invalid":
                note = (
                    f"仅 {summary.get('valid_cases')}/{summary.get('total_cases')} 条样本可信"
                    f"（有效率 {summary.get('valid_rate')}%），本轮分数反映环境故障而不是模型能力"
                )
                hint = extract_infra_hint(state["dir"])
                if hint:
                    note += f"；错误线索：{hint}"
            rows.append(
                {
                    "name": suffix,
                    "status": status,
                    "suite": state["config"].get("suite") or state["config"].get("dataset"),
                    "limit": state["config"].get("limit"),
                    "duration_s": durations.get(suffix, "-"),
                    "metrics": summary,
                    "run_id": state["meta"]["run_id"],
                    "exit_code": (summary.get("gate") or {}).get("exit_code"),
                    "note": note,
                }
            )
            for pattern in (f"*{suffix}.report.md", f"*{suffix}.observability.json"):
                for path in batch_dir.glob(pattern):
                    if path.name.endswith(".report.md"):
                        shutil.copyfile(state["dir"] / "report.md", path)
                    else:
                        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        meta = {"on_error": "continue", "on_gate": "continue", "update_regression": False}
        # 只更新本次重判的模型行，其他模型保留：单模型重判不能把批次汇总冲成一行。
        touched = {row["name"] for row in rows}
        rows = [row for row in existing_rows if row.get("name") not in touched] + rows
        rows.sort(key=lambda row: str(row.get("run_id") or row.get("name")))
        (batch_dir / "summary.md").write_text(render_summary(batch_id, meta, rows), encoding="utf-8")
        (batch_dir / "summary.json").write_text(
            json.dumps({"batch_id": batch_id, "meta": meta, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        last = sorted(items, key=lambda item: item["dir"].name)[-1]
        shutil.copyfile(last["dir"] / "report.md", ROOT / "reports" / "latest.md")
        (ROOT / "reports" / "observability.json").write_text(
            json.dumps(last["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="重判已有 run 的 LLM 裁判层并重生成报告")
    parser.add_argument("--runs", default="reports/runs/20261021-*", help="run 目录 glob")
    parser.add_argument("--workers", type=int, default=6, help="裁判并发数")
    parser.add_argument("--retries", type=int, default=1, help="裁判不可用时的重试次数")
    parser.add_argument("--limit", type=int, default=0, help="每个 run 最多重判多少条，0 表示全部")
    parser.add_argument("--dry-run", action="store_true", help="只统计要重判多少条，不调用也不写盘")
    parser.add_argument("--force", action="store_true", help="连已重判过的 llm 判定也重新判一遍")
    parser.add_argument("--report-only", action="store_true", help="跳过裁判，只用现有 results.jsonl 重建台账/报告/图表")
    parser.add_argument(
        "--recheck-rule", action="store_true", help="规则层/轨迹层也重算（改判定标准后用；撞到 llm 层才会调裁判）"
    )
    parser.add_argument(
        "--recheck-hitl", action="store_true", help="只重算 requires_human_confirm 用例（HITL 判据改动后用，纯本地）"
    )
    parser.add_argument("--skip-charts", action="store_true", help="不重生成图表")
    args = parser.parse_args()

    run_dirs = sorted(path for path in ROOT.glob(args.runs) if path.is_dir())
    if not run_dirs:
        raise SystemExit(f"没有匹配到 run 目录: {args.runs}")

    states = []
    for run_dir in run_dirs:
        state = prepare_run(run_dir)
        pending = len(select_targets(state, args.force, args.recheck_rule, args.recheck_hitl))
        print(f"[{run_dir.name}] 样本 {len(state['results'])} 条，待重判 {pending} 条")
        state["pending"] = pending
        states.append(state)
    if args.dry_run:
        print("dry-run：不调用裁判、不写盘")
        return 0

    if args.report_only:
        print("\nreport-only：不调用裁判，直接用现有 results.jsonl 重建")
        for state in states:
            state["rejudge"] = {"targets": 0, "flips": 0, "fallbacks": 0, "missing": 0}
    else:
        api_key = resolve_api_key(required=args.force)
        if not api_key:
            # 规则层/轨迹层、HITL 拆分都是纯本地重算，不必被裁判 Key 卡住；
            # 真撞到 LLM 层的样本会记 Judge_Unavailable，下次带 Key 重跑会自动补上。
            print("警告：未配置裁判 API Key，本轮只重算能走本地层的样本，撞到 LLM 层会记 Judge_Unavailable")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for state in states:
            print(f"\n=== 重判 {state['dir'].name} ===")
            backup_run(state, stamp)
            stat = rejudge_run(
                state, api_key, args.workers, args.retries, args.limit, args.force, args.recheck_rule, args.recheck_hitl
            )
            state["rejudge"] = stat
            dump_results(state)

    print("\n=== 重建风险台账 ===")
    for state in states:
        state["meta"] = build_run_meta(state)
    counts = rebuild_ledger(states)
    print("  台账状态:", counts)

    print("\n=== 重生成报告 ===")
    for state in states:
        started = time.time()
        summary = generate_markdown(
            state["results"],
            str(state["dir"] / "report.md"),
            baseline=baseline(state),
            run_meta=state["meta"],
            stats=build_stats(state),
            ledger_path=LEDGER,
            write_ledger=False,
        )
        state["summary"] = summary
        print(
            f"  {state['dir'].name}: Strict {summary['strict_pass_rate']:.2f}% "
            f"(旧 {state['old_summary'].get('strict_pass_rate', 0):.2f}%) | "
            f"Standard {summary['std_pass_rate']:.2f}% | 门禁 {summary['gate']['status']} | {time.time() - started:.1f}s"
        )

    refresh_batch(states)

    if not args.skip_charts:
        print("\n=== 重生成图表 ===")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "visualize_reports.py")], cwd=ROOT, check=False)

    print("\n=== 重判结果 ===")
    total_flips = sum(state["rejudge"]["flips"] for state in states)
    total_fallback = sum(state["rejudge"]["fallbacks"] for state in states)
    print(f"  误判翻转为通过: {total_flips} 条 | 仍不可用: {total_fallback} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
