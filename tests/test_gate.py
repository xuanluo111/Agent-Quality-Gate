from src.engine.gate import evaluate_gate
from tests.conftest import make_result


def result(strict=True, passed=None, risk="low", layer="rule", fallback=False):
    return make_result(
        passed=strict if passed is None else passed,
        strict_passed=strict,
        risk_level=risk,
        judge_layer=layer,
        is_fallback=fallback,
    )


def test_gate_passes_clean_run():
    decision = evaluate_gate([result(), result()], strict_baseline=80)
    assert decision.status == "passed"
    assert decision.exit_code == 0
    assert decision.enforced is True


def test_gate_blocks_on_critical():
    decision = evaluate_gate([result(), result(strict=False, risk="critical")], strict_baseline=80)
    assert decision.status == "blocked"
    assert decision.exit_code == 1
    assert any("Critical" in reason for reason in decision.reasons)


def test_gate_blocks_below_baseline():
    results = [result(strict=False, risk="medium") for _ in range(3)] + [result() for _ in range(7)]
    decision = evaluate_gate(results, strict_baseline=80)
    assert decision.status == "blocked"


def test_gate_warns_on_high_above_threshold():
    results = [result(strict=False, risk="high") for _ in range(4)] + [result() for _ in range(6)]
    decision = evaluate_gate(results, strict_baseline=50, max_high=3)
    assert decision.status == "warn"
    assert decision.exit_code == 2


def test_gate_shadow_never_blocks():
    decision = evaluate_gate([result(strict=False, risk="critical")], mode="shadow")
    assert decision.status == "shadow"
    assert decision.exit_code == 0
    assert decision.enforced is False


def test_gate_blocks_empty_sample():
    decision = evaluate_gate([])
    assert decision.status == "blocked"
    assert decision.exit_code == 1


def test_gate_marks_env_failure_as_invalid():
    """全是网关异常时应判“数据无效”，而不是把环境问题当成模型不合格。"""
    decision = evaluate_gate([result(strict=False, risk="high", layer="harness")], strict_baseline=0)
    assert decision.status == "invalid"
    assert decision.exit_code == 1
    assert any("Harness" in reason for reason in decision.reasons)
    assert decision.metrics["valid_cases"] == 0
    assert decision.metrics["untrusted_samples"] == 1


def test_gate_marks_judge_fallback_as_invalid():
    decision = evaluate_gate(
        [result(strict=False, risk="high", layer="local_fallback", fallback=True)],
        strict_baseline=0,
    )
    assert decision.status == "invalid"
    assert any("兜底" in reason for reason in decision.reasons)


def test_gate_blocked_when_samples_are_trusted():
    """样本可信但模型真失败，仍然是 blocked。"""
    results = [result(strict=False, risk="critical") for _ in range(3)] + [result() for _ in range(7)]
    decision = evaluate_gate(results, strict_baseline=80)
    assert decision.status == "blocked"
    assert decision.metrics["valid_rate"] == 100.0


def blocked_result(index: int):
    return make_result(
        case_id=f"blocked-{index}",
        passed=False,
        strict_passed=False,
        risk_level="medium",
        judge_layer="harness",
        fail_reason="Provider_Blocked",
        data_valid=False,
    )


def test_provider_blocked_samples_leave_the_validity_denominator():
    """被网关内容审核拦截的样本永远测不到，不能算成“有效率低”去判 invalid。"""
    results = [result() for _ in range(8)] + [blocked_result(index) for index in range(2)]
    decision = evaluate_gate(results, strict_baseline=50)
    assert decision.metrics["provider_blocked_samples"] == 2
    assert decision.metrics["measurable_cases"] == 8
    assert decision.metrics["valid_rate"] == 100.0
    # 但没测到的样本也不能当通过：结论仍是 blocked，只是原因分开了。
    assert decision.status == "blocked"
    assert any("2 条用例被网关内容安全审核拦截" in reason for reason in decision.reasons)


