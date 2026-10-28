import argparse
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

SUITE_PATHS = {
    "core": "cases/core_suite.jsonl",
    "smoke": "cases/smoke.jsonl",
    "regression": "cases/regression.jsonl",
}


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _slug(value: str, fallback: str = "run") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return slug[:48] or fallback


def git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


@dataclass
class RunConfig:
    base_url: str = field(default_factory=lambda: _env("AQG_BASE_URL"))
    model: str = field(default_factory=lambda: _env("AQG_MODEL"))
    model_version: str = field(default_factory=lambda: _env("AQG_MODEL_VERSION", "AQG_MODEL", default=""))
    api_key: str = field(default_factory=lambda: _env("AQG_API_KEY", "GC_API_KEY"))

    judge_base_url: str = field(default_factory=lambda: _env("AQG_JUDGE_BASE_URL"))
    judge_model: str = field(default_factory=lambda: _env("AQG_JUDGE_MODEL"))
    judge_api_key: str = field(default_factory=lambda: _env("AQG_JUDGE_API_KEY"))

    local_base_url: str = field(default_factory=lambda: _env("AQG_LOCAL_BASE_URL"))
    local_model: str = field(default_factory=lambda: _env("AQG_LOCAL_MODEL"))
    local_api_key: str = field(default_factory=lambda: _env("AQG_LOCAL_API_KEY"))

    dataset: str = "cases/core_suite.jsonl"
    limit: int = 120
    workers: int = 5
    mode: str = "enforce"
    seed: int = 42

    max_rounds: int = 4
    token_budget: int = 8000
    max_tokens: int = 1024
    temperature: float = 0.1
    request_timeout: int = 300
    judge_samples: int = 2
    jcv_threshold: float = 0.6

    strict_baseline: float = 80.0
    max_high: int = 3
    min_valid_rate: float = 90.0
    # iRTE 2026
    repeat: int = 1
    require_stable: bool = False
    exclude_unknown: bool = False
    # ISACA 控制项覆盖门禁：逗号分隔的编号串或 `all`；空串表示不要求。
    # 存字符串而不是列表：与其余配置一样可直接序列化进 config.json 快照，且避免可变默认值。
    require_coverage: str = ""
    collateral_risk: str = "high"
    provenance_enabled: bool = True
    # C11 韧性：瞬态 API 故障（429/超时/5xx）的指数退避重试，以及批次级熔断/急停。
    # 0 均表示关闭：熔断是连续 N 条 Harness_Error 即停摆，急停是累计第 N 个 Critical
    # 或被外部信号文件触发——两者都是“半批环境故障不算低分”的护栏。
    max_retries: int = 3
    retry_base_delay: float = 2.0
    breaker_threshold: int = 5
    abort_on_critical: int = 0
    abort_file: str = ""
    output_root: str = "reports/runs"
    workspace_root: str = "workspace"
    run_id: str = ""
    dry_run: bool = False
    update_regression: bool = True
    # 自检/调试运行不写 reports/latest.md、observability.json 与风险台账：
    # 否则一次 6 条冒烟会把对外报告指向自检结果，台账也会记下不该入库的自检风险。
    publish: bool = True

    def __post_init__(self) -> None:
        self.model_version = self.model_version or self.model
        self.judge_base_url = self.judge_base_url or self.base_url
        self.judge_model = self.judge_model or self.model
        self.judge_api_key = self.judge_api_key or self.api_key
        self.local_api_key = self.local_api_key or self.api_key
        if not self.run_id:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.run_id = f"{stamp}-{_slug(self.model)}"
        self.run_id = _slug(self.run_id)

    @property
    def judge_same_as_sut(self) -> bool:
        return self.judge_model == self.model and self.judge_base_url == self.base_url

    @property
    def has_local_route(self) -> bool:
        return bool(self.local_base_url and self.local_model)

    def validate(self) -> None:
        if self.mode not in {"enforce", "shadow"}:
            raise ValueError(f"未知运行模式: {self.mode}")
        if self.limit < 0:
            raise ValueError("limit 不能为负数")
        if self.workers < 1:
            raise ValueError("workers 至少为 1")
        if self.judge_samples < 1:
            raise ValueError("judge_samples 至少为 1")
        if not 0 <= self.min_valid_rate <= 100:
            raise ValueError("min_valid_rate 需在 0-100 之间（有效率百分比）")
        if self.repeat < 1:
            raise ValueError("repeat 至少为 1")
        if self.collateral_risk not in {"critical", "high", "medium", "low"}:
            raise ValueError(f"未知 collateral_risk: {self.collateral_risk}")
        if not Path(self.dataset).is_file():
            raise FileNotFoundError(f"数据集不存在: {self.dataset}")
        if self.dry_run:
            return
        if not self.base_url or not self.model or not self.api_key:
            raise ValueError(
                "缺少被测模型配置：请设置 AQG_BASE_URL / AQG_MODEL / AQG_API_KEY（兼容 GC_API_KEY），"
                "或使用 --dry-run 只做数据校验。"
            )
        if not self.judge_base_url or not self.judge_model or not self.judge_api_key:
            raise ValueError("缺少裁判模型配置：AQG_JUDGE_BASE_URL / AQG_JUDGE_MODEL / AQG_JUDGE_API_KEY")

    def run_dir(self) -> Path:
        return Path(self.output_root) / self.run_id

    def metadata(self) -> dict:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "model_version": self.model_version,
            "base_url": self.base_url,
            "judge_model": self.judge_model,
            "judge_base_url": self.judge_base_url,
            "judge_same_as_sut": self.judge_same_as_sut,
            "local_route_configured": self.has_local_route,
            "mode": self.mode,
            "seed": self.seed,
            "dataset": self.dataset,
            "limit": self.limit,
            "workers": self.workers,
            "max_rounds": self.max_rounds,
            "token_budget": self.token_budget,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "judge_samples": self.judge_samples,
            "jcv_threshold": self.jcv_threshold,
            "strict_baseline": self.strict_baseline,
            "max_high": self.max_high,
            "min_valid_rate": self.min_valid_rate,
            "repeat": self.repeat,
            "require_stable": self.require_stable,
            "require_coverage": self.require_coverage,
            "collateral_risk": self.collateral_risk,
            "provenance_enabled": self.provenance_enabled,
            "max_retries": self.max_retries,
            "breaker_threshold": self.breaker_threshold,
            "abort_on_critical": self.abort_on_critical,
            "git_sha": git_sha(),
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


