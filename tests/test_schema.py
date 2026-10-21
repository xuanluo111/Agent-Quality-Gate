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
