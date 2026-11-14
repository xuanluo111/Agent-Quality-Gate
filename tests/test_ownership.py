"""M1 责任入账的口径测试。

这两个字段最容易悄悄失效的地方不是"没写"，而是"写了但不可追溯"：签字人不在名单、
缺理由、跨代沿用旧签字——三种情况都比不签更糟，因为它们在报告里看起来像已经处理过。
所以这里既测"该派发的派了"，也测"无效签字拦得住"。
"""
import json

import pytest

from scripts import verify
from src.engine import ownership as ow
from src.engine.report import backfill_ledger_owners


def make_entry(**overrides):
    entry = {
        "case_id": "case-a",
        "model": "spark-x2.5-4b",
        "category": "工具与行动安全",
        "risk_level": "critical",
        "status": "open",
        "occurrences": 1,
    }
    entry.update(overrides)
    return entry


# --------------------------------------------------------------------------
# 派发
# --------------------------------------------------------------------------
def test_owner_for_known_category():
    assert ow.owner_for("运行环境与隔离") == "Harness 编排层"
    assert ow.owner_for("数据与上下文安全") == "Harness 自检层"
    assert ow.owner_for("数据分析与计算") == "模型/工具调用层"


def test_owner_for_unknown_category_is_unassigned():
    assert ow.owner_for("某个新类别") == ow.UNASSIGNED
    assert ow.owner_for("") == ow.UNASSIGNED


def test_every_dispatch_target_is_a_valid_role():
    # 派发表的值必须都在 OWNER_ROLES 里，否则 verify 会把它们当 unassigned。
    for category, role in ow.CATEGORY_OWNER.items():
        assert role in ow.OWNER_ROLES, f"{category} -> {role} 不是合法角色"


def test_assign_owner_fills_from_category_map():
    entry = ow.assign_owner(make_entry())
    assert entry["owner"] == "Harness 编排层"
    assert entry["owner_source"] == "category_map"


def test_assign_owner_keeps_manual_reassignment():
    # 人工把某条改派给别的角色后，重跑不得覆盖回去。
    entry = ow.assign_owner(make_entry(owner="Harness 自检层"))
    assert entry["owner"] == "Harness 自检层"
    assert entry["owner_source"] == "manual"


def test_assign_owner_overwrites_illegal_value():
    # 人名或拼错的值不是合法角色，按派发表重置——否则台账里会混进无法统计的 owner。
    entry = ow.assign_owner(make_entry(owner="xuanluo111"))
    assert entry["owner"] == "Harness 编排层"
    assert entry["owner_source"] == "category_map"


def test_assign_owner_marks_unassigned_for_unknown_category():
    entry = ow.assign_owner(make_entry(category="另一个新类别"))
    assert entry["owner"] == ow.UNASSIGNED
    assert entry["owner_source"] == "unassigned"


# --------------------------------------------------------------------------
# 签字状态
# --------------------------------------------------------------------------
def test_acceptance_state_unsigned_by_default():
    assert ow.acceptance_state(make_entry()) == "unsigned"


def test_acceptance_state_signed():
    entry = make_entry(
        risk_accepted_by="xuanluo111",
        accept_reason="已知误判，等待重构判据时一并处理",
        accepted_epoch="deff7501007cff61",
    )
    assert ow.acceptance_state(entry, "deff7501007cff61") == "signed"


def test_acceptance_state_invalid_actor():
    entry = make_entry(risk_accepted_by="someone-else", accept_reason="x", accepted_epoch="e")
    assert ow.acceptance_state(entry, "e") == "invalid_actor"


def test_acceptance_state_self_accept():
    # owner 与签字人相同即自己给自己免责。
    entry = make_entry(owner="xuanluo111", risk_accepted_by="xuanluo111", accept_reason="x", accepted_epoch="e")
    assert ow.acceptance_state(entry, "e") == "self_accept"


def test_acceptance_state_missing_reason():
    entry = make_entry(risk_accepted_by="xuanluo111", accepted_epoch="e")
    assert ow.acceptance_state(entry, "e") == "missing_reason"


def test_acceptance_state_expired_across_epoch():
    # 签字只在签它的那一代有效：换了数据集还沿用旧签字 = 用旧判断背书新题面。
    entry = make_entry(risk_accepted_by="xuanluo111", accept_reason="x", accepted_epoch="old-epoch")
    assert ow.acceptance_state(entry, "deff7501007cff61") == "expired"


def test_acceptance_state_invalid_actor_outranks_expired():
    # 过期的无效签字应报 invalid_actor，避免引导人去续期一个本不该存在的签字。
    entry = make_entry(risk_accepted_by="nobody", accept_reason="", accepted_epoch="old-epoch")
    assert ow.acceptance_state(entry, "new-epoch") == "invalid_actor"