def test_provider_blocked_only_run_is_not_reported_as_environment_fault():
    """全被拦截时没有可测样本：有效率归 0 判 invalid，但不能说是“环境故障”。"""
    decision = evaluate_gate([blocked_result(index) for index in range(3)], strict_baseline=50)
    assert decision.metrics["measurable_cases"] == 0
    assert decision.metrics["valid_rate"] == 0.0
    assert decision.status == "invalid"
    assert any("网关内容审核拦截 3 条" in reason for reason in decision.reasons)
    # 不能说成“修复 Key”，那会把人往错方向推。
    assert any("换" in reason and "网关" in reason for reason in decision.reasons)
    assert not any("修复 Key/配额/网关/裁判" in reason for reason in decision.reasons)


def test_gate_invalid_only_when_below_min_valid_rate():
    """少量环境故障仍是 blocked（失败关闭），只有大面积故障才升级为数据无效。"""
    mostly_valid = [result() for _ in range(19)] + [
        result(strict=False, risk="high", layer="harness")
    ]
    decision = evaluate_gate(mostly_valid, strict_baseline=0, min_valid_rate=90.0)
    assert decision.status == "blocked"
    assert decision.metrics["valid_rate"] == 95.0
    assert any("Harness" in reason for reason in decision.reasons)
    assert not any("环境故障而不是模型能力" in reason for reason in decision.reasons)

    mostly_broken = [result() for _ in range(8)] + [
        result(strict=False, risk="high", layer="harness") for _ in range(2)
    ]
    decision = evaluate_gate(mostly_broken, strict_baseline=0, min_valid_rate=90.0)
    assert decision.status == "invalid"
    assert decision.metrics["valid_rate"] == 80.0
    assert decision.metrics["strict_rate_on_valid_only"] == 100.0
    assert any("环境故障而不是模型能力" in reason for reason in decision.reasons)


def test_gate_shadow_reports_valid_rate_without_blocking():
    decision = evaluate_gate(
        [result(strict=False, risk="high", layer="harness") for _ in range(5)],
        mode="shadow",
    )
    assert decision.status == "shadow"
    assert decision.exit_code == 0
    assert any("有效率" in reason for reason in decision.reasons)


def test_gate_invalid_when_truncation_drags_valid_rate_down():
    """相当比例样本因 max_tokens 被截断时，分数再高也不能放行。"""
    results = [make_result() for _ in range(88)] + [
        make_result(
            passed=False,
            strict_passed=False,
            risk_level="medium",
            judge_layer="harness",
            fail_reason="Output_Truncated",
            data_valid=False,
            output_truncated=True,
        )
        for _ in range(12)
    ]
    decision = evaluate_gate(results, strict_baseline=80.0, min_valid_rate=90.0)
    assert decision.status == "invalid"
    assert decision.metrics["output_truncated_samples"] == 12
    assert any("max_tokens 截断" in reason for reason in decision.reasons)


def test_strict_rate_on_valid_samples_excludes_untrusted_passes():
    """口径不能靠“无效样本恰好都失败”的巧合：无效样本即使通过也不进分子。"""
    results = [
        make_result(),
        make_result(),
        make_result(judge_layer="harness", data_valid=False, passed=True, strict_passed=True),
    ]
    decision = evaluate_gate(results, strict_baseline=0, min_valid_rate=50.0)
    assert decision.metrics["valid_cases"] == 2
    assert decision.metrics["strict_pass"] == 3
    assert decision.metrics["strict_pass_valid"] == 2
    assert decision.metrics["strict_rate_on_valid_only"] == 100.0


def test_gate_keeps_gateway_and_truncation_counts_separate():
    """两类故障的修复动作不同（换 Key vs 调大 max_tokens），文案不能糊在一起。"""
    results = [make_result() for _ in range(88)] + [
        make_result(
            passed=False,
            strict_passed=False,
            risk_level="medium",
            judge_layer="harness",
            fail_reason="Output_Truncated",
            data_valid=False,
            output_truncated=True,
        )
        for _ in range(6)
    ] + [
        make_result(
            passed=False,
            strict_passed=False,
            risk_level="high",
            judge_layer="harness",
            fail_reason="Harness_Error",
            data_valid=False,
        )
        for _ in range(6)
    ]
    decision = evaluate_gate(results, strict_baseline=80.0, min_valid_rate=90.0)
    text = "；".join(decision.reasons)
    assert "输出被 max_tokens 截断 6 条" in text
    assert "Harness/网关异常 6 条" in text
