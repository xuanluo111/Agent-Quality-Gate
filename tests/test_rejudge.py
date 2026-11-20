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


def test_rebuild_ledger_preserves_lineage_and_marks_breakpoint(tmp_path, monkeypatch):
    """重放重建台账时顶层 lineage 元数据不能被抹：真实切代必须留下 generation_breakpoint。

    回归 rejudge 曾把写回简化成 {"entries": kept}，丢掉 dataset_epochs/prev_dataset_epoch，
    使 f687 -> 新代 的真实切代被误判成“一直在新代”（breakpoint=False、代际史归零）。
    """
    ledger = tmp_path / "risk_ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "dataset_epoch": "f6871e251a276a69",
                "dataset_epochs": [
                    {"epoch": "5fb7bc27549ab6af", "first_recorded_at": "2026-10-28 12:53:23"},
                    {"epoch": "f6871e251a276a69", "first_recorded_at": "2026-10-28 15:52:20"},
                ],
                "entries": {"other::keep": _entry("keep", ["h1"])},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rejudge, "LEDGER", ledger)
    monkeypatch.setattr(rejudge, "BACKUP_ROOT", tmp_path / "bak")

    state = _state("glm", "v1", [make_result(case_id="v1-pass")])
    state["old_summary"]["dataset_hash"] = "db2add8d0d06f780" + "0" * 48
    rebuild_ledger([state])

    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["prev_dataset_epoch"] == "f6871e251a276a69"
    assert data["generation_breakpoint"] is True
    # 代际史：保留旧两代 + 追加本次重放的新代，顺序稳定可溯源
    assert [h["epoch"] for h in data["dataset_epochs"]] == [
        "5fb7bc27549ab6af",
        "f6871e251a276a69",
        "db2add8d0d06f780",
    ]
    # 非本次 scope 的历史条目不能被带走
    assert "other::keep" in data["entries"]


def test_rebuild_ledger_prefers_declared_generation_over_file_fingerprint(tmp_path, monkeypatch):
    """重放时“声明的代际”必须压过“按文件指纹重算”。

    探针集是 core 语料的子集视图，文件指纹不同。按指纹重算会把它当成下一代数据集，
    在台账里伪造一次代际切换，并把跨代劣化护栏（同一代内 resolved→open 才算劣化）
    的基准指到子集文件上。
    """
    ledger = tmp_path / "risk_ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "dataset_epoch": "2fd4a5646accb8d7",
                "dataset_epochs": [{"epoch": "2fd4a5646accb8d7", "first_recorded_at": "2026-11-05 18:27:07"}],
                "entries": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rejudge, "LEDGER", ledger)
    monkeypatch.setattr(rejudge, "BACKUP_ROOT", tmp_path / "bak")

    state = _state("glm", "v1", [make_result(case_id="v1-pass")])
    # 子集视图的文件指纹（另一个代）
    state["old_summary"]["dataset_hash"] = "a014b04076fd14c3" + "0" * 48
    state["old_summary"]["dataset_epoch"] = "a014b04076fd14c3"
    # 但 run 自己声明了它属于语料本代
    state["meta"]["dataset_generation"] = "deff7501007cff61"

    rebuild_ledger([state])

    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["dataset_epoch"] == "deff7501007cff61"
    assert data["prev_dataset_epoch"] == "2fd4a5646accb8d7"
    # 声明的是语料本代 → 相对上一代是真换代，相对台账当前代不是伪造切换
    assert data["switched_this_run"] is True
    assert "a014b04076fd14c3" not in [h["epoch"] for h in data["dataset_epochs"]]


def test_build_run_meta_carries_declared_generation():
    """声明代际要从 config.json 传进 run_meta，否则重放时又退回按指纹算。"""
    state = {
        "config": {"model": "glm-4.5-flash", "run_id": "v1", "dataset_generation": "DEFF7501007CFF61"},
        "old_summary": {"generated_at": "2026-11-06 12:00:00"},
    }
    assert rejudge.build_run_meta(state)["dataset_generation"] == "deff7501007cff61"


def test_build_run_meta_and_baseline_carry_judge_kappa():
    """重判重建报告必须带出 κ：两侧都丢了会退回 unverified，同一批结果从 warn 翻回 blocked。"""
    state = {
        "config": {"model": "glm-4.5-flash", "run_id": "v1", "judge_kappa": 0.70},
        "old_summary": {"generated_at": "2026-11-06 12:00:00"},
    }
    assert rejudge.build_run_meta(state)["judge_kappa"] == 0.70
    assert rejudge.baseline(state)["judge_kappa"] == 0.70


def test_legacy_run_without_judge_kappa_stays_unverified():
    """旧 run 的 config.json 没这个字段：回落 None 即 opt-in 未启用，行为不变。"""
    state = {
        "config": {"model": "glm-4.5-flash", "run_id": "v1"},
        "old_summary": {"generated_at": "2026-11-06 12:00:00"},
    }
    assert rejudge.build_run_meta(state)["judge_kappa"] is None
    assert rejudge.baseline(state)["judge_kappa"] is None


