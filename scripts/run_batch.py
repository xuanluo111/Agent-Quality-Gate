#!/usr/bin/env python3
"""串行批量评测多个模型：前一个模型跑完后自动启动下一个。

模型清单来源（优先级从高到低）：
  1. --matrix 指定的 JSON 文件
  2. 环境变量 AQG_MODEL_MATRIX 指向的 JSON 文件
  3. 环境变量 AQG_MODELS 内联：name|model|base_url|KEY_ENV;name2|model2|url2|KEY_ENV2

清单示例见 models.example.json。API Key 只通过子进程环境变量传递，不会出现在
命令行参数、日志或批次汇总里。

常用命令：
  python scripts/run_batch.py --matrix models.json --dry-run    # 先免费校验清单
  python scripts/run_batch.py --matrix models.json              # 正式串行评测
  python scripts/run_batch.py --matrix models.json --only glm-5.2
  python scripts/run_batch.py --matrix models.json --skip-existing
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.engine.report import gate_brief

RUN_EVAL = ROOT / "scripts" / "run_eval.py"
OBSERVABILITY = ROOT / "reports" / "observability.json"

# 清单键 -> run_eval 命令行开关
PASSTHROUGH = {
    "workers": "--workers",
    "seed": "--seed",
    "mode": "--mode",
    "temperature": "--temperature",
    "timeout": "--timeout",
    "max_rounds": "--max-rounds",
    "token_budget": "--token-budget",
    "max_tokens": "--max-tokens",
    "judge_samples": "--judge-samples",
    "judge_model": "--judge-model",
    "judge_base_url": "--judge-base-url",
    "strict_baseline": "--strict-baseline",
    "max_high": "--max-high",
    "min_valid_rate": "--min-valid-rate",
    "repeat": "--repeat",
    "collateral_risk": "--collateral-risk",
}

# 环境故障线索：命中这些模式说明是 Key/配额/网关问题，而不是模型答错。
RE_INFRA = re.compile(
    r"max_tokens"
    r"|Error code:\s*\d+|HTTP 4\d\d|HTTP 5\d\d"
    r"|APIConnectionError|ConnectTimeout|ReadTimeout|RateLimit"
    r"|insufficient|disabled|quota"
    # 内容安全审核也归这里：先给出线索，具体是不是“网关拦截”看结果里的 Provider_Blocked。
    r"|content.?security|content.?safety|safety system",
    re.IGNORECASE,
)


def slug(value: str, fallback: str = "model") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip()).strip("-._")
    return cleaned[:48] or fallback


def parse_inline(raw: str) -> list[dict]:
    specs: list[dict] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [part.strip() for part in chunk.split("|")]
        if len(parts) < 3:
            raise SystemExit(f"AQG_MODELS 片段格式错误，需要 name|model|base_url[|KEY_ENV]：{chunk}")
        spec = {"name": parts[0], "model": parts[1], "base_url": parts[2]}
        if len(parts) > 3 and parts[3]:
            spec["api_key_env"] = parts[3]
        specs.append(spec)
    return specs


def load_specs(matrix: str) -> tuple[dict, list[dict]]:
    path_value = (matrix or os.getenv("AQG_MODEL_MATRIX", "")).strip()
    if path_value:
        path = Path(path_value)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise SystemExit(f"模型清单不存在: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"模型清单不是合法 JSON: {path} ({exc})") from exc
        if isinstance(data, list):
            return {}, data
        if not isinstance(data, dict):
            raise SystemExit("模型清单顶层必须是数组，或含 models 数组的对象")
        return data.get("defaults") or {}, data.get("models") or []
    inline = os.getenv("AQG_MODELS", "").strip()
    if inline:
        return {}, parse_inline(inline)
    raise SystemExit(
        "缺少模型清单：用 --matrix 指定 JSON，"
        "或设置 AQG_MODEL_MATRIX（文件路径）/ AQG_MODELS（name|model|base_url|KEY_ENV;...）。"
    )


def normalize_spec(raw: dict, defaults: dict, index: int) -> dict:
    merged = {**defaults, **raw}
    name = str(merged.get("name") or merged.get("model") or f"model{index}").strip()
    model = str(merged.get("model") or name).strip()
    base_url = str(merged.get("base_url") or merged.get("url") or "").strip()
    if not base_url:
        raise SystemExit(f"[{name}] 缺少 base_url")
    spec = dict(merged)
    spec["name"] = name
    spec["model"] = model
    spec["base_url"] = base_url
    spec.setdefault("suite", "core")
    spec.setdefault("limit", 50)
    spec.setdefault("workers", 3)
    return spec


def resolve_secret(spec: dict, field: str, required: bool = True) -> str:
    direct = str(spec.get(field) or "").strip()
    if direct:
        return direct
    env_name = str(spec.get(f"{field}_env") or "").strip()
    if not env_name:
        return ""
    value = os.getenv(env_name, "").strip()
    if not value and required:
        raise SystemExit(f"[{spec['name']}] 环境变量 {env_name} 为空，无法读取 {field}")
    return value


def child_env(spec: dict, require_key: bool = True) -> dict[str, str]:
    """构造子进程环境：先清空 AQG_*/GC_API_KEY，避免上一个模型的配置串到下一个。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AQG_") and key != "GC_API_KEY"
    }
    env["AQG_BASE_URL"] = spec["base_url"]
    env["AQG_MODEL"] = spec["model"]
    # 必须按模型显式赋值：子进程会再 load_dotenv()，留空就会被 .env 里的旧值顶上，
    # 导致 7 份报告全写成同一个模型版本。
    env["AQG_MODEL_VERSION"] = str(spec.get("model_version") or spec["model"]).strip()
    env["PYTHONUNBUFFERED"] = "1"

    api_key = (
        resolve_secret(spec, "api_key", required=require_key)
        or os.getenv("AQG_API_KEY", "")
        or os.getenv("GC_API_KEY", "")
    )
    if not api_key and require_key:
        raise SystemExit(
            f"[{spec['name']}] 缺少 API Key：请在清单里配 api_key_env，或先 export AQG_API_KEY。"
        )
    if api_key:
        env["AQG_API_KEY"] = api_key

    for field, var in (
        ("judge_api_key", "AQG_JUDGE_API_KEY"),
        ("local_api_key", "AQG_LOCAL_API_KEY"),
        ("local_base_url", "AQG_LOCAL_BASE_URL"),
        ("local_model", "AQG_LOCAL_MODEL"),
        ("model_version", "AQG_MODEL_VERSION"),
    ):
        value = (
            resolve_secret(spec, field, required=require_key)
            if field.endswith("api_key")
            else str(spec.get(field) or "").strip()
        )
        if value:
            env[var] = value
    return env


