"""pass^k 与稳定性：同一用例独立重复 N 次后的汇总口径。

- pass@k：N 次里至少一次通过（宽松，衡量"能不能做对"）。
- pass^k：N 次全部通过（严格，衡量"能不能稳定做对"）。
- stability_rate：不是 flaky 的用例占比；flaky = 至少一次过且至少一次不过。

门禁默认用 pass^k（等价于把 N 次结果取最差），
这样偶发失败不会被"多试几次就过了"掩盖。
"""

from typing import Dict, List, Optional, Sequence, Tuple

from src.engine.textsim import jaccard, shingles
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


def behavior_signature(trace: AgentTrace) -> set:
    """一次尝试的「行为面」：实际调用过的工具名集合。

    用工具名而不是完整参数：参数里带时间戳、随机 id、绝对路径，那些差异说明不了
    行为有没有分化，只会把 Jaccard 永远拉到 1 以下——指标就失去分辨力。
    """
    names = getattr(trace, "tool_call_sequence", None) or getattr(trace, "tools_called", None) or []
    return {str(name) for name in names if str(name)}


def attempt_divergence(traces: Sequence[AgentTrace]) -> dict:
    """K08 验证口径：同一用例 N 次尝试之间到底分化了多少。

    三个数字各自回答一个不同的问题，故意**不合并成一个分数**：

    - `seed_unique` — 机制有没有生效。等于 1 说明变序没接上（或被网关吞掉），
      此时下面两个数字再怎么变都是噪声，不是“稳定性好”；
    - `tool_jaccard` — 行为路径是否分化（两两平均）；
    - `reply_jaccard` — 回复文本是否分化（两两平均，字符 shingle）。

    合成一个分数会把“网关不认 seed”和“模型本来就稳”混成一谈，而这两件事
    该修的地方完全不同。

    `reply_jaccard` 对空回复记 1.0（都没答），但不能让它稀释分化度——空回复样本
    要么被判 `unknow`要么进 `NO_FINAL_RESPONSE`，本就不该当作“稳定”。
    """
    items = list(traces)
    if len(items) < 2:
        return {
            "attempts": len(items),
            "seed_unique": len({getattr(item, "seed", None) for item in items}),
            "tool_jaccard": None,
            "reply_jaccard": None,
            "empty_reply_pairs": 0,
        }

    seeds = {getattr(item, "seed", None) for item in items}
    signatures = [behavior_signature(item) for item in items]
    replies = [shingles(getattr(item, "agent_response", "") or "") for item in items]
    tool_scores: List[float] = []
    reply_scores: List[float] = []
    empty_pairs = 0
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            tool_scores.append(jaccard(signatures[left], signatures[right]))
            if not replies[left] and not replies[right]:
                empty_pairs += 1
                continue
            reply_scores.append(jaccard(replies[left], replies[right]))
    return {
        "attempts": len(items),
        "seed_unique": len(seeds),
        "tool_jaccard": round(sum(tool_scores) / len(tool_scores), 4) if tool_scores else None,
        "reply_jaccard": round(sum(reply_scores) / len(reply_scores), 4) if reply_scores else None,
        "empty_reply_pairs": empty_pairs,
    }


def divergence_summary(per_case: Sequence[dict]) -> dict:
    """把逐用例的分化度汇总成一批的口径。

    `cases_with_single_seed` 是最关键的一列：它数的是**机制没生效**的用例。
    只要它非 0，“稳定性数字”就没有资格被引用，不管 jaccard 多好看。
    """
    items = [item for item in per_case if item.get("attempts", 0) > 1]
    if not items:
        return {
            "cases": 0,
            "cases_with_single_seed": 0,
            "tool_jaccard": None,
            "reply_jaccard": None,
            "empty_reply_pairs": 0,
        }
    tool_values = [item["tool_jaccard"] for item in items if item.get("tool_jaccard") is not None]
    reply_values = [item["reply_jaccard"] for item in items if item.get("reply_jaccard") is not None]
    return {
        "cases": len(items),
        "cases_with_single_seed": sum(1 for item in items if item.get("seed_unique", 0) <= 1),
        "tool_jaccard": round(sum(tool_values) / len(tool_values), 4) if tool_values else None,
        "reply_jaccard": round(sum(reply_values) / len(reply_values), 4) if reply_values else None,
        "empty_reply_pairs": sum(int(item.get("empty_reply_pairs", 0)) for item in items),
    }
