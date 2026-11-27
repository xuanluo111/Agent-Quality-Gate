import pytest
from pydantic import ValidationError

from src.models.schema import TestCase, normalize_category, normalize_sensitivity, normalize_severity


def test_normalize_aliases():
    assert normalize_severity("严重") == "critical"
    assert normalize_severity("HIGH") == "high"
    assert normalize_sensitivity("s2") == "S2"
    assert normalize_sensitivity("") == "S1"
    assert normalize_category("permission_boundary") == "身份与授权"
    assert normalize_category("") == "未分类"


def test_case_normalizes_fields_and_lists():
    case = TestCase(
        id="x",
        category="tool_misuse",
        input="hello",
        allowed_tools="file_read",
        forbidden_actions=None,
        expected_behavior="ok",
        severity="高",
        data_sensitivity="中",
        expected_variables=None,
        tags="tag1",
    )
    assert case.category == "工具与行动安全"
    assert case.severity == "high"
    assert case.data_sensitivity == "S2"
    assert case.allowed_tools == ["file_read"]
    assert case.forbidden_actions == []
    assert case.expected_variables == []
    assert case.tags == ["tag1"]


def test_case_defaults_missing_sensitivity_to_s1():
    case = TestCase(
        id="x",
        category="身份与授权",
        input="hello",
        expected_behavior="ok",
        severity="medium",
    )
    assert case.data_sensitivity == "S1"
    assert case.fixtures == {}


def test_case_rejects_unknown_severity():
    with pytest.raises(ValidationError):
        TestCase(
            id="x",
            category="身份与授权",
            input="hello",
            expected_behavior="ok",
            severity="urgent",
        )


def test_fixtures_must_be_mapping():
    with pytest.raises(ValidationError):
        TestCase(
            id="x",
            category="身份与授权",
            input="hello",
            expected_behavior="ok",
            severity="high",
            fixtures=["a.txt"],
        )


def test_isaca_controls_defaults_to_empty_list():
    # 空列表是默认值：表示"未显式标注"，交给 infer_case_controls 现场推导，不是"不覆盖任何控制项"。
    case = TestCase(id="x", category="工具与行动安全", input="hi", expected_behavior="ok", severity="high")
    assert case.isaca_controls == []


def test_isaca_controls_accepts_comma_string_and_normalizes():
    case = TestCase(
        id="x",
        category="工具与行动安全",
        input="hi",
        expected_behavior="ok",
        severity="high",
        isaca_controls="c5, C4,c5 ",
    )
    # 大写归一 + 去重保序，脚本批量写入的重复编号在落盘前就收敛。
    assert case.isaca_controls == ["C5", "C4"]


def test_isaca_controls_does_not_validate_membership_here():
    # schema 层不校验编号是否存在（枚举权威在 controls.py）：未知编号在此保留，
    # 由 normalize_control_ids 在构建矩阵时报错，避免两处枚举随条目增删而漂移。
    case = TestCase(
        id="x",
        category="工具与行动安全",
        input="hi",
        expected_behavior="ok",
        severity="high",
        isaca_controls=["C99"],
    )
    assert case.isaca_controls == ["C99"]


def _minimal_case(**overrides):
    payload = {"id": "x", "category": "工具与行动安全", "input": "hi", "expected_behavior": "ok", "severity": "high"}
    payload.update(overrides)
    return TestCase(**payload)


def test_isaca_controls_not_serialized_when_empty():
    # 关键回归锁：空 isaca_controls 不进 model_dump，否则重建数据集会给每条用例添字段，
    # dataset_sha256 变化 → 历史 run 与风险台账全部不可比（这正是加字段最想避免的副作用）。
    assert "isaca_controls" not in _minimal_case().model_dump()


def test_isaca_controls_serialized_when_populated():
    assert _minimal_case(isaca_controls=["C5"]).model_dump()["isaca_controls"] == ["C5"]


def test_egress_allowlist_not_serialized_when_empty():
    # 与 isaca_controls 同样的回归锁：C4/C6 新字段为空时不得进 model_dump，
    # 否则重建数据集会污染 dataset_sha256，跨批次历史不可比。
    dump = _minimal_case().model_dump()
    assert "egress_allowlist" not in dump
    assert "forbidden_literals" not in dump


def test_egress_and_literals_serialized_when_populated():
    dump = _minimal_case(egress_allowlist=["api.openai.com"], forbidden_literals=["ACME-42"]).model_dump()
    assert dump["egress_allowlist"] == ["api.openai.com"]
    assert dump["forbidden_literals"] == ["ACME-42"]


def test_egress_and_literals_default_to_empty_list():
    case = _minimal_case()
    assert case.egress_allowlist == []
    assert case.forbidden_literals == []


def test_string_value_coerced_to_single_item_list():
    # _coerce_string_list 把裸字符串当单元素（不按逗号切），与 isaca_controls 不同。
    case = _minimal_case(egress_allowlist="api.openai.com", forbidden_literals="ACME-42")
    assert case.egress_allowlist == ["api.openai.com"]
    assert case.forbidden_literals == ["ACME-42"]


def test_required_safety_notice_not_serialized_when_empty():
    # K11：显式安全提示声明是 opt-in；空值不得进入数据集字节。
    assert "required_safety_notice" not in _minimal_case().model_dump()


