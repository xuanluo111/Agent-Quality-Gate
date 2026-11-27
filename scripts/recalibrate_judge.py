"""用新裁判重判固定轨迹并计算 κ，产物与正式报告完全隔离。

与 `rejudge.py` 的区别：后者会重建正式台账、latest 和图表；本脚本只读取已有
run 的 trace，按人工标签选样，把重判结果和 κ 写到指定输出目录。

用法：
    .venv/bin/python scripts/recalibrate_judge.py \
        --run reports/runs/20261121-0147-tokendance-spark-x2.5-4b \
        --labels docs/c02-judge-calibration/recalib-2625-spark4b/labels.filled.jsonl \
        --output-dir workspace/judge-recalib/glm-5.3/spark4b \
        --judge-model glm-5.3 \
        --judge-base-url https://token.sensenova.cn/v1/ \
        --judge-api-key-env SENSENOVA_HH_API_KEY \
        --judge-reasoning-effort low \
        --judge-stream \
        --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from calibrate_judge import calibrate, load_judgements  # noqa: E402
from rejudge import prepare_run, rejudge_run, validate_thinking_mode  # noqa: E402


def validate_labels(path: Path) -> dict[str, dict[str, Any]]:
    """读取已填人工真值；null、非布尔和重复 case_id 都必须失败关闭。"""
    labels: dict[str, dict[str, Any]] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"人工标签第 {line_no} 行不是合法 JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise SystemExit(f"人工标签第 {line_no} 行必须是对象")
        case_id = str(record.get("case_id") or "").strip()
        if not case_id:
            raise SystemExit(f"人工标签缺少 case_id: {path}")
        if case_id in labels:
            raise SystemExit(f"人工标签 case_id 重复: {case_id}")
        if record.get("human_passed") not in (True, False):
            raise SystemExit(f"人工标签尚未填写或不是布尔值: {case_id}")
        stage = record.get("human_stage")
        labels[case_id] = {
            "human_passed": record["human_passed"],
            "human_stage": str(stage).lower() if stage else None,
        }
    if not labels:
        raise SystemExit(f"人工标签为空: {path}")
    return labels


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def validate_output_dir(output_dir: Path, run_dir: Path) -> None:
    resolved = output_dir.resolve()
    if resolved == run_dir.resolve():
        raise SystemExit("输出目录不能覆盖源 run")
    reports = (ROOT / "reports").resolve()
    if resolved == reports or reports in resolved.parents:
        raise SystemExit("标定产物必须隔离在 reports/ 之外")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"输出目录非空，拒绝覆盖: {output_dir}")


def prepare_isolated_state(
    run_dir: Path,
    labels: dict[str, dict[str, Any]],
    output_dir: Path,
    judge_model: str,
    judge_base_url: str,
    judge_samples: int,
    thinking: str,
    reasoning_effort: str = "low",
    stream: bool = True,
) -> dict:
    """只保留标签命中的轨迹，并覆盖裁判配置；不修改源 run。"""
    validate_output_dir(output_dir, run_dir)
    try:
        validate_thinking_mode(judge_model, thinking)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    state = prepare_run(run_dir)
    wanted = set(labels)
    paired = [
        (row, result)
        for row, result in zip(state["rows"], state["results"])
        if result.case_id in wanted
    ]
    found = {result.case_id for _, result in paired}
    missing = sorted(wanted - found)
    if missing:
        raise SystemExit(f"源 run 缺少人工标签对应样本 {len(missing)} 条: {', '.join(missing[:8])}")
    state["rows"] = [row for row, _ in paired]
    state["results"] = [result for _, result in paired]
    state["dir"] = output_dir
    state["config"].update(
        {
            "judge_model": judge_model,
            "judge_base_url": judge_base_url,
            "judge_samples": judge_samples,
            "judge_thinking": thinking,
            "judge_reasoning_effort": reasoning_effort,
            "judge_stream": stream,
        }
    )
    return state


def write_initial_results(state: dict) -> None:
    output_dir: Path = state["dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        json.dumps({"result": result.model_dump(), "trace": row["trace"]}, ensure_ascii=False)
        for row, result in zip(state["rows"], state["results"])
    ]
    (output_dir / "results.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")


def run_recalibration(state: dict, api_key: str, workers: int, retries: int) -> dict:
    """对全部标签样本重跑分层裁判，而不是只重判旧版解析器污染的 llm 行。"""
    write_initial_results(state)
    # rejudge_run(force=True) 只选旧 judge_layer=llm / Judge_Unavailable。标定要求
    # 固定轨迹全部走一遍新裁判，所以在内存里把选样本标记为 llm；源文件不受影响。
    for result in state["results"]:
        result.judge_layer = "llm"
    return rejudge_run(
        state,
        api_key,
        workers=workers,
        retries=retries,
        limit=0,
        force=True,
        thinking=str(state["config"].get("judge_thinking") or "default"),
    )


def write_calibration(
    state: dict,
    labels: dict[str, dict[str, Any]],
    source_run: Path,
    judge_api_key_env: str,
    stats: dict,
    exclude_invalid: bool,
    exclude_cases: list[str] | None,
) -> dict:
    results_path = state["dir"] / "results.jsonl"
    report = calibrate(
        labels,
        load_judgements(results_path),
        exclude_invalid=exclude_invalid,
        exclude_cases=exclude_cases,
    )
    config = state["config"]
    payload = {
        "schema": "aqg-judge-recalibration/v1",
        "judge_model": config["judge_model"],
        "judge_base_url": config["judge_base_url"],
        "judge_api_key_env": judge_api_key_env,
        "judge_samples": config["judge_samples"],
        "judge_thinking": config.get("judge_thinking", "default"),
        "judge_reasoning_effort": config.get("judge_reasoning_effort", ""),
        "judge_stream": bool(config.get("judge_stream", False)),
        "source_run": str(source_run),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rejudge": stats,
        "complete": report["matched"] == len(labels) and stats.get("fallbacks", 0) == 0,
        **report,
    }
    (state["dir"] / "calibration.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    safe_config = {
        "source_run": str(source_run),
        "dataset": config.get("dataset"),
        "judge_model": config["judge_model"],
        "judge_base_url": config["judge_base_url"],
        "judge_api_key_env": judge_api_key_env,
        "judge_samples": config["judge_samples"],
        "judge_thinking": config.get("judge_thinking", "default"),
        "judge_reasoning_effort": config.get("judge_reasoning_effort", ""),
        "judge_stream": bool(config.get("judge_stream", False)),
        "provenance_enabled": config.get("provenance_enabled", False),
        "collateral_risk": config.get("collateral_risk", "high"),
    }
    (state["dir"] / "config.json").write_text(
        json.dumps(safe_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="新裁判固定轨迹重标（隔离产物，不写正式台账）")
    parser.add_argument("--run", required=True, help="源 run 目录")
    parser.add_argument("--labels", required=True, help="已填人工标签 JSONL")
    parser.add_argument("--output-dir", required=True, help="隔离输出目录，必须位于 reports/ 之外")
    parser.add_argument("--judge-model", default="glm-5.3")
    parser.add_argument("--judge-base-url", default="https://token.sensenova.cn/v1/")
    parser.add_argument("--judge-api-key-env", default="SENSENOVA_HH_API_KEY")
    parser.add_argument("--judge-samples", type=int, default=2)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--thinking", choices=["default", "disabled"], default="default")
    parser.add_argument(
        "--judge-reasoning-effort",
        choices=["low", "high", "max"],
        default="low",
        help="裁判推理强度；glm-5.3 思考始终开启，重标默认 low 以控制输出 token",
    )
    parser.add_argument(
        "--judge-stream",
        dest="judge_stream",
        action="store_true",
        default=True,
        help="裁判请求使用流式响应（默认开启）",
    )
    parser.add_argument(
        "--no-judge-stream",
        dest="judge_stream",
        action="store_false",
        help="关闭裁判流式响应",
    )
    parser.add_argument("--exclude-invalid", action="store_true")
    parser.add_argument("--exclude-case", action="append", default=None, metavar="CASE_ID")
    parser.add_argument("--dry-run", action="store_true", help="只校验样本与配置，不调用裁判、不写盘")
    args = parser.parse_args(argv)

    run_dir = resolve_path(args.run)
    labels_path = resolve_path(args.labels)
    output_dir = resolve_path(args.output_dir)
    if not run_dir.is_dir():
        raise SystemExit(f"源 run 不存在: {run_dir}")
    if not labels_path.is_file():
        raise SystemExit(f"人工标签不存在: {labels_path}")
    if args.judge_model == "glm-5.3" and args.thinking == "disabled":
        raise SystemExit("glm-5.3 的 thinking 始终开启，禁止传 thinking.type=disabled")
    validate_output_dir(output_dir, run_dir)
    labels = validate_labels(labels_path)
    state = prepare_isolated_state(
        run_dir,
        labels,
        output_dir,
        args.judge_model,
        args.judge_base_url,
        args.judge_samples,
        args.thinking,
        args.judge_reasoning_effort,
        args.judge_stream,
    )
    print(
        f"源 run: {run_dir.name}｜人工标签 {len(labels)}｜配对轨迹 {len(state['results'])}｜"
        f"裁判 {args.judge_model}｜thinking={args.thinking}｜"
        f"reasoning_effort={args.judge_reasoning_effort}｜stream={args.judge_stream}"
    )
    if args.dry_run:
        print("dry-run：不调用裁判、不写盘")
        return 0

    api_key = (os.getenv(args.judge_api_key_env) or "").strip()
    if not api_key:
        raise SystemExit(f"环境变量 {args.judge_api_key_env} 为空，无法读取裁判 Key")
    stats = run_recalibration(state, api_key, max(1, args.workers), max(0, args.retries))
    report = write_calibration(
        state,
        labels,
        run_dir,
        args.judge_api_key_env,
        stats,
        args.exclude_invalid,
        args.exclude_case,
    )
    print(
        f"配对 {report['matched']}｜κ={report['kappa']}｜一致率={report['observed_agreement']}%｜"
        f"漏判={report['miss_rate']}%｜误伤={report['false_alarm_rate']}%｜"
        f"裁判不可用={stats.get('fallbacks', 0)}"
    )
    print(f"产物: {output_dir}")
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
