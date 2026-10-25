import hashlib

import pytest
from pydantic import ValidationError

from src.engine.state import (
    check_state_assertions,
    correction_start_step,
    deprecated_values_for,
    diff_states,
    evaluate_assertions,
    evaluate_state,
    extract_corrections,
    infer_completion,
    matches_any,
    scan_state_pollution,
    snapshot_workspace,
    unexpected_changes,
)
from src.models.schema import StateAssertion
from tests.conftest import make_case, make_trace


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_snapshot_and_diff_detect_created_modified_deleted(tmp_path):
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    before = snapshot_workspace(tmp_path)

    (tmp_path / "a.txt").write_text("v2", encoding="utf-8")
    (tmp_path / "b.txt").write_text("new", encoding="utf-8")
    after = snapshot_workspace(tmp_path)

    diff = diff_states(before, after)
    assert diff["created"] == ["b.txt"]
    assert diff["modified"] == ["a.txt"]
    assert diff["deleted"] == []
    assert after["a.txt"] == sha("v2")

    (tmp_path / "a.txt").unlink()
    assert diff_states(before, snapshot_workspace(tmp_path))["deleted"] == ["a.txt"]


def test_snapshot_missing_directory_returns_empty(tmp_path):
    assert snapshot_workspace(tmp_path / "nope") == {}


def test_snapshot_ignores_directories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.txt").write_text("x", encoding="utf-8")
    snapshot = snapshot_workspace(tmp_path)
    assert set(snapshot) == {"sub/file.txt"}


def test_matches_any_supports_glob_and_dir_prefix():
    assert matches_any("logs/app.log", ["logs/**"])
    assert matches_any("out/report.md", ["*.md"])
    assert not matches_any("secret.env", ["logs/**", "*.md"])


def test_unexpected_changes_only_lists_declared_violations():
    diff = {"created": ["logs/a.log"], "modified": ["core.py"], "deleted": ["old.tmp"]}
    assert unexpected_changes(diff, ["logs/**"]) == ["modified:core.py", "deleted:old.tmp"]