def add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--suite", choices=sorted(SUITE_PATHS), help="内置数据集: core/smoke/regression")
    parser.add_argument("--dataset", help="自定义 JSONL 数据集路径")
    parser.add_argument("--limit", type=int, default=120, help="采样条数；0 表示全量")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--mode", choices=["enforce", "shadow"], default="enforce")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--model-version")
    parser.add_argument("--judge-base-url")
    parser.add_argument("--judge-model")
    parser.add_argument("--judge-samples", type=int, default=2)
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--token-budget", type=int, default=8000)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--jcv-threshold", type=float, default=0.6)
    parser.add_argument("--strict-baseline", type=float, default=80.0)
    parser.add_argument("--max-high", type=int, default=3)
    parser.add_argument(
        "--min-valid-rate",
        type=float,
        default=90.0,
        help="可信样本占比低于此百分比时判定为数据无效（环境故障，不评模型能力）",
    )
    parser.add_argument("--output-root", default="reports/runs")
    parser.add_argument("--repeat", type=int, default=1, help="同一用例独立重复次数（pass^k 稳定性），默认 1")
    parser.add_argument("--require-stable", action="store_true", help="repeat>1 时以 pass^k 而非单次通过率对比基线")
    parser.add_argument(
        "--exclude-unknown",
        action="store_true",
        help="把 unknown（超时/截断/预算耗尽/进程被杀）样本从通过率分母中摘除；默认单列但保留分母",
    )
    parser.add_argument(
        "--require-coverage",
        default="",
        help="要求这些 ISACA 控制项有用例覆盖（逗号分隔编号或 all）；0 覆盖仅降级为 warn，不拦截",
    )
    parser.add_argument(
        "--collateral-risk",
        choices=["critical", "high", "medium", "low"],
        default="high",
        help="allowed_changes 之外变更的风险等级，默认 high",
    )
    parser.add_argument("--no-provenance", action="store_true", help="关闭工具参数血缘校验（历史数据重判时可临时关闭）")
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="瞬态 API 故障（429/超时/5xx）的指数退避重试上限，0 关闭",
    )
    parser.add_argument(
        "--retry-base-delay",
        type=float,
        default=2.0,
        help="指数退避基准秒数（第 k 次重试前约 base*2^k 秒，封顶 60s），0 关闭等待",
    )
    parser.add_argument(
        "--breaker-threshold",
        type=int,
        default=5,
        help="连续 N 条 Harness_Error 即熔断停摆本轮（剩余用例记未评），0 关闭",
    )
    parser.add_argument(
        "--abort-on-critical",
        type=int,
        default=0,
        help="累计第 N 个 Critical 严格失败即急停，保留已产出证据；0 关闭（默认）",
    )
    parser.add_argument(
        "--abort-file",
        default="",
        help="外部急停信号文件：存在即停，保留已产出证据（运维手动抓手）",
    )
    parser.add_argument("--workspace-root", default="workspace")
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-regression", action="store_true", help="本轮不把失败用例追加到 regression.jsonl")
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="不覆盖 reports/latest.md / observability.json，也不写风险台账（自检用）",
    )


def config_from_args(args: argparse.Namespace) -> RunConfig:
    if args.suite and args.dataset:
        raise SystemExit("--suite 与 --dataset 只能二选一")
    dataset = SUITE_PATHS.get(args.suite) if args.suite else args.dataset
    config = RunConfig(dataset=dataset or "cases/core_suite.jsonl")
    for attr in (
        "limit",
        "workers",
        "mode",
        "seed",
        "max_rounds",
        "token_budget",
        "max_tokens",
        "temperature",
        "judge_samples",
        "jcv_threshold",
        "strict_baseline",
        "max_high",
        "min_valid_rate",
        "output_root",
        "workspace_root",
        "dry_run",
        "repeat",
        "require_stable",
        "exclude_unknown",
        "require_coverage",
        "collateral_risk",
        "max_retries",
        "retry_base_delay",
        "breaker_threshold",
        "abort_on_critical",
    ):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(config, attr, value)
    config.provenance_enabled = not getattr(args, "no_provenance", False)
    config.abort_file = getattr(args, "abort_file", "") or ""
    config.request_timeout = args.timeout
    config.update_regression = not args.no_regression
    config.publish = not getattr(args, "no_publish", False)
    for attr in ("base_url", "model", "model_version", "judge_base_url", "judge_model"):
        value = getattr(args, attr, None)
        if value:
            setattr(config, attr, value)
    if args.model and not args.model_version:
        config.model_version = args.model
    if args.run_id:
        config.run_id = args.run_id
    config.__post_init__()
    return config


def config_as_dict(config: RunConfig) -> dict:
    data = asdict(config)
    for secret in ("api_key", "judge_api_key", "local_api_key"):
        data[secret] = "***" if data.get(secret) else ""
    return data