def build_command(spec: dict, run_id: str, output_root: Path, dry_run: bool, update_regression: bool) -> list[str]:
    command = [sys.executable, str(RUN_EVAL)]
    dataset = str(spec.get("dataset") or "").strip()
    if dataset:
        command += ["--dataset", dataset]
    else:
        command += ["--suite", str(spec.get("suite") or "core")]
    command += ["--limit", str(spec.get("limit", 50))]
    for key, flag in PASSTHROUGH.items():
        value = spec.get(key)
        if value is None or value == "":
            continue
        command += [flag, str(value)]
    # 布尔开关不能进 PASSTHROUGH（会变成 `--require-stable True`），单独处理。
    if str(spec.get("require_stable") or "").strip().lower() in {"1", "true", "yes"}:
        command.append("--require-stable")
    if spec.get("provenance_enabled") is False:
        command.append("--no-provenance")
    if not update_regression:
        command.append("--no-regression")
    command += ["--output-root", str(output_root), "--run-id", run_id]
    if dry_run:
        command.append("--dry-run")
    return command


def run_one(command: list[str], env: dict[str, str], log_path: Path) -> tuple[int, float]:
    started = time.time()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
                log.flush()
            code = process.wait()
        except KeyboardInterrupt:
            print("\n收到中断信号，正在终止当前模型评测...")
            process.terminate()
            process.wait()
            raise
    return code, round(time.time() - started, 1)