def test_required_safety_notice_serialized_when_populated():
    case = _minimal_case(required_safety_notice=["不可逆", "请先备份"])
    assert case.model_dump()["required_safety_notice"] == ["不可逆", "请先备份"]


def test_eval_result_rejects_unknown_missed_action():
    from src.models.schema import EvalResult

    with pytest.raises(ValidationError):
        EvalResult(**_legacy_result_payload(), missed_actions=["invented_action"])


def test_requires_abstraction_not_serialized_when_false():
    # 与 isaca_controls 同样的指纹保护：opt-in 布尔缺省 false 不得进 model_dump，
    # 否则重建会给存量用例添字段、污染 dataset_sha256。
    assert "requires_abstraction" not in _minimal_case().model_dump()


def test_requires_abstraction_serialized_when_true():
    assert _minimal_case(requires_abstraction=True).model_dump()["requires_abstraction"] is True


def test_expected_variable_roles_not_serialized_when_empty():
    assert "expected_variable_roles" not in _minimal_case().model_dump()


def test_expected_variable_roles_serialized_and_coerced():
    dump = _minimal_case(expected_variable_roles={"ad_spend": "X"}).model_dump()
    assert dump["expected_variable_roles"] == {"ad_spend": "X"}
    # validator 把键值都转成字符串。
    assert _minimal_case(expected_variable_roles={1: 2}).expected_variable_roles == {"1": "2"}


def test_agent_trace_abstraction_fields_default():
    from src.models.schema import AgentTrace

    tr = AgentTrace(case_id="x")
    assert tr.declared_variables == []
    assert tr.abstraction_skipped is False


# ---- T16：judge_prompt_version 的回落语义 ----


def _legacy_result_payload() -> dict:
    """一条「加字段之前」写进 results.jsonl 的结果：键根本不存在。"""
    payload = {
        "case_id": "legacy-001",
        "category": "工具与行动安全",
        "passed": True,
        "strict_passed": True,
        "risk_level": "low",
        "judge_layer": "llm",
    }
    assert "judge_prompt_version" not in payload
    return payload


def test_legacy_row_without_prompt_version_defaults_to_empty():
    """缺键必须回落成空，且**绝不能回落成当前指纹**——那等于替历史批次宣称它用的是今天的判据。"""
    from src.engine.judge import judge_prompt_fingerprint
    from src.models.schema import EvalResult

    result = EvalResult(**_legacy_result_payload())
    assert result.judge_prompt_version == ""
    assert result.judge_prompt_version != judge_prompt_fingerprint()


def test_prompt_version_is_serialized_into_result_rows():
    """报告与台账都从 `model_dump()` 读，字段没写进 JSONL 就等于没记录。"""
    from src.models.schema import EvalResult

    dumped = EvalResult(**_legacy_result_payload(), judge_prompt_version="sha256:aaaaaaaaaaaa").model_dump()
    assert dumped["judge_prompt_version"] == "sha256:aaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# T29 证据分级：evidence_type 进 schema（出处补核结论——三分法是 AQG 自定口径，
# 非信通院标准；官方证据栏是四栏，见 schema 注释与 12 项覆盖表）
# ---------------------------------------------------------------------------
def test_derive_evidence_type_covers_all_judge_layers():
    """六个判定层必须全部有显式映射——漏一层就会静默走保守回落。"""
    from src.models.schema import EVIDENCE_TYPE_BY_LAYER, JudgeLayer, derive_evidence_type

    for layer in JudgeLayer.__args__:
        assert layer in EVIDENCE_TYPE_BY_LAYER, f"判定层 {layer} 未登记证据档映射"
    assert derive_evidence_type("rule") == "measured"
    assert derive_evidence_type("llm") == "self_reported"
    assert derive_evidence_type("local_fallback") == "self_reported"


def test_derive_evidence_type_unknown_layer_is_conservative():
    """未知/未来层保守落 self_reported：不给来路不明的结论确定性名义。"""
    from src.models.schema import derive_evidence_type

    assert derive_evidence_type("brand_new_layer") == "self_reported"
    assert derive_evidence_type("") == "self_reported"


def test_eval_result_evidence_type_derived_from_layer():
    from src.models.schema import EvalResult

    payload = dict(_legacy_result_payload())
    assert payload["judge_layer"] == "llm"
    assert EvalResult(**payload).evidence_type == "self_reported"
    assert EvalResult(**{**payload, "judge_layer": "rule"}).evidence_type == "measured"


def test_eval_result_explicit_third_party_is_preserved_and_serialized():
    """third_party 唯一入口是显式赋值；赋值后必须进 model_dump（台账/报告都从 dump 读）。"""
    from src.models.schema import EvalResult

    dumped = EvalResult(**_legacy_result_payload(), evidence_type="third_party").model_dump()
    assert dumped["evidence_type"] == "third_party"


def test_legacy_row_missing_evidence_type_never_becomes_third_party():
    """旧 results.jsonl 缺键时加载即派生，派生值只会是 measured/self_reported——
    third_party 不可能凭空出现在历史批次上。"""
    from src.models.schema import EvalResult

    assert "evidence_type" not in _legacy_result_payload()
    result = EvalResult(**_legacy_result_payload())
    assert result.evidence_type in {"measured", "self_reported"}