def test_needs_acceptance_only_critical_open():
    assert ow.needs_acceptance(make_entry())
    assert ow.needs_acceptance(make_entry(status="regressed"))
    assert not ow.needs_acceptance(make_entry(risk_level="high"))
    assert not ow.needs_acceptance(make_entry(status="resolved"))


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def test_summary_separates_unassigned_and_invalid_from_unsigned():
    entries = [
        make_entry(case_id="ok"),
        make_entry(case_id="unowned", category="未知类别"),
        make_entry(case_id="bad-sign", risk_accepted_by="stranger", accept_reason="x", accepted_epoch="e"),
        make_entry(case_id="unsigned"),
    ]
    for entry in entries:
        ow.assign_owner(entry)
    summary = ow.ownership_summary(entries, "e")
    assert summary["unassigned"] == ["spark-x2.5-4b::unowned"]
    assert [item["state"] for item in summary["invalid_acceptances"]] == ["invalid_actor"]
    assert "spark-x2.5-4b::unsigned" in summary["unsigned_critical"]
    assert summary["needs_acceptance"] == 4


def test_summary_ignores_non_dict_rows():
    summary = ow.ownership_summary([make_entry(), "not-a-dict"])
    assert summary["total"] == 1


def test_render_acceptance_labels():
    assert ow.render_acceptance(make_entry()) == "unsigned"
    assert ow.render_acceptance(make_entry(risk_level="high")) == "-"
    signed = make_entry(risk_accepted_by="xuanluo111", accept_reason="x", accepted_epoch="e")
    assert ow.render_acceptance(signed, "e") == "signed(xuanluo111)"


# --------------------------------------------------------------------------
# 存量回填
# --------------------------------------------------------------------------
def _write_ledger(path, entries):
    ledger = {
        "dataset_epoch": "deff7501007cff61",
        "entries": {f"k{i}": dict(entry) for i, entry in enumerate(entries)},
        "open_risks": [dict(entry) for entry in entries if entry.get("status") in {"open", "regressed"}],
    }
    path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    return ledger


def test_backfill_fills_entries_and_open_risks(tmp_path):
    path = tmp_path / "risk_ledger.json"
    _write_ledger(path, [make_entry(), make_entry(case_id="b", status="resolved")])
    ledger = backfill_ledger_owners(path)
    assert all(entry["owner"] for entry in ledger["entries"].values())
    # open_risks 反序列化后是独立对象：只改 entries 会让台账里两处不一致。
    assert all("owner" in entry for entry in ledger["open_risks"])


def test_backfill_is_idempotent_and_keeps_manual(tmp_path):
    path = tmp_path / "risk_ledger.json"
    _write_ledger(path, [make_entry(), make_entry(case_id="b", owner="Harness 自检层")])
    backfill_ledger_owners(path)
    ledger = backfill_ledger_owners(path)
    assert ledger["entries"]["k0"]["owner"] == "Harness 编排层"
    assert ledger["entries"]["k1"]["owner"] == "Harness 自检层"
    assert ledger["entries"]["k1"]["owner_source"] == "manual"


# --------------------------------------------------------------------------
# verify 编排：只拦"没人负责"与"无效签字"，不拦"还没签"
# --------------------------------------------------------------------------
def test_step_risk_ownership_skips_without_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    step = verify.step_risk_ownership()
    assert step.ok and "跳过" in step.detail


def test_step_risk_ownership_fails_on_unassigned(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    (tmp_path / "reports").mkdir()
    _write_ledger(tmp_path / "reports" / "risk_ledger.json", [make_entry(category="未知类别")])
    step = verify.step_risk_ownership()
    assert not step.ok and "未派发责任方" in step.detail


def test_step_risk_ownership_fails_on_invalid_acceptance(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    (tmp_path / "reports").mkdir()
    _write_ledger(
        tmp_path / "reports" / "risk_ledger.json",
        [make_entry(risk_accepted_by="stranger", accept_reason="x", accepted_epoch="e")],
    )
    step = verify.step_risk_ownership()
    assert not step.ok and "无效签字" in step.detail


def test_step_risk_ownership_passes_but_reports_unsigned_critical(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    (tmp_path / "reports").mkdir()
    _write_ledger(tmp_path / "reports" / "risk_ledger.json", [make_entry(), make_entry(case_id="b")])
    step = verify.step_risk_ownership()
    # 未签字是待办清单，不是门禁失败——否则每轮跑批都会被逼着批量签字。
    assert step.ok and "待签 Critical 2" in step.detail


def test_repository_ledger_has_no_unassigned():
    """仓库里的真实台账必须全部派发完毕，防止新增类别后没人接手。"""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "reports" / "risk_ledger.json"
    if not path.is_file():
        pytest.skip("台账尚未生成")
    ledger = json.loads(path.read_text(encoding="utf-8"))
    summary = ow.ownership_summary(list(ledger["entries"].values()), ledger.get("dataset_epoch", ""))
    assert summary["unassigned"] == []
    assert summary["invalid_acceptances"] == []
