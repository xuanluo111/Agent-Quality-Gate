"""工具调用的 append-only 操作日志（WAL）。

一次工具调用在执行**之前**就落盘一条 `intent`，返回后再落一条 `done`/`failed`。
这样进程被杀、机器断电之后，仍然能从 jsonl 里看出「它本来要做什么」，
而不是只剩一份「改了文件、但不知道谁改的」的终态差异。

字段与词表：
- operation：一次用户意图级别的动作，系统内唯一，跨重试不变；
  这里用 `case_id + tool + params_hash` 派生，同一个工具同参数的重复调用属于同一 operation。
- attempt：operation 的一次执行尝试，重试产生新的 attempt（`<operation>#a2`）。
- status：`intent` / `done` / `failed` / `unknown`，只有后三者是终态。

写盘策略：每条记录一行、`O_APPEND` 追加、写后 fsync，保证崩溃时最多丢最后一行。
本模块只用标准库，不依赖被测模型的任何输出。
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

STATUS_INTENT = "intent"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"
TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_UNKNOWN})

JOURNAL_FILENAME = "operations.jsonl"

# 多线程跑用例时会并发追加同一个文件，行内写入需要串行化。
_WRITE_LOCK = threading.Lock()


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _digest(value: Any) -> str:
    if isinstance(value, str):
        blob = value
    else:
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def params_digest(params: Any) -> str:
    return _digest(params if params is not None else {})


def result_digest(result: Any) -> str:
    return _digest(result if result is not None else "")


def append_record(path: str | Path, record: dict) -> None:
    """追加一行。调用方保证 record 里不含明文敏感数据（只放摘要与路径）。"""
    target = Path(path)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _WRITE_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def load_records(path: str | Path) -> List[dict]:
    """按写入顺序读出所有行；坏行（被截断的半行）跳过，不影响前面的记录。"""
    target = Path(path)
    if not target.is_file():
        return []
    records: List[dict] = []
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _attempt_key(record: dict) -> str:
    return str(record.get("attempt_id") or record.get("operation_id") or "")


def load_operations(path: str | Path) -> List[dict]:
    """把同一 attempt 的多行（intent + 终态）合并成一条视图，保持首次出现顺序。"""
    merged: Dict[str, dict] = {}
    order: List[str] = []
    for record in load_records(path):
        key = _attempt_key(record)
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(record)
            order.append(key)
        else:
            merged[key].update(record)
    return [merged[key] for key in order]


def pending_attempts(path: str | Path) -> List[dict]:
    """有 intent 但没有终态记录的 attempt：进程在工具返回前就没了。"""
    return [item for item in load_operations(path) if item.get("status") == STATUS_INTENT]


def recover(path: str | Path, at: Optional[str] = None) -> List[dict]:
    """把遗留的 `intent` 标成 `unknown` 并追加 orphan 标记。

    只能在**开跑之前**调用：一旦有并发用例在写，它自己的 in-flight intent
    会被误标。调用方负责保证时机（run_eval 在启动线程池之前调一次）。
    """
    repaired: List[dict] = []
    for item in pending_attempts(path):
        record = {key: item.get(key) for key in _RECORD_FIELDS}
        record.update(
            {
                "status": STATUS_UNKNOWN,
                "ended_at": at or now_stamp(),
                "orphan": True,
                "result_digest": item.get("result_digest") or "",
            }
        )
        append_record(path, record)
        repaired.append(record)
    return repaired


_RECORD_FIELDS = (
    "operation_id",
    "attempt_id",
    "case_id",
    "round",
    "tool",
    "params_hash",
    "started_at",
    "ended_at",
    "status",
    "result_digest",
    "target",
)


class OperationJournal:
    """单条用例视角的日志写入器。

    每条 intent 都带 `target`（写文件的相对路径），终态差异里的“无主变更”
    靠它来对账：`state_diff` 里出现了 journal 没有 done 记录的路径，即为 orphan。
    """

    def __init__(self, path: str | Path, case_id: str):
        self.path = Path(path)
        self.case_id = case_id
        self._open: Dict[str, dict] = {}
        self._attempts: Dict[str, int] = {}
        self._seeded = False

    def _seed(self) -> None:
        """跨进程续跑时，attempt 序号要接着文件里已有的往下排。"""
        if self._seeded:
            return
        self._seeded = True
        try:
            for item in load_operations(self.path):
                operation_id = str(item.get("operation_id") or "")
                if not operation_id or str(item.get("case_id") or "") != self.case_id:
                    continue
                attempt_id = str(item.get("attempt_id") or "")
                suffix = attempt_id.rsplit("#a", 1)[-1]
                try:
                    number = int(suffix)
                except ValueError:
                    number = 1
                if number > self._attempts.get(operation_id, 0):
                    self._attempts[operation_id] = number
        except OSError:
            pass

    def begin(self, tool: str, params: Any = None, round: int = 1, target: str = "") -> str:
        """执行前落一条 intent，返回 attempt_id。"""
        self._seed()
        digest = params_digest(params)
        operation_id = f"{self.case_id}:{tool}:{digest[:12]}"
        attempt = self._attempts.get(operation_id, 0) + 1
        self._attempts[operation_id] = attempt
        attempt_id = f"{operation_id}#a{attempt}"
        record = {
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "case_id": self.case_id,
            "round": int(round or 1),
            "tool": str(tool or ""),
            "params_hash": digest,
            "started_at": now_stamp(),
            "ended_at": None,
            "status": STATUS_INTENT,
            "result_digest": "",
            "target": str(target or ""),
        }
        try:
            append_record(self.path, record)
        except OSError:
            # 日志写不下去不能连累评测：本条退化成"没记录"，由 orphan 检测兜住。
            return ""
        self._open[attempt_id] = record
        return attempt_id

    def finish(self, attempt_id: str, status: str, result: Any = None) -> None:
        record = self._open.pop(attempt_id, None)
        if record is None:
            return
        terminal = status if status in TERMINAL_STATUSES else STATUS_UNKNOWN
        entry = dict(record)
        entry.update(
            {
                "status": terminal,
                "ended_at": now_stamp(),
                "result_digest": result_digest(result),
                "orphan": False,
            }
        )
        try:
            append_record(self.path, entry)
        except OSError:
            pass

    def mark_open_unknown(self) -> None:
        """异常收尾：还没落终态的 attempt 一律记 unknown。"""
        for attempt_id in list(self._open):
            self.finish(attempt_id, STATUS_UNKNOWN)


def done_targets(records: Iterable[dict]) -> List[str]:
    """有 done 记录、且落过 target 的路径：这些变更是有主的。"""
    return sorted(
        {
            str(item.get("target"))
            for item in records
            if item.get("status") == STATUS_DONE and str(item.get("target") or "")
        }
    )


def repeated_attempts_without_terminal(records: Iterable[dict]) -> List[str]:
    """同一 operation 多次 attempt 但仍有 attempt 没落终态：更像 Harness 自己重试/中断。"""
    by_operation: Dict[str, List[dict]] = {}
    for item in records:
        by_operation.setdefault(str(item.get("operation_id") or ""), []).append(item)
    flagged: List[str] = []
    for operation_id, items in by_operation.items():
        if not operation_id or len(items) < 2:
            continue
        terminal = [item for item in items if item.get("status") in TERMINAL_STATUSES]
        if len(terminal) < len(items):
            flagged.append(operation_id)
    return sorted(flagged)
