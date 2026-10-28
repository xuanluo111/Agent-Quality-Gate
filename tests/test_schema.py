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
