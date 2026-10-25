import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import List

sys.path.append(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.config import add_run_args, config_as_dict, config_from_args
from src.engine.dataset import audit_cases, load_cases, stratified_sample
from src.engine.failure_class import classify_case
from src.engine.journal import JOURNAL_FILENAME, recover as recover_journal
from src.engine.judge import LayeredJudge
from src.engine.report import generate_markdown
from src.engine.runner import AgentRunner
from src.engine.stability import merge_attempts
from src.models.schema import AgentTrace, EvalResult, TestCase

load_dotenv()

REGRESSION_FILE = "cases/regression.jsonl"


def _harness_failure(case: TestCase, error: Exception | str, run_id: str, model: str) -> tuple[AgentTrace, EvalResult]:
    message = str(error)
    trace = AgentTrace(
        case_id=case.id,
        agent_response="",
        model=model,
        run_id=run_id,
        is_success=False,
        error_message=message,
        should_escalate_to_human=case.requires_human_confirm,
    )
    result = EvalResult(
        case_id=case.id,
        category=case.category,
        passed=False,
        strict_passed=False,
        risk_level="high",
        judge_layer="harness",
        judge_reason="Harness 执行失败，按失败关闭处理",
        fail_reason="Harness_Error",
        phenomenon="运行期异常",
        evidence=message[:1000],
        impact="本轮结果不能被当作通过",
        suggestion="检查模型网关、工具沙箱与裁判服务",
        model=model,
        run_id=run_id,
    )
    return trace, result


def _attempts_payload(traces: List[AgentTrace]) -> List[dict]:
    """逐次轨迹按尝试顺序、统一成 1 起再落盘。

    `AgentTrace.attempt` 是 0 起（workspace 后缀按 `attempt>0` 判断），而
    `EvalResult.attempts` 里的结论是 1 起；而且合并后“最差的一次”被挪到了列表
    末尾。不重排不重编号的话，外部脚本按 `attempt` 关联轨迹与结论会错位一对。
    """
    ordered = sorted(traces, key=lambda item: int(getattr(item, "attempt", 0) or 0))
    payload: List[dict] = []
    for index, trace in enumerate(ordered, 1):
        item = trace.model_dump()
        item["attempt"] = index
        payload.append(item)
    return payload


def run_single_case(
    case: TestCase, runner: AgentRunner, judge: LayeredJudge, run_id: str, model: str, repeat: int = 1
):
    """跑一条用例。

    repeat>1 时同一用例独立跑 N 次：每次新建 workspace（attempt>0 带 -aN 后缀）、
    重新构造 messages（对话上下文天然清空），最后按最差一次合并。
    返回 (traces, result)，traces 用于成本/延迟统计；其中最后一个元素必须是
    result 对应的那次尝试——results.jsonl 只存它，rejudge 重判时也按它回放。
    """
    attempts = []
    for index in range(max(1, repeat)):
        try:
            trace = runner.run_case(case, attempt=index)
            result = judge.evaluate(case, trace)
            result.model = model
            result.run_id = run_id
            # 误报归属：规则层/环境信号在 trace 与 journal 里，不在裁判输出里。
            result.failure_class = classify_case(
                result, trace, journal_path=getattr(runner, "journal_path", None), case=case
            )
        except Exception as exc:
            trace, result = _harness_failure(case, exc, run_id, model)
            result.failure_class = classify_case(result, trace, case=case)
        attempts.append((trace, result))
    if len(attempts) == 1:
        trace, result = attempts[0]
        return [trace], result
    worst_trace, result = merge_attempts(attempts, run_id=run_id, model=model)
    traces = [item[0] for item in attempts]
    # 合并结论取自“最差一次”，配对写盘时也必须是这次，否则 rejudge 会拿错轨迹重判。
    return [trace for trace in traces if trace is not worst_trace] + [worst_trace], result


def _append_regression(path: Path, cases: list[TestCase]) -> int:
    existing_ids = set()
    existing_inputs = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("id"):
                existing_ids.add(record["id"])
            if record.get("input"):
                existing_inputs.add(hashlib.sha256(record["input"].strip().encode("utf-8")).hexdigest())
    added = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for case in cases:
            input_hash = hashlib.sha256(case.input.strip().encode("utf-8")).hexdigest()
            if case.id in existing_ids or input_hash in existing_inputs:
                continue
            handle.write(json.dumps(case.model_dump(), ensure_ascii=False) + "\n")
            existing_ids.add(case.id)
            existing_inputs.add(input_hash)
            added += 1
    return added


def _print_dry_run(cases, load_report, sampled, sampling, audit) -> int:
    print(f"数据集: {load_report['path']}")
    print(f"原始记录: {load_report['raw_records']} | 可用: {load_report['loaded_cases']} | 重复: {load_report['duplicates']}")
    print(f"解析失败: {len(load_report['parse_errors'])} | 字段校验失败: {len(load_report['validation_errors'])}")
    for item in (load_report.get("warnings") or [])[:5]:
        print(f"数据告警 {item['id']}: {item['warning']}")
    print(f"采样: {sampling['sample']} / {sampling['population']}，方法 {sampling['method']}，seed={sampling['seed']}")
    print(f"类别分布: {audit['by_category']}")
    print(f"敏感度分布: {audit['by_sensitivity']}")
    print(f"JCV 用例: {audit['expected_variables_cases']} | HITL 用例: {audit['hitl_cases']} | 带 fixture 用例: {audit['fixture_cases']}")
    if audit["unsupported_tools"]:
        print(f"未支持的 allowed_tools: {audit['unsupported_tools']}")
    warnings = []
    for name, key in (("JCV", "expected_variables_cases"), ("HITL", "hitl_cases")):
        if audit[key] == 0:
            warnings.append(f"没有 {name} 用例")
    if not {"S2", "S3"} & set(audit["by_sensitivity"]):
        warnings.append("没有 S2/S3 用例")
    for warning in warnings:
        print(f"WARN: {warning}")
    if load_report["parse_errors"] or load_report["validation_errors"]:
        print("数据校验失败，dry-run 返回 1")
        return 1
    print("dry-run 通过：数据可加载、可采样，未调用任何模型。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent-Quality-Gate 评测入口")
    add_run_args(parser)
    args = parser.parse_args()
    config = config_from_args(args)

    try:
        config.validate()
    except Exception as exc:
        print(f"配置错误: {exc}")
        return 1

    cases, load_report = load_cases(config.dataset, dedupe=True)
    if not cases:
        print(f"没有可用用例: {config.dataset}")
        return 1
    if load_report["parse_errors"] or load_report["validation_errors"]:
        print(
            f"数据集存在不可解析记录：解析失败 {len(load_report['parse_errors'])} 条，"
            f"字段校验失败 {len(load_report['validation_errors'])} 条。"
        )
        print("为避免静默丢弃用例，评测已中止；请先运行 python scripts/run_eval.py --dry-run 修复数据。")
        return 1
    sampled, sampling = stratified_sample(cases, config.limit, config.seed)
    audit = audit_cases(sampled, load_report)

    if config.dry_run:
        return _print_dry_run(cases, load_report, sampled, sampling, audit)

    run_dir = config.run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(
        json.dumps(config_as_dict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "dataset_audit.json").write_text(
        json.dumps({"load": load_report, "sample_audit": audit, "sampling": sampling}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "sampled_cases.jsonl").write_text(
        "\n".join(json.dumps(case.model_dump(), ensure_ascii=False) for case in sampled) + "\n",
        encoding="utf-8",
    )

    # 开跑前先收尾上次遗留的 intent：此时还没有任何用例在写，标 unknown 才安全。
    journal_file = Path(config.workspace_root) / config.run_id / JOURNAL_FILENAME
    orphaned = recover_journal(journal_file)
    if orphaned:
        print(f"[journal] 上次中断遗留 {len(orphaned)} 条未完成操作，已标记为 unknown")

    runner = AgentRunner(
        config.base_url,
        config.api_key,
        config.model,
        local_base=config.local_base_url,
        local_api_key=config.local_api_key,
        local_model=config.local_model,
        workspace_root=config.workspace_root,
        run_id=config.run_id,
        max_rounds=config.max_rounds,
        token_budget=config.token_budget,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        timeout=config.request_timeout,
        seed=config.seed,
    )
    judge = LayeredJudge(
        base_url=config.judge_base_url,
        api_key=config.judge_api_key,
        model=config.judge_model,
        mode=config.mode,
        samples=config.judge_samples,
        jcv_threshold=config.jcv_threshold,
        timeout=config.request_timeout,
        collateral_risk=config.collateral_risk,
        provenance_enabled=config.provenance_enabled,
    )

    results: list[EvalResult] = []
    traces: list[AgentTrace] = []
    runtime_error_cases: list[str] = []
    partial_path = run_dir / "results.jsonl"
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    start_time = time.time()

    print(
        f"开始评测 | run_id={config.run_id} | mode={config.mode} | model={config.model} | "
        f"judge={config.judge_model} | sample={len(sampled)}/{len(cases)}"
    )
    with open(partial_path, "w", encoding="utf-8") as partial:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.workers) as executor:
            futures = {
                executor.submit(
                    run_single_case, case, runner, judge, config.run_id, config.model, config.repeat
                ): case
                for case in sampled
            }
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                case = futures[future]
                completed += 1
                try:
                    case_traces, result = future.result()
                except Exception as exc:
                    trace, result = _harness_failure(case, exc, config.run_id, config.model)
                    case_traces = [trace]
                results.append(result)
                traces.extend(case_traces)
                if any(not trace.is_success for trace in case_traces):
                    runtime_error_cases.append(case.id)
                partial.write(
                    json.dumps(
                        {
                            "result": result.model_dump(),
                            "trace": case_traces[-1].model_dump(),
                            "attempts": _attempts_payload(case_traces),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                partial.flush()
                elapsed = time.time() - start_time
                eta = (elapsed / completed) * (len(sampled) - completed) / 60 if completed else 0
                print(
                    f"[{completed}/{len(sampled)}] {result.risk_level.upper()} L={result.judge_layer} "
                    f"strict={'PASS' if result.strict_passed else 'FAIL'} "
                    f"attempts={len(case_traces)} "
                    f"target_tokens={sum(trace.token_cost for trace in case_traces)} judge_tokens={result.token_cost} "
                    f"elapsed={elapsed / 60:.1f}m eta={eta:.1f}m"
                )

    finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    total_tokens = sum(result.token_cost for result in results) + sum(trace.token_cost for trace in traces)
    # 429/超时样本的 latency 是重试累计（如 602s），计入平均值会把“环境故障”算成“模型慢”。
    success_traces = [trace for trace in traces if trace.is_success]
    avg_latency = (
        sum(trace.latency_ms for trace in success_traces) / len(success_traces)
        if success_traces
        else 0.0
    )
    stats = {
        "dataset_hash": load_report["dataset_sha256"],
        "skipped_data": (
            len(load_report["parse_errors"])
            + len(load_report["validation_errors"])
            + load_report["duplicates"]
        ),
        "parse_errors": len(load_report["parse_errors"]),
        "validation_errors": len(load_report["validation_errors"]),
        "duplicates": load_report["duplicates"],
        "missing_sensitivity": load_report["missing_sensitivity"],
        "runtime_errors": len(runtime_error_cases),
        "runtime_error_cases": runtime_error_cases,
        "population": len(cases),
        "sample_size": len(sampled),
        "sampling": sampling,
        "total_token_cost": total_tokens,
        "avg_latency_ms": round(avg_latency, 2),
    }
    run_meta = config.metadata()
    run_meta["started_at"] = started_at
    run_meta["finished_at"] = finished_at
    run_meta["strict_baseline"] = config.strict_baseline
    run_meta["max_high"] = config.max_high
    run_meta["jcv_threshold"] = config.jcv_threshold
    run_meta["require_stable"] = config.require_stable
    run_meta["repeat"] = config.repeat

    summary = generate_markdown(
        results,
        str(run_dir / "report.md"),
        baseline={
            "pass_rate": config.strict_baseline,
            "max_high": config.max_high,
            "min_valid_rate": config.min_valid_rate,
            "require_stable": config.require_stable,
            "exclude_unknown": config.exclude_unknown,
        },
        run_meta=run_meta,
        stats=stats,
        ledger_path="reports/risk_ledger.json",
        write_ledger=config.publish,
    )
    # --no-publish（自检/调试）：跑出来的结论只留在本轮 run 目录，不覆盖对外副本。
    if config.publish:
        Path("reports").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run_dir / "report.md", "reports/latest.md")
        Path("reports/observability.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    regression_added = 0
    if config.update_regression:
        case_map = {case.id: case for case in sampled}
        failures = [
            case_map[result.case_id]
            for result in results
            if not result.strict_passed
            and result.risk_level in {"critical", "high"}
            and result.case_id in case_map
        ]
        regression_added = _append_regression(Path(REGRESSION_FILE), failures)

    print("\n" + "=" * 58)
    print("评测完成")
    print("=" * 58)
    print(f"总用例: {summary['total_cases']} | Standard: {summary['std_pass_rate']:.2f}% | Strict: {summary['strict_pass_rate']:.2f}%")
    print(f"Strict 95% CI: [{summary['strict_ci_low']:.2f}%, {summary['strict_ci_high']:.2f}%]")
    print(f"Critical: {summary['critical_count']} | High: {summary['high_count']} | fallback: {summary['fallback_count']}")
    print(
        f"稳定性: repeat={summary['repeat']} | pass@k={summary['pass_at_k']:.2f}% | "
        f"pass^k={summary['pass_pow_k']:.2f}% | flaky={summary['flaky_count']}"
    )
    print(f"Token: {summary['total_token_cost']:,} | 平均延迟: {summary['avg_latency_ms']:.0f} ms")
    print(f"关键产物: {run_dir / 'report.md'}")
    ledger_note = "reports/risk_ledger.json" if config.publish else "未写入（--no-publish）"
    print(f"台账: {ledger_note} | 新增回归用例: {regression_added}")
    gate = summary["gate"]
    print(f"门禁: {gate['status']} ({gate['exit_code']}) - {'; '.join(gate['reasons'])}")
    print("=" * 58)
    return int(gate["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
