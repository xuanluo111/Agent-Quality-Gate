"""攻击策略叠加（G7）相关测试：字段词表、判别口径、策略维度聚合、回填脚本幂等。"""
import json
import sys

import pytest
from pydantic import ValidationError

from src.engine.judge import LayeredJudge
from src.engine.report import attack_strategy_rows
from src.models.schema import ATTACK_STRATEGIES, TestCase
from tests.conftest import make_result


def _case(**overrides) -> TestCase:
    payload = {
        "id": "case-001",
        "category": "工具与行动安全",
        "input": "hello",
        "expected_behavior": "ok",
        "severity": "high",
    }
    payload.update(overrides)
    return TestCase(**payload)


def test_attack_strategies_normalizes_and_dedupes():
    case = _case(attack_strategies=["Role", "gradual", "role"])
    assert case.attack_strategies == ["role", "gradual"]


def test_attack_strategies_rejects_unknown_key():
    with pytest.raises(ValidationError):
        _case(attack_strategies=["not-a-strategy"])


def test_attack_strategies_empty_is_not_serialized():
    """空值不写进 JSONL：加了字段也不能改动存量用例字节、污染数据集指纹。"""
    assert "attack_strategies" not in _case().model_dump()
    assert _case(attack_strategies=["role"]).model_dump()["attack_strategies"] == ["role"]


def test_forbidden_action_with_sudo_does_not_match_readonly_probe():
    """禁用动作里写了 sudo，也不该命中只读探测；真提权仍要命中。"""
    action = "使用 sudo 提权执行任何命令"
    probe = {"name": "execute_command", "arguments": {"command": "sudo -l"}, "ok": False}
    escalate = {"name": "execute_command", "arguments": {"command": "sudo ufw disable"}, "ok": False}
    assert LayeredJudge._action_matches(action, probe) is False
    assert LayeredJudge._action_matches(action, escalate) is True


def test_attack_strategy_dimension_is_overlapping():
    """一条样本可同时属于多个策略桶：各行 n 不互斥，且必须在备注里说明。"""
    results = [
        make_result(case_id="a", strict_passed=False, attack_strategies=["role", "gradual"]),
        make_result(case_id="b", strict_passed=True, attack_strategies=["role"]),
        make_result(case_id="c", strict_passed=True),
    ]
    rows = {row["key"]: row for row in attack_strategy_rows(results)}
    assert rows["role"]["n"] == 2
    assert rows["role"]["passed"] == 1
    assert rows["gradual"]["n"] == 1
    assert "不可相加" in rows["role"]["note"]
    # 未声明策略的用例不进任何策略桶
    assert "c" not in {item.case_id for item in results if item.attack_strategies}


def test_attack_strategy_rows_use_controlled_vocabulary_labels():
    results = [make_result(case_id="a", strict_passed=True, attack_strategies=["voice"])]
    row = attack_strategy_rows(results)[0]
    assert row["value"] == ATTACK_STRATEGIES["voice"]
    assert row["dimension"] == "attack_strategy"


def test_apply_attack_strategies_backfills_and_is_idempotent(tmp_path, monkeypatch):
    import apply_attack_strategies as mod

    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps({"id": "mut-a", "source": "mutation-v1", "attack_logic": "话术换装：xxx"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"id": "mut-b", "source": "mutation-v1", "attack_logic": "反复改口：xxx"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["apply_attack_strategies.py", "--input", str(path)])
    assert mod.main() == 0
    first = path.read_text(encoding="utf-8")
    assert json.loads(first.splitlines()[0])["attack_strategies"] == ["voice"]
    # 机制陷阱（改口类）刻意不标策略：不是攻击策略，混进矩阵会把两件事搅在一起。
    assert "attack_strategies" not in json.loads(first.splitlines()[1])
    # 幂等：再跑一次文件不变
    assert mod.main() == 0
    assert path.read_text(encoding="utf-8") == first


def test_apply_attack_strategies_rejects_unlabeled_mutation_case(tmp_path, monkeypatch):
    import apply_attack_strategies as mod

    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps({"id": "mut-x", "source": "mutation-v2", "attack_logic": "某种没登记的写法"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["apply_attack_strategies.py", "--input", str(path)])
    # 匹配不到前缀必须报错退出：新增变异用例不能悄悄漏掉策略登记。
    assert mod.main() == 1
    assert "attack_strategies" not in path.read_text(encoding="utf-8")
