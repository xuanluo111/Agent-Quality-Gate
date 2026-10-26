import json

import rejudge
from rejudge import build_judge, rebuild_ledger

from src.engine.failure_class import UNKNOWN
from tests.conftest import make_case, make_result, make_trace


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


def _write_run(tmp_path, results):
    """铺一个最小可用的 run 目录：config / summary / results 三件套。"""
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        "\n".join(
            json.dumps(make_case(id=item.case_id).model_dump(), ensure_ascii=False) for item in results
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "config.json").write_text(
        json.dumps({"dataset": str(dataset), "run_id": "run-001", "workspace_root": str(tmp_path / "ws")}),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(json.dumps({}), encoding="utf-8")
    rows = [
        json.dumps(
            {"result": item.model_dump(), "trace": make_trace(case_id=item.case_id).model_dump()},
            ensure_ascii=False,
        )
        for item in results
    ]
    (run_dir / "results.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return run_dir


def _read_results(run_dir):
    return [
        json.loads(line)["result"]
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_dump_results_restores_failure_class(tmp_path):
    """`failure_class` 只在 run_eval 里算，重判写回时必须重算。

    丢了它 gate 就分不清「前置条件不满足 / 没测到」，这两类样本会被算进通过率分母。
    """
    untested = make_result(
        case_id="case-trunc",
        passed=False,
        strict_passed=False,
        fail_reason="Output_Truncated",
        data_valid=False,
        failure_class=None,
    )
    healthy = make_result(case_id="case-pass", failure_class=None)
    run_dir = _write_run(tmp_path, [untested, healthy])

    state = rejudge.prepare_run(run_dir)
    rejudge.dump_results(state)

    restored = {item["case_id"]: item["failure_class"] for item in _read_results(run_dir)}
    assert restored == {"case-trunc": UNKNOWN, "case-pass": None}


def test_dump_results_keeps_existing_class(tmp_path):
    """已有归类不能被重算改写成别的值，否则等于换了口径却不留痕。"""
    untested = make_result(
        case_id="case-trunc",
        passed=False,
        strict_passed=False,
        fail_reason="Output_Truncated",
        data_valid=False,
        failure_class=UNKNOWN,
    )
    run_dir = _write_run(tmp_path, [untested])

    state = rejudge.prepare_run(run_dir)
    rejudge.dump_results(state)

    assert _read_results(run_dir)[0]["failure_class"] == UNKNOWN