def test_state_assertions_cover_all_ops(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("结论：通过\n", encoding="utf-8")
    (workspace / "done.flag").write_text("1", encoding="utf-8")
    snapshot = snapshot_workspace(workspace)

    case = make_case(
        state_assertions=[
            {"path": "report.md", "op": "exists"},
            {"path": "missing.md", "op": "not_exists"},
            {"path": "report.md", "op": "sha256", "value": sha("结论：通过\n")},
            {"path": "report.md", "op": "equals", "value": "结论：通过"},
            {"path": "report.md", "op": "contains", "value": "通过"},
        ]
    )
    assert check_state_assertions(case, snapshot, workspace) == []


def test_state_assertion_failures_are_reported_with_detail(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("结论：失败\n", encoding="utf-8")
    (workspace / "kept.txt").write_text("keep", encoding="utf-8")

    case = make_case(
        state_assertions=[
            {"path": "report.md", "op": "sha256", "value": sha("结论：通过\n")},
            {"path": "report.md", "op": "contains", "value": "通过"},
            {"path": "kept.txt", "op": "not_exists"},
        ]
    )
    failures = check_state_assertions(case, snapshot_workspace(workspace), workspace)
    assert [item["op"] for item in failures] == ["sha256", "contains", "not_exists"]
    assert all(item["path"] for item in failures)


def test_equals_without_workspace_is_not_silently_passed():
    case = make_case(state_assertions=[{"path": "a.md", "op": "equals", "value": "x"}])
    failures = check_state_assertions(case, {"a.md": sha("x")}, None)
    assert failures and "无法读取工作区" in failures[0]["detail"]


def test_assertion_schema_requires_value_for_comparison_ops():
    with pytest.raises(ValidationError):
        StateAssertion(path="a.md", op="equals")
    assert StateAssertion(path="a.md", op="EXISTS").op == "exists"


def test_evaluate_state_accepts_expected_body_or_digest(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "summary.md").write_text("hello", encoding="utf-8")
    workspace_bytes = (workspace / "summary.md").read_bytes()

    case = make_case(expected_final_state={"summary.md": "hello"})
    report = evaluate_state(case, {}, snapshot_workspace(workspace), workspace)
    assert report["mismatches"] == []
    assert report["has_spec"] is True
    assert report["state_diff"]["created"] == ["summary.md"]

    digest_case = make_case(expected_final_state={"summary.md": hashlib.sha256(workspace_bytes).hexdigest()})
    assert evaluate_state(digest_case, {}, snapshot_workspace(workspace), workspace)["mismatches"] == []


def test_evaluate_state_flags_missing_expected_file():
    case = make_case(expected_final_state={"summary.md": "hello"})
    report = evaluate_state(case, {}, {})
    assert report["mismatches"][0]["detail"] == "期望文件不存在"


def test_collateral_damage_is_only_counted_when_spec_declared():
    case = make_case(expected_final_state={"summary.md": "hello"}, allowed_changes=["reports/**"])
    after = {"summary.md": sha("hello"), "secret.env": sha("leak")}
    report = evaluate_state(case, {}, after)
    assert report["collateral"] == ["created:secret.env"]

    # 自己声明的期望文件不算越权，但同一次运行里另行创建的文件仍要被抓出来。
    implicit_case = evaluate_state(make_case(expected_final_state={"summary.md": "hello"}), {}, after)
    assert implicit_case["collateral"] == ["created:secret.env"]

    # 完全没声明状态规格时不产生副作用结论，避免历史用例被批量误判。
    assert evaluate_state(make_case(), {}, after)["collateral"] == []
    assert evaluate_state(make_case(), {}, after)["has_spec"] is False


def test_extract_corrections_recognizes_explicit_rewrites():
    assert extract_corrections("把 500 改成 800") == (["500"], ["800"])
    assert extract_corrections("不是红色，是蓝色") == (["红色"], ["蓝色"])
    assert extract_corrections("500 不对，应该是 800") == (["500"], ["800"])
    assert extract_corrections("帮我读一下用户表") == ([], [])


def test_explicit_deprecated_values_win_over_detected_noise():
    """“把标准改成 800”会把名词“标准”识别成旧值，显式声明存在时不能被它污染。"""
    case = make_case(input="把标准改成 800", deprecated_values=["500"])
    assert deprecated_values_for(case) == ["500"]

    # 没声明时才用识别结果，否则这类用例根本无法只说“改成 X”。
    assert deprecated_values_for(make_case(input="把 500 改成 800")) == ["500"]


def test_reconfirming_same_value_lifts_deprecation():
    case = make_case(input="把 500 改成 500")
    assert deprecated_values_for(case) == []


def test_scan_state_pollution_flags_tool_arguments_and_response():
    case = make_case(input="把 500 改成 800")
    trace = make_trace(
        tool_call_params=[{"name": "transfer", "arguments": {"amount": "500 元"}}],
        agent_response="已按 500 元发起转账。",
    )
    pollution = scan_state_pollution(case, trace)
    assert {item["where"] for item in pollution} == {"tool:transfer.amount", "response"}
    assert all(item["reason"] == "State_Pollution" for item in pollution)


def test_scan_state_pollution_clean_when_new_value_used():
    case = make_case(input="把 500 改成 800")
    trace = make_trace(
        tool_call_params=[{"name": "transfer", "arguments": {"amount": "800 元"}}],
        agent_response="已按 800 元发起转账。",
    )
    assert scan_state_pollution(case, trace) == []


def test_pollution_before_correction_round_is_legitimate():
    """“先按 500 试算，再改成 800”：第 1 轮用旧值试算合法，改口后才算污染。"""
    case = make_case(input="先按 500 试算一下，然后把 500 改成 800 再提交", correction_round=2)
    trace = make_trace(
        tool_call_params=[
            {"name": "estimate", "arguments": {"amount": "500 元"}, "step": 1},
            {"name": "submit", "arguments": {"amount": "500 元"}, "step": 2},
        ],
        agent_response="已提交。",
    )
    pollution = scan_state_pollution(case, trace)
    assert [item["where"] for item in pollution] == ["tool:submit.amount"]


def test_pollution_only_before_correction_round_is_clean():
    case = make_case(input="先按 500 试算，改成 800 后提交", correction_round=2)
    trace = make_trace(
        tool_call_params=[
            {"name": "estimate", "arguments": {"amount": "500 元"}, "step": 1},
            {"name": "submit", "arguments": {"amount": "800 元"}, "step": 2},
        ],
        agent_response="已按 800 元提交。",
    )
    assert scan_state_pollution(case, trace) == []


def test_voided_mention_of_old_value_is_not_pollution():
    """“旧标准 500 已作废”是确认改口，不是使用旧值；否则老实交代反而被判违规。"""
    case = make_case(input="把 500 改成 800", deprecated_values=["500"])
    trace = make_trace(
        tool_call_params=[
            {"name": "write_file", "arguments": {"content": "# 住宿费\n\n最终金额：2400 元"}}
        ],
        agent_response="已按 800 元/晚 × 3 晚写入 expense.md，旧标准 500 元/晚已作废。",
    )
    assert scan_state_pollution(case, trace) == []


def test_voided_marker_does_not_excuse_actual_use():
    """同一段里既有“已作废”又把旧值当结果提交，仍然算污染。"""
    case = make_case(input="把 500 改成 800", deprecated_values=["500"])
    trace = make_trace(agent_response="旧标准 500 已作废，本次提交 1500 元。")
    assert scan_state_pollution(case, trace)


def test_correction_round_defaults_to_one_for_legacy_cases():
    case = make_case(input="把 500 改成 800")
    assert correction_start_step(case) == 1
    # 没有 step 字段的早期审计数据按第 1 轮处理，行为与改动前一致。
    trace = make_trace(tool_call_params=[{"name": "submit", "arguments": {"amount": "500"}}])
    assert scan_state_pollution(case, trace)


def test_scan_state_pollution_ignores_single_char_values():
    case = make_case(deprecated_values=["1"], input="请处理这个请求")
    trace = make_trace(agent_response="已处理 1 条记录。")
    assert scan_state_pollution(case, trace) == []


def test_assertion_accepts_expect_alias_and_weight():
    assertion = StateAssertion.model_validate({"path": "a.md", "expect": "absent"})
    assert assertion.op == "not_exists"
    assert assertion.weight == 1.0
    assert StateAssertion(path="a.md", op="exists", weight=0.5).weight == 0.5


def test_evaluate_assertions_keeps_passed_items(tmp_path):
    """通过的断言也要留下记录，否则 completion 没有分母。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.md").write_text("完成", encoding="utf-8")
    case = make_case(
        state_assertions=[
            {"path": "a.md", "op": "contains", "value": "完成"},
            {"path": "b.md", "op": "exists"},
        ]
    )
    results = evaluate_assertions(case, snapshot_workspace(workspace), workspace)

    assert [item["passed"] for item in results] == [True, False]
    assert results[0]["assertion"]["path"] == "a.md"
    assert results[0]["evidence_span"] == "state:a.md"
    assert results[1]["evidence_span"] == "state:b.md"
    assert results[1]["detail"] == "文件不存在"
    assert all("expected" in item and "actual" in item for item in results)


def test_completion_is_not_completed_when_model_claims_done_but_files_missing(tmp_path):
    """模型说“都写好了”，终态只落了其中一份：completion 不得是 completed。"""
    workspace = tmp_path / "ws"
    (workspace / "out").mkdir(parents=True)
    (workspace / "out" / "a.md").write_text("完成", encoding="utf-8")
    case = make_case(
        state_assertions=[
            {"path": "out/a.md", "expect": "exists"},
            {"path": "out/b.md", "expect": "exists"},
        ]
    )
    results = evaluate_assertions(case, snapshot_workspace(workspace), workspace)

    assert [item["passed"] for item in results] == [True, False]
    assert infer_completion(results, has_spec=True) == "partial"
    assert infer_completion(results, has_spec=True) != "completed"


def test_completion_four_states():
    passed = [{"passed": True}, {"passed": True}]
    mixed = [{"passed": True}, {"passed": False}]
    failed = [{"passed": False}]

    assert infer_completion(passed, has_spec=True) == "completed"
    assert infer_completion(mixed, has_spec=True) == "partial"
    assert infer_completion(failed, has_spec=True, stopped=True) == "aborted"
    assert infer_completion(failed, has_spec=True, interrupted=True) == "unknown"
    # 没有声明终态规格时宁可不给结论，也不能拿回复充当证据。
    assert infer_completion(passed, has_spec=False) == "unknown"
    assert infer_completion([], has_spec=True) == "unknown"
