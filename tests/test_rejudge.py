import json

import rejudge
from rejudge import build_judge, rebuild_ledger

from tests.conftest import make_result


def test_legacy_config_does_not_retro_apply_provenance():
    """老批次根本没有血缘语义，重判时默认关闭，否则历史结果会凭空多出 Fabricated_Parameter。"""
    assert build_judge({}, "key").provenance_enabled is False


def test_new_config_keeps_its_provenance_setting():
    assert build_judge({"provenance_enabled": True}, "key").provenance_enabled is True
    assert build_judge({"provenance_enabled": False}, "key").provenance_enabled is False


def test_cli_override_wins_over_config():
    assert build_judge({}, "key", provenance_override=True).provenance_enabled is True
    assert build_judge({"provenance_enabled": True}, "key", provenance_override=False).provenance_enabled is False


def test_collateral_risk_falls_back_to_high_and_can_be_overridden():
    assert build_judge({}, "key").collateral_risk == "high"
    assert build_judge({"collateral_risk": "critical"}, "key").collateral_risk == "critical"
    assert build_judge({}, "key", collateral_risk_override="medium").collateral_risk == "medium"


def test_invalid_collateral_risk_falls_back_to_high():
    assert build_judge({"collateral_risk": "urgent"}, "key").collateral_risk == "high"


def _state(model: str, run_id: str, results: list) -> dict:
    return {
        "meta": {"model": model, "run_id": run_id},
        "old_summary": {"generated_at": "2026-10-24 22:18:00"},
        "results": results,
    }


def _entry(case_id: str, runs: list, occurrences: int = 1, first: str | None = None) -> dict:
    return {
        "case_id": case_id,
        "risk_level": "high",
        "status": "open",
        "first_seen_run": first or runs[0],
        "runs": list(runs),
        "occurrences": occurrences,
    }


def test_rebuild_ledger_keeps_other_runs_of_the_same_model(tmp_path, monkeypatch):
    """只重判一个 run，同模型其它 run 的历史条目不能被带走。

    台账只按模型分区，按模型清空时 glm 的正式批次条目（first_seen_run 指向别处）
    会跟着消失；实测丢过 55 条。
    """
    ledger = tmp_path / "risk_ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "entries": {
                    "glm::old-risk": _entry("old-risk", ["h1"], occurrences=2),
                    "glm::selfcheck-risk": _entry("selfcheck-risk", ["v1"]),
                    "other::untouched": _entry("untouched", ["h2"]),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rejudge, "LEDGER", ledger)
    monkeypatch.setattr(rejudge, "BACKUP_ROOT", tmp_path / "bak")

    rebuild_ledger([_state("glm", "v1", [make_result(case_id="v1-pass")])])

    entries = json.loads(ledger.read_text(encoding="utf-8"))["entries"]
    assert "glm::old-risk" in entries
    assert entries["glm::old-risk"]["occurrences"] == 2
    assert "other::untouched" in entries
    # 本次 run 自己造出来的条目：重判结果已通过，不该再留在台账里。
    assert "glm::selfcheck-risk" not in entries


def test_rebuild_ledger_trims_the_replayed_run_only(tmp_path, monkeypatch):
    """条目同时被历史 run 和本次 run 命中时，只扣掉本次那一次，历史计数要留住。"""
    ledger = tmp_path / "risk_ledger.json"
    ledger.write_text(
        json.dumps({"entries": {"glm::shared": _entry("shared", ["h1", "v1"], occurrences=2)}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(rejudge, "LEDGER", ledger)
    monkeypatch.setattr(rejudge, "BACKUP_ROOT", tmp_path / "bak")

    rebuild_ledger([_state("glm", "v1", [make_result(case_id="shared")])])

    entry = json.loads(ledger.read_text(encoding="utf-8"))["entries"]["glm::shared"]
    assert entry["occurrences"] == 1
    assert entry["runs"] == ["h1"]


def test_rebuild_ledger_returns_empty_when_nothing_to_replay(tmp_path, monkeypatch):
    ledger = tmp_path / "risk_ledger.json"
    monkeypatch.setattr(rejudge, "LEDGER", ledger)
    monkeypatch.setattr(rejudge, "BACKUP_ROOT", tmp_path / "bak")

    assert rebuild_ledger([]) == {}
    assert not ledger.exists()
