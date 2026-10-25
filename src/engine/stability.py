"""pass^k 与稳定性：同一用例独立重复 N 次后的汇总口径。

- pass@k：N 次里至少一次通过（宽松，衡量"能不能做对"）。
- pass^k：N 次全部通过（严格，衡量"能不能稳定做对"）。
- stability_rate：不是 flaky 的用例占比；flaky = 至少一次过且至少一次不过。

门禁默认用 pass^k（等价于把 N 次结果取最差），
这样偶发失败不会被"多试几次就过了"掩盖。
"""

from typing import Dict, List, Optional, Sequence, Tuple

from src.models.schema import AttemptResult, AgentTrace, EvalResult

RISK_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def attempt_outcomes(result: EvalResult) -> List[bool]:
    """取出该用例每次尝试的 Strict 结论；未开启 repeat 时退化为单次结果。"""
    if getattr(result, "attempts", None):
        return [bool(item.strict_passed) for item in result.attempts]
    return [bool(result.strict_passed)]


def to_attempt(index: int, result: EvalResult, trace: Optional[AgentTrace] = None) -> AttemptResult:
    return AttemptResult(
        attempt=index,
        passed=bool(result.passed),
        strict_passed=bool(result.strict_passed),
        risk_level=result.risk_level,
        judge_layer=result.judge_layer,
        fail_reason=result.fail_reason,
        data_valid=bool(result.data_valid),
        state_diff=dict(result.state_diff or {}),
        latency_ms=float(getattr(trace, "latency_ms", 0.0) or 0.0),
    )


def merge_attempts(
    attempts: Sequence[Tuple[AgentTrace, EvalResult]],
    *,
    run_id: str = "",
    model: str = "",
) -> Tuple[AgentTrace, EvalResult]:
    """把 N 次尝试压成一条用例结论。

    主结论取"最差的一次"：只要有一次 Strict 失败，该用例就是失败；
    Standard 取"任一次造成实际危害即失败"。原始逐次结论完整保留在 attempts 里。
    """
    if not attempts:
        raise ValueError("merge_attempts 需要至少一次尝试")
    results = [result for _, result in attempts]
    failed = [item for item in results if not item.strict_passed]
    worst = min(failed, key=lambda item: RISK_RANK.get(item.risk_level, 9)) if failed else results[0]
    worst_index = results.index(worst)
    merged = worst.model_copy(deep=True)

    merged.attempts = [
        to_attempt(index + 1, result, trace) for index, (trace, result) in enumerate(attempts)
    ]
    merged.strict_passed = all(item.strict_passed for item in results)
    merged.passed = all(item.passed for item in results)
    merged.data_valid = all(item.data_valid for item in results)
    merged.token_cost = sum(item.token_cost for item in results)
    merged.judge_attempts = sum(item.judge_attempts for item in results)
    merged.judge_disagreement = any(item.judge_disagreement for item in results)
    merged.state_diff = dict(worst.state_diff or {})
    merged.provenance_violations = list(worst.provenance_violations or [])
    merged.model = model or worst.model
    merged.run_id = run_id or worst.run_id

    outcomes = [item.strict_passed for item in results]
    if len(outcomes) > 1 and len(set(outcomes)) > 1:
        merged.judge_reason = f"[不稳定] {sum(outcomes)}/{len(outcomes)} 次通过｜{merged.judge_reason}"
    return attempts[worst_index][0], merged


def stability_metrics(results: Sequence[EvalResult]) -> Dict[str, object]:
    items = list(results)
    total = len(items)
    repeat = max((len(attempt_outcomes(item)) for item in items), default=1) or 1
    at_least_one = 0
    all_pass = 0
    stable = 0
    flaky_cases: List[str] = []
    for item in items:
        outcomes = attempt_outcomes(item)
        if any(outcomes):
            at_least_one += 1
        if outcomes and all(outcomes):
            all_pass += 1
        if len(set(outcomes)) > 1:
            flaky_cases.append(item.case_id)
        else:
            stable += 1
    percent = lambda count: round(count / total * 100, 2) if total else 0.0  # noqa: E731
    return {
        "repeat": repeat,
        "pass_at_k": percent(at_least_one),
        "pass_pow_k": percent(all_pass),
        "stability_rate": percent(stable),
        "flaky_count": len(flaky_cases),
        "flaky_cases": flaky_cases,
    }
