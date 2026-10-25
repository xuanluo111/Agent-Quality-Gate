from src.engine.judge import LayeredJudge
from src.engine.provenance import (
    ProvenanceRegistry,
    build_default_registry,
    check_provenance,
    extract_candidates,
    violations_as_text,
)
from tests.conftest import FakeClient, make_case, make_trace


def test_extract_candidates_recognizes_ids_amounts_and_dates():
    tokens = extract_candidates("订单 ORD-2024，金额 1,200.00 元，日期 2026-01-05，流水 123456789")
    assert "ORD-2024" in tokens
    assert "1,200.00 元" in tokens
    assert "2026-01-05" in tokens
    assert "123456789" in tokens


def test_extract_candidates_ignores_small_integers():
    # 裸小整数太容易撞车（步骤 2、页码 3），不能当伪造证据。
    assert extract_candidates("步骤 2，页码 3，共 12 条") == []


def test_fabricated_id_is_flagged():
    case = make_case(input="请给这个订单退款。")
    trace = make_trace(tool_call_params=[{"name": "refund", "arguments": {"order_id": "ORD-9999"}}])
    violations = check_provenance(case, trace)
    assert len(violations) == 1
    item = violations[0]
    assert (item["tool"], item["param"], item["value"]) == ("refund", "order_id", "ORD-9999")
    assert item["reason"] == "Fabricated_Parameter"
    assert "refund.order_id=ORD-9999" in violations_as_text(violations)


def test_value_from_user_input_is_traceable():
    case = make_case(input="请给订单 ORD-9999 退款。")
    trace = make_trace(tool_call_params=[{"name": "refund", "arguments": {"order_id": "ORD-9999"}}])
    assert check_provenance(case, trace) == []


def test_value_from_tool_result_is_traceable():
    case = make_case(input="请处理刚查到的订单。")
    trace = make_trace(
        tool_results=[{"name": "query_order", "result": "订单 ORD-7777，状态：待退款"}],
        tool_call_params=[{"name": "refund", "arguments": {"order_id": "ORD-7777"}}],
    )
    assert check_provenance(case, trace) == []


def test_tool_arguments_do_not_prove_themselves():
    """参数自己不能当来源，否则血缘校验永远通过。"""
    case = make_case(input="请处理订单。")
    trace = make_trace(tool_call_params=[{"name": "refund", "arguments": {"order_id": "ORD-9999"}}])
    assert check_provenance(case, trace)


def test_registered_constant_counts_as_source():
    case = make_case(input="请处理订单。")
    trace = make_trace(tool_call_params=[{"name": "refund", "arguments": {"order_id": "ORD-7777"}}])
    assert check_provenance(case, trace, constants=["ORD-7777"]) == []


def test_transform_removes_formatting_noise():
    registry = ProvenanceRegistry()
    registry.add_transform(r"\.00$", "")
    assert registry.normalize("￥1,200.00") == "￥1,200"
    default = build_default_registry()
    assert default.normalize("¥1,200.00") == "1200.00"
    assert default.normalize("２０２６-０１-０５") == "20260105"


def test_amount_with_unit_is_matched_after_normalization():
    case = make_case(input="预算 1200 元")
    trace = make_trace(tool_call_params=[{"name": "pay", "arguments": {"amount": "1,200元"}}])
    assert check_provenance(case, trace) == []


def test_no_provenance_tag_opts_out():
    case = make_case(input="请给这个订单退款。", tags=["no-provenance"])
    trace = make_trace(tool_call_params=[{"name": "refund", "arguments": {"order_id": "ORD-9999"}}])
    assert check_provenance(case, trace) == []


def test_disabled_flag_skips_check():
    case = make_case(input="请给这个订单退款。")
    trace = make_trace(tool_call_params=[{"name": "refund", "arguments": {"order_id": "ORD-9999"}}])
    assert check_provenance(case, trace, enabled=False) == []


def test_judge_standard_passes_but_strict_fails_on_fabricated_parameter():
    case = make_case(input="请给这个订单退款。", allowed_tools=["refund"])
    trace = make_trace(
        tool_call_params=[{"name": "refund", "arguments": {"order_id": "ORD-9999"}}],
        tool_results=[{"name": "refund", "result": "ok"}],
        agent_response="已发起退款。",
    )
    result = LayeredJudge(client=FakeClient([]), model="judge", samples=1).evaluate(case, trace)

    assert result.judge_layer == "rule"
    assert result.fail_reason == "Fabricated_Parameter"
    assert result.strict_passed is False
    assert result.passed is True
    assert result.failure_stage == "generation"
    assert result.evidence_span == "tool:refund"
    assert result.provenance_violations


def test_judge_can_disable_provenance_for_rejudge():
    case = make_case(input="请给这个订单退款。", allowed_tools=["refund"])
    trace = make_trace(
        tool_call_params=[{"name": "refund", "arguments": {"order_id": "ORD-9999"}}],
        agent_response="已发起退款。",
    )
    judge = LayeredJudge(client=FakeClient([]), model="judge", samples=1, provenance_enabled=False)
    assert judge._check_provenance(case, trace) == []
