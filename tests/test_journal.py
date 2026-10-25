import json

from src.engine.journal import (
    STATUS_DONE,
    STATUS_INTENT,
    STATUS_UNKNOWN,
    OperationJournal,
    load_operations,
    load_records,
    pending_attempts,
    recover,
)
from src.engine.runner import AgentRunner
from tests.conftest import FakeClient, make_case, text_response, tool_response


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_journal_writes_intent_before_execution(tmp_path):
    """工具执行前必须先落 intent，返回后再落 done。"""
    journal_path = tmp_path / "journal" / "operations.jsonl"
    runner = AgentRunner(
        api_base="https://example.invalid/v1",
        api_key="test-key",
        model="fake-model",
        workspace_root=str(tmp_path / "workspace"),
        run_id="run-1",
        client=FakeClient(
            [
                tool_response("write_file", json.dumps({"path": "out.txt", "content": "ok"})),
                text_response("已写入。"),
            ]
        ),
        journal_path=str(journal_path),
    )
    trace = runner.run_case(make_case(id="wal-1", allowed_tools=["write_file"]))

    assert trace.is_success
    lines = read_lines(journal_path)
    assert [line["status"] for line in lines] == [STATUS_INTENT, STATUS_DONE]
    first, second = lines
    assert first["tool"] == "write_file"
    assert first["case_id"] == "wal-1"
    assert first["round"] == 1
    assert first["target"] == "out.txt"
    assert first["ended_at"] is None
    # intent 与终态是同一个 operation / attempt，终态才能覆盖它。
    assert first["operation_id"] == second["operation_id"]
    assert first["attempt_id"] == second["attempt_id"]
    assert second["ended_at"]
    assert second["result_digest"]


def test_journal_failed_call_records_failed_status(tmp_path):
    journal_path = tmp_path / "operations.jsonl"
    runner = AgentRunner(
        api_base="https://example.invalid/v1",
        api_key="test-key",
        model="fake-model",
        workspace_root=str(tmp_path / "workspace"),
        run_id="run-1",
        client=FakeClient(
            [
                tool_response("file_read", json.dumps({"path": "missing.txt"})),
                text_response("文件不存在。"),
            ]
        ),
        journal_path=str(journal_path),
    )
    runner.run_case(make_case(id="wal-2", fixtures={}))

    lines = read_lines(journal_path)
    assert lines[0]["status"] == STATUS_INTENT
    assert lines[-1]["status"] == "failed"
    assert load_operations(journal_path)[0]["status"] == "failed"


def test_journal_recovery_marks_orphan_intent_only_for_that_case(tmp_path):
    """模拟写 intent 后进程被杀：重跑能把该 operation 标 unknown，别的 case 不受影响。"""
    journal_path = tmp_path / "operations.jsonl"
    crashed = OperationJournal(journal_path, "crashed-case")
    crashed.begin("write_file", {"path": "half.txt", "content": "x"}, round=2, target="half.txt")

    finished = OperationJournal(journal_path, "other-case")
    attempt = finished.begin("file_read", {"path": "a.txt"}, round=1)
    finished.finish(attempt, STATUS_DONE, "ok")

    assert [item["case_id"] for item in pending_attempts(journal_path)] == ["crashed-case"]

    repaired = recover(journal_path, at="2026-01-01 00:00:00")

    assert len(repaired) == 1
    assert repaired[0]["case_id"] == "crashed-case"
    assert repaired[0]["status"] == STATUS_UNKNOWN
    assert repaired[0]["orphan"] is True
    states = {item["case_id"]: item["status"] for item in load_operations(journal_path)}
    assert states == {"crashed-case": STATUS_UNKNOWN, "other-case": STATUS_DONE}
    # 恢复只追加，不改写历史：这个文件永远是 append-only。
    assert [line["status"] for line in read_lines(journal_path)] == [
        STATUS_INTENT,
        STATUS_INTENT,
        STATUS_DONE,
        STATUS_UNKNOWN,
    ]
    assert load_records(journal_path)[0]["started_at"]
    # 再跑一次不会重复追加：已经终态化的 attempt 不再算 pending。
    assert pending_attempts(journal_path) == []


def test_journal_attempt_id_increments_on_retry(tmp_path):
    journal_path = tmp_path / "operations.jsonl"
    journal = OperationJournal(journal_path, "retry-case")
    first = journal.begin("file_read", {"path": "a.txt"}, round=1)
    journal.finish(first, STATUS_DONE, "ok")
    second = journal.begin("file_read", {"path": "a.txt"}, round=2)
    journal.finish(second, "failed", "boom")

    lines = read_lines(journal_path)
    assert lines[0]["operation_id"] == lines[2]["operation_id"]
    assert lines[0]["attempt_id"].endswith("#a1")
    assert lines[2]["attempt_id"].endswith("#a2")


def test_journal_bad_line_does_not_break_reading(tmp_path):
    journal_path = tmp_path / "operations.jsonl"
    journal = OperationJournal(journal_path, "torn-case")
    journal.finish(journal.begin("file_read", {"path": "a.txt"}), STATUS_DONE, "ok")
    with open(journal_path, "a", encoding="utf-8") as handle:
        handle.write('{"attempt_id": "torn", "status": "in')
    assert len(load_operations(journal_path)) == 1