def snapshot_metrics(run_id: str, run_dir: Path, batch_dir: Path, stem: str) -> dict:
    """把本轮 report.md 与 observability.json 快照到批次目录，避免被下一个模型覆盖。"""
    metrics: dict = {}
    report = run_dir / "report.md"
    if report.is_file():
        shutil.copyfile(report, batch_dir / f"{stem}.report.md")
    if not OBSERVABILITY.is_file():
        return metrics
    try:
        data = json.loads(OBSERVABILITY.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return metrics
    if data.get("run_id") != run_id:
        return metrics
    shutil.copyfile(OBSERVABILITY, batch_dir / f"{stem}.observability.json")
    return data


def read_run_epoch(run_dir: Path) -> str:
    """从本轮 summary.json 读数据集代际指纹（无则空串），供批次跨代分组。"""
    summary = run_dir / "summary.json"
    if not summary.is_file():
        return ""
    try:
        data = json.loads(summary.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return str(data.get("dataset_epoch") or "")


def already_done(output_root: Path, name: str) -> bool:
    return any((path / "report.md").is_file() for path in output_root.glob(f"*-{slug(name)}"))


def extract_infra_hint(run_dir: Path, limit: int = 3) -> str:
    """从逐条结果里提取环境故障线索，让批次摘要能直接看出是 Key 还是模型问题。"""
    path = run_dir / "results.jsonl"
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    counter: Counter = Counter()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = item.get("result") or {}
        trace = item.get("trace") or {}
        if result.get("judge_layer") != "harness" and trace.get("is_success", True):
            continue
        blob = " ".join(
            str(value or "")
            for value in (trace.get("error_message"), result.get("judge_reason"), result.get("evidence"))
        ).strip()
        match = RE_INFRA.search(blob)
        counter[match.group(0) if match else (blob[:100] or "未知异常")] += 1
    if not counter:
        return ""
    return "；".join(f"{key} ×{count}" for key, count in counter.most_common(limit))


def _cell(value, suffix: str = "") -> str:
    if value is None or value == "":
        return "-"
    return f"{value}{suffix}"


def _hitl_cell(metrics: dict) -> str:
    """HITL 两个口径合到一列：安全守住率 / 升级流程完成率。"""
    safe = metrics.get("hitl_safe_rate")
    escalate = metrics.get("hitl_escalate_rate")
    if safe is None or escalate is None:
        return "-"
    return f"{safe:.1f}% / {escalate:.1f}%"


def _two_prop_p(a: int, n1: int, b: int, n2: int) -> float:
    """双比例 z 检验的双侧 p 值。样本百级、比例接近 1 时只能作粗略参考。"""
    if n1 <= 0 or n2 <= 0:
        return 1.0
    pooled = (a + b) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(a / n1 - b / n2) / se
    return math.erfc(z / math.sqrt(2))


def _rank_block(rows: list[dict]) -> str:
    """排名必须带显著性：120 条样本下，几个百分点的差距常常说明不了问题。"""
    entries = []
    for row in rows:
        metrics = row.get("metrics") or {}
        total = int(metrics.get("total_cases") or 0)
        passed = int(metrics.get("strict_pass") or 0)
        if not total:
            continue
        entries.append(
            {
                "name": row["name"],
                "passed": passed,
                "total": total,
                "rate": passed / total * 100,
                "rate_valid": metrics.get("strict_pass_rate_valid_only"),
                "ci_low": metrics.get("strict_ci_low"),
                "ci_high": metrics.get("strict_ci_high"),
                "reliable": bool(metrics.get("data_reliable", True)),
                "valid": metrics.get("valid_cases"),
            }
        )
    if len(entries) < 2:
        return ""
    ranked = sorted(entries, key=lambda entry: entry["rate"], reverse=True)
    baseline = next((entry for entry in ranked if entry["reliable"]), ranked[0])
    lines = [
        "",
        "## 排名解读（Strict，全量口径）",
        "",
        "| 名次 | 模型 | Strict | Strict(仅可信) | 95% CI | 可信样本 | 与第 1 名相比 | 判定 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for index, entry in enumerate(ranked, 1):
        if entry is baseline:
            compared, verdict = "—", "基准"
        else:
            # 判定用与展示相同的三位小数，避免出现 p=0.050 却写“显著”的自相矛盾。
            p = round(
                _two_prop_p(entry["passed"], entry["total"], baseline["passed"], baseline["total"]), 3
            )
            compared = "p<0.001" if p < 0.001 else f"p={p:.3f}"
            verdict = "统计上不可区分" if p >= 0.05 else "差异显著"
        low, high = entry["ci_low"], entry["ci_high"]
        ci = f"{low:.2f}~{high:.2f}" if low is not None and high is not None else "-"
        valid_rate = entry.get("rate_valid")
        valid_text = f"{valid_rate:.2f}%" if valid_rate is not None else "-"
        name = entry["name"] + (" *" if not entry["reliable"] else "")
        lines.append(
            f"| {index} | {name} | {entry['rate']:.2f}% | {valid_text} | {ci} "
            f"| {entry.get('valid') or '-'}/{entry['total']} | {compared} | {verdict} |"
        )
    lines += [
        "",
        "> 双比例 z 检验（双侧，全量口径），未做多重比较校正；p≥0.05 只代表「这批样本不足以证明有差异」，不是「两者相同」。"
        "标 * 的场次数据有效率不足，分数受环境故障影响，不参与排名结论。",
    ]
    return "\n".join(lines)


def render_summary(batch_id: str, meta: dict, rows: list[dict]) -> str:
    judges = sorted({str((row.get("metrics") or {}).get("judge_model") or "") for row in rows} - {""})
    same_source = [row["name"] for row in rows if (row.get("metrics") or {}).get("judge_same_as_sut")]
    lines = [
        f"# 批量评测汇总 `{batch_id}`",
        "",
        f"- 模型数量：{len(rows)}（失败策略 on_error={meta['on_error']}, on_gate={meta['on_gate']}）",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 回归回写：{'开启' if meta['update_regression'] else '关闭（保证各模型数据集一致）'}",
    ]
    if meta.get("generation_mixed"):
        epochs = meta.get("dataset_epochs") or []
        lines.append(
            f"- ⚠️ 本批次混用 {len(epochs)} 代数据集（{', '.join(epochs)}）：跨代通过率不可直接横向排名，请分代比较。"
        )
    if judges:
        line = f"- 裁判模型：{', '.join(judges)}"
        if same_source:
            line += f"（{', '.join(same_source)} 场次与被测模型同源，判分需打折）"
        lines.append(line)
    lines += [
        "",
        "| # | 模型 | 状态 | 数据集 | 用例 | 可信样本 | Standard | Strict | Strict(仅可信) | Critical | High "
        "| HITL 守住/升级 | Tokens | 平均延迟(ms) | 门禁 | 耗时(s) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for index, row in enumerate(rows, 1):
        metrics = row.get("metrics") or {}
        gate = gate_brief(metrics.get("gate") or {})
        lines.append(
            "| {i} | {name} | {status} | {dataset} | {total} | {valid} | {std} | {strict} | {strict_valid} "
            "| {critical} | {high} | {hitl} | {tokens} | {latency} | {gate} | {duration} |".format(
                i=index,
                name=row["name"],
                status=row["status"],
                dataset=f"{row['suite']}({row['limit']})",
                total=_cell(metrics.get("total_cases")),
                valid=_cell(metrics.get("valid_cases")),
                std=_cell(metrics.get("std_pass_rate"), "%"),
                strict=_cell(metrics.get("strict_pass_rate"), "%"),
                strict_valid=_cell(metrics.get("strict_pass_rate_valid_only"), "%"),
                critical=_cell(metrics.get("critical_count")),
                high=_cell(metrics.get("high_count")),
                hitl=_hitl_cell(metrics),
                tokens=_cell(metrics.get("total_token_cost")),
                latency=_cell(metrics.get("avg_latency_ms")),
                gate=gate,
                duration=row.get("duration_s"),
            )
        )
    lines += [
        "",
        "> `blocked` 分两种归因：带 `Critical` 或 `Strict 低于基线` 是模型能力不达标；"
        "只标注 `裁判兜底`/`截断`/`网关异常` 时是样本不可信，换一轮运行结果可能就变，"
        "不宜直接当作模型不合格。",
    ]
    notes = [row for row in rows if row.get("note")]
    rank_block = _rank_block(rows)
    if rank_block:
        lines.append(rank_block)
    if notes:
        lines += ["", "## 异常说明", ""]
        lines += [f"- `{row['name']}`：{row['note']}" for row in notes]
    invalid = [row for row in rows if row["status"] == "invalid"]
    if invalid:
        lines += [
            "",
            "> ⚠️ 状态为 `invalid` 的模型存在 Key/配额/网关/裁判异常或 max_tokens 截断，部分样本根本没测到模型"
            "（缺口见上方「可信样本」列），其 Strict 分数按“失败关闭”计算，**不能与其他模型横向对比**，也不应对外引用；"
            "需要横向比较时看「Strict(仅可信)」列。",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="串行批量评测多个模型")
    parser.add_argument("--matrix", default="", help="模型清单 JSON 路径（默认读 AQG_MODEL_MATRIX）")
    parser.add_argument("--output-root", default="reports/runs")
    parser.add_argument("--batch-name", default="", help="批次目录名后缀，默认使用时间戳")
    parser.add_argument("--only", default="", help="只跑指定模型，逗号分隔（支持 name 或 model）")
    parser.add_argument("--skip-existing", action="store_true", help="已有完整 report.md 的模型直接跳过")
    parser.add_argument("--dry-run", action="store_true", help="只校验数据，不调用模型（不产生费用）")
    parser.add_argument("--on-error", choices=["continue", "stop"], default="continue", help="执行异常时的策略")
    parser.add_argument("--on-gate", choices=["continue", "stop"], default="continue", help="门禁未通过时的策略")
    parser.add_argument(
        "--update-regression",
        action="store_true",
        help="允许把失败用例追加进 cases/regression.jsonl（默认关闭，避免模型间数据集不一致）",
    )
    args = parser.parse_args()

    defaults, raw_specs = load_specs(args.matrix)
    if not raw_specs:
        print("模型清单为空。")
        return 1

    specs: list[dict] = []
    for index, raw in enumerate(raw_specs, 1):
        if not isinstance(raw, dict):
            raise SystemExit(f"第 {index} 个模型配置不是对象")
        specs.append(normalize_spec(raw, defaults, index))

    only = {item.strip() for item in args.only.split(",") if item.strip()}
    if only:
        specs = [spec for spec in specs if spec["name"] in only or spec["model"] in only]
        if not specs:
            print(f"--only 没有匹配到任何模型: {sorted(only)}")
            return 1

    output_root = Path(args.output_root)
    if not output_root.is_absolute():
        output_root = ROOT / output_root
    batch_id = args.batch_name or time.strftime("%Y%m%d-%H%M%S")
    batch_dir = output_root.parent / f"batch-{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    total = len(specs)
    print("=" * 72)
    print(f"批量评测启动 | 批次={batch_id} | 模型数={total} | 串行执行 | dry_run={args.dry_run}")
    print(f"执行顺序: {' -> '.join(spec['name'] for spec in specs)}")
    print(f"批次产物: {batch_dir}")
    print("=" * 72)

    rows: list[dict] = []
    batch_started = time.time()
    for index, spec in enumerate(specs, 1):
        name = spec["name"]
        stem = f"{index:02d}-{slug(name)}"
        run_id = f"{batch_id}-{slug(name)}"
        run_dir = output_root / run_id

        if args.skip_existing and already_done(output_root, name):
            print(f"\n[{index}/{total}] 跳过 {name}：已有历史 report.md（--skip-existing）")
            rows.append(
                {
                    "name": name,
                    "status": "skipped",
                    "suite": spec.get("suite") or spec.get("dataset"),
                    "limit": spec.get("limit"),
                    "duration_s": 0,
                    "metrics": {},
                    "dataset_epoch": read_run_epoch(run_dir),
                    "note": "使用 --skip-existing 跳过",
                }
            )
            continue

        print("\n" + "-" * 72)
        print(f"[{index}/{total}] 开始评测 {name} | model={spec['model']} | base_url={spec['base_url']}")
        print("-" * 72)

        env = child_env(spec, require_key=not args.dry_run)
        command = build_command(spec, run_id, output_root, args.dry_run, args.update_regression)
        code, duration = run_one(command, env, batch_dir / f"{stem}.log")
        metrics = snapshot_metrics(run_id, run_dir, batch_dir, stem)
        has_report = (run_dir / "report.md").is_file()

        if not has_report and code != 0:
            status = "error"
        elif not has_report:
            status = "ok"
        elif code == 1:
            status = "blocked"
        elif code == 2:
            status = "warn"
        else:
            status = "passed"

        note = ""
        gate_status = (metrics.get("gate") or {}).get("status") or ""
        if status == "error":
            note = f"exit_code={code}，见 {stem}.log"
        elif gate_status == "invalid":
            # 大部分样本没测到模型：分数是失败关闭造出来的，不能当成模型能力差。
            status = "invalid"
            valid_cases = metrics.get("valid_cases", 0)
            total_cases = metrics.get("total_cases", 0)
            measurable = metrics.get("measurable_cases") or total_cases
            valid_rate = metrics.get("valid_rate", 0)
            blocked = int(metrics.get("provider_blocked_count") or 0)
            note = (
                f"仅 {valid_cases}/{measurable} 条可测样本可信（有效率 {valid_rate}%），"
                "本轮分数反映环境故障而不是模型能力"
            )
            if blocked:
                note += f"；网关内容审核拦截 {blocked} 条（模型未被调用，需换链路）"
            hint = extract_infra_hint(run_dir)
            if hint:
                note += f"；错误线索：{hint}"
        rows.append(
            {
                "name": name,
                "status": status,
                "suite": spec.get("suite") or spec.get("dataset"),
                "limit": spec.get("limit"),
                "duration_s": duration,
                "metrics": metrics,
                "dataset_epoch": read_run_epoch(run_dir),
                "run_id": run_id,
                "exit_code": code,
                "note": note,
            }
        )
        print(f"[{index}/{total}] {name} 完成 | 状态={status} | exit={code} | 耗时={duration}s")

        if code != 0 and not has_report and args.on_error == "stop":
            print(f"执行异常且 on_error=stop，终止批次（剩余 {total - index} 个模型未执行）")
            break
        if code in (1, 2) and has_report and args.on_gate == "stop":
            print(f"门禁未通过且 on_gate=stop，终止批次（剩余 {total - index} 个模型未执行）")
            break

    run_epochs = sorted({str(row.get("dataset_epoch") or "") for row in rows} - {""})
    meta = {
        "on_error": args.on_error,
        "on_gate": args.on_gate,
        "update_regression": args.update_regression,
        # 批次跨代信息：>1 个 epoch 说明同一批次里混了不同代数据集，跨代不能直接排名。
        "dataset_epochs": run_epochs,
        "generation_mixed": len(run_epochs) > 1,
    }
    summary_path = batch_dir / "summary.md"
    summary_path.write_text(render_summary(batch_id, meta, rows), encoding="utf-8")
    (batch_dir / "summary.json").write_text(
        json.dumps({"batch_id": batch_id, "meta": meta, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    elapsed = (time.time() - batch_started) / 60
    print("\n" + "=" * 72)
    print(f"批量评测结束 | 耗时 {elapsed:.1f} 分钟 | 汇总: {summary_path}")
    for row in rows:
        metrics = row.get("metrics") or {}
        strict = metrics.get("strict_pass_rate")
        print(
            f"  {row['name']:<28} {row['status']:<8} "
            f"Strict={_cell(strict, '%'):<8} 仅可信={_cell(metrics.get('strict_pass_rate_valid_only'), '%'):<8} "
            f"Critical={_cell(metrics.get('critical_count'))}"
        )
    print("=" * 72)

    failed = [row for row in rows if row["status"] == "error"]
    invalid = [row for row in rows if row["status"] == "invalid"]
    if invalid:
        print(
            f"\n⚠️  {len(invalid)} 个模型本轮数据无效（环境故障），分数不可引用："
            + "、".join(row["name"] for row in invalid)
            + "\n   请先检查 API Key 状态/套餐配额/网关可用性，再重跑这几个模型。"
        )
    return 1 if failed or invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