def test_retry_delay_backs_off_exponentially_on_rate_limit():
    """免费档撞 429 要等分钟级窗口，1/2/3 秒的重试等于白判成 fallback。"""
    evidence = "Error code: 429 - {'error': {'code': '1302', 'message': '您的账户已达到速率限制'}}"
    assert rejudge.retry_delay(0, evidence) == 10
    assert rejudge.retry_delay(1, evidence) == 20
    assert rejudge.retry_delay(2, evidence) == 40


def test_retry_delay_caps_at_ceiling():
    assert rejudge.retry_delay(10, "rate limit exceeded") == rejudge.RATE_LIMIT_BACKOFF_CAP


def test_retry_delay_treats_chinese_concurrency_limit_as_environment():
    assert rejudge.retry_delay(0, "当前并发上限为 1") == rejudge.RATE_LIMIT_BACKOFF_BASE


def test_retry_delay_keeps_short_wait_for_non_environment_errors():
    """配置错、响应解析失败等，等多久都不会好，保持短退避尽快暴露。"""
    assert rejudge.retry_delay(0, "未配置裁判客户端") == 1
    assert rejudge.retry_delay(2, "裁判没有返回有效样本") == 3


def test_checkpoint_interval_is_positive():
    assert rejudge.CHECKPOINT_EVERY > 0


def test_with_thinking_disabled_injects_flag():
    """glm-4.5 系列带 thinking 单次 13~22 秒，关掉后 6.7 秒。"""
    assert rejudge.with_thinking_disabled(None) == {"thinking": {"type": "disabled"}}
    assert rejudge.with_thinking_disabled({"foo": 1}) == {
        "foo": 1,
        "thinking": {"type": "disabled"},
    }


def test_with_thinking_disabled_respects_caller_setting():
    """调用方显式指定了 thinking 就不该被悄悄覆盖。"""
    assert rejudge.with_thinking_disabled({"thinking": {"type": "enabled"}}) == {
        "thinking": {"type": "enabled"}
    }


def test_is_json_parse_error_detects_broken_json():
    assert rejudge.is_json_parse_error("Expecting ',' delimiter: line 10 column 46 (char 337)")
    assert rejudge.is_json_parse_error("JSONDecodeError: Expecting value")


def test_is_json_parse_error_ignores_rate_limit():
    """429 不该走降级路径：换裁判一样限流，该退避而不是换模型配置。"""
    assert not rejudge.is_json_parse_error("Error code: 429 - {'error': {'code': '1302'}}")
    assert not rejudge.is_json_parse_error("")


# ---- T16：判据指纹在重判链路上的语义 ----


def _strip_prompt_version(run_dir) -> None:
    """把 run 里的结果改成「T16 之前写的」：键不存在，不是存在但为空。"""
    lines = []
    for raw in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        row["result"].pop("judge_prompt_version", None)
        lines.append(json.dumps(row, ensure_ascii=False))
    (run_dir / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_legacy_row_without_prompt_version_prepares_clean(tmp_path):
    """历史 run 缺这个键必须能照常载入，且回落成空——空才是「当时没记」的诚实表达。"""
    run_dir = _write_run(tmp_path, [make_result(case_id="case-001", judge_layer="llm")])
    _strip_prompt_version(run_dir)

    state = rejudge.prepare_run(run_dir)

    assert state["results"][0].judge_prompt_version == ""


def test_dump_results_keeps_prompt_version(tmp_path):
    """重判写回走的是 `model_dump()`，指纹没落盘就等于整条追溯链断在最后一步。"""
    judged = make_result(case_id="case-001", judge_layer="llm", judge_prompt_version="sha256:1234567890ab")
    run_dir = _write_run(tmp_path, [judged])

    state = rejudge.prepare_run(run_dir)
    rejudge.dump_results(state)

    assert _read_results(run_dir)[0]["judge_prompt_version"] == "sha256:1234567890ab"


def test_rejudge_run_stamps_current_fingerprint_instead_of_blank(tmp_path, monkeypatch):
    """重判后不能留空，也不能沿用旧值：重判最常见的动机就是「判据改了」，
    写上新指纹才能回答「这几条是哪版 prompt 判的」。"""
    from src.engine.judge import LayeredJudge, judge_prompt_fingerprint
    from tests.conftest import FakeClient, judge_response

    run_dir = _write_run(tmp_path, [make_result(case_id="case-001", judge_layer="llm")])
    _strip_prompt_version(run_dir)
    state = rejudge.prepare_run(run_dir)
    monkeypatch.setattr(
        rejudge,
        "build_judge",
        lambda *args, **kwargs: LayeredJudge(
            client=FakeClient([judge_response(True, True)]), model="judge", samples=1
        ),
    )

    stats = rejudge.rejudge_run(state, "key", 1, 0, 0, True)

    assert stats["targets"] == 1
    assert _read_results(run_dir)[0]["judge_prompt_version"] == judge_prompt_fingerprint()
