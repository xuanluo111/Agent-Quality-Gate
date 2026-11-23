"""T31 AgentAdapter 协议 + 轨迹重放参考实现（06 §四 W5 / 04 X-D8 裁定）。

把「只能评 OpenAI 兼容端点」变成「能评任意外部 agent」：外部框架
（LangGraph / 自研 orchestrator）把自己的工具调用轨迹导出为 JSONL，本模块在
**同一套 Sandbox / fixture / 快照 / 断言链**里重放，终态证据（state_diff、
assertion_results、completion、policy_violations）全部由 runner.py 提取的共用
推导函数产出——判定层（judge/gate/failure_class）零改动，不区分轨迹来源。

协议接缝只有一个方法：run_case(case, attempt) -> AgentTrace。`AgentRunner`
结构上天然满足（不显式继承，避免把 OpenAI 客户端耦合带进协议）。

已知限制（必须随报告披露，勿当模型真实表现引用）：
- 非实时交互：重放不能复现外部 agent 与环境的双向决策，只重放它导出的调用序列；
- token_cost / input_tokens / output_tokens / seed / finish_reason / backoff_ms
  外部轨迹给不了，如实置 0/None/空——统计均值会被拉低，不代表被测方更省或更快；
- latency_ms 是**重放耗时**，不是外部 agent 的真实耗时；
- S3 用例一律 Harness 阻断：外部 trace 无法证明执行发生在本地端点，fail-closed。
"""

import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError, model_validator

from src.engine.failure_class import is_precondition_error
from src.engine.fixture_ready import check_fixtures, render_issues
from src.engine.journal import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_UNKNOWN,
    OperationJournal,
)
from src.engine.privacy import mask_sensitive_data
from src.engine.runner import (
    _apply_initial_state,
    _declared_variables,
    _mask_payload,
    _parse_arguments,
    build_audit_item,
    finalize_trace,
    journal_path_for,
    policy_block_trace,
    precondition_failure_trace,
    workspace_for,
)
from src.engine.state import snapshot_workspace
from src.engine.tools import Sandbox, canonical_tool_name, resolve_case_tools
from src.engine.variables import extract_variables_from_results
from src.models.schema import NO_FINAL_RESPONSE_TEXT, AgentTrace, TestCase


@runtime_checkable
class AgentAdapter(Protocol):
    """评测执行适配器协议：consume 侧只依赖 run_case；journal_path 可选
    （run_eval/run_single_case 已用 getattr 兜底，缺失只是少一路误报归属信号）。"""

    def run_case(self, case: TestCase, attempt: int = 0) -> AgentTrace:
        ...


class ExternalToolCall(BaseModel):
    """外部轨迹里的一次工具调用。arguments 允许 dict 或 JSON 字符串——
    各家框架两种导出姿势都见过，归一化在模型校验里一次做完。
    id 可选：给了就保留（与外部系统自己的日志对账），缺省落 replay_<n>。"""

    name: str = Field(..., min_length=1)
    id: str = Field(default="", description="外部系统自己的 call id，可选")
    arguments: Dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_arguments(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        raw = data.get("arguments")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"arguments 不是合法 JSON 字符串: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("arguments 解析后必须是对象")
            return {**data, "arguments": parsed}
        if raw is None:
            return {**data, "arguments": {}}
        return data


class ExternalTraceRecord(BaseModel):
    """外部 agent 导出的一次完整尝试：工具调用序列 + 最终回复。"""

    case_id: str = Field(..., min_length=1)
    attempt: int = Field(default=0, ge=0)
    tool_calls: List[ExternalToolCall] = Field(default_factory=list)
    final_response: str = ""


def load_external_traces(
    path: str | Path,
    known_case_ids: Optional[Mapping[str, object]] = None,
) -> Dict[tuple, ExternalTraceRecord]:
    """加载外部轨迹 JSONL，按 (case_id, attempt) 归组。

    与数据集加载同口径的 fail-closed：畸形行、字段校验失败、未知 case_id、
    重复 (case_id, attempt) 一律报错中止，绝不静默丢行——静默丢弃会让外部
    agent「少跑了几条还没人知道」。
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"外部轨迹文件不存在: {file_path}")
    records: Dict[tuple, ExternalTraceRecord] = {}
    lines = file_path.read_text(encoding="utf-8").splitlines()
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{file_path}:{line_no} 不是合法 JSON: {exc}") from exc
        try:
            record = ExternalTraceRecord.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"{file_path}:{line_no} 字段校验失败: {exc}") from exc
        if known_case_ids is not None and record.case_id not in known_case_ids:
            raise ValueError(
                f"{file_path}:{line_no} 引用了数据集里不存在的 case_id: {record.case_id}"
            )
        key = (record.case_id, record.attempt)
        if key in records:
            raise ValueError(
                f"{file_path}:{line_no} 重复的 (case_id, attempt)={key}：同一次尝试"
                "出现两条轨迹，重放哪条都不该由加载器替你选"
            )
        records[key] = record
    if not records:
        raise ValueError(f"{file_path} 没有任何可用轨迹记录")
    return records


def _mask_if_s2(route: str) -> Callable[[object], object]:
    return _mask_payload if route == "s2_masked_cloud" else (lambda value: value)


class TraceReplayAdapter:
    """轨迹重放适配器：外部 agent 的调用序列 → 沙箱重放 → 统一 AgentTrace。

    与 AgentRunner 的语义逐段对齐（fixture 预检、Sandbox 兜底、未暴露工具拦截、
    formalize 声明路径、WAL、首个调用失败的前置判定、S2 脱敏、终态推导），
    差异只在证据来源：模型不再参与决策。
    """

    def __init__(
        self,
        traces: str | Path | Mapping[tuple, ExternalTraceRecord],
        cases: Mapping[str, TestCase],
        *,
        workspace_root: str = "workspace",
        run_id: str = "run",
        journal_path: Optional[str] = None,
        name: Optional[str] = None,
    ):
        if isinstance(traces, (str, Path)):
            loaded = load_external_traces(traces, cases)
            source: Optional[Path] = Path(traces)
        else:
            loaded = dict(traces)
            source = None
        self.records = loaded
        self.cases = dict(cases)
        self.workspace_root = Path(workspace_root)
        self.run_id = run_id
        self.journal_path = journal_path_for(self.workspace_root, run_id, journal_path)
        # model 字段的落法：trace:<文件名slug>，报告里一眼能看出这条结论来自重放
        # 而不是端点模型；显式 name 供测试与多轨迹文件场景覆盖。
        stem = name or (source.stem if source else "external")
        self.name = stem
        self.model = f"trace:{stem}"

    def _normalized_calls(self, record: ExternalTraceRecord) -> List[dict]:
        calls = []
        for index, call in enumerate(record.tool_calls):
            call_id = call.id or f"replay_{index + 1}"
            calls.append({"id": call_id, "name": call.name, "arguments": call.arguments})
        return calls

    def run_case(self, case: TestCase, attempt: int = 0) -> AgentTrace:
        record = self.records.get((case.id, attempt))
        if record is None:
            available = sorted(
                cid for (cid, _) in self.records if cid == case.id
            )
            raise KeyError(
                f"外部轨迹缺少 (case_id={case.id}, attempt={attempt})"
                + (f"；该用例已有 attempt {available}" if available else "；该用例没有任何轨迹")
            )
        started = time.time()
        unsupported_note: List[str] = []
        # 阻断检查必须在 fixture 预检之前：S3 的数据一行都不该进沙箱与审计产物，
        # 顺序反过来会让阻断 trace 背上 unsupported_tools。
        if case.data_sensitivity == "S3":
            # 外部轨迹无法证明这些调用发生在本地端点——即使配了 local 路由也
            # 一律阻断（fail-closed，同 runner 的 S3 姿势）。
            workspace = workspace_for(self.workspace_root, self.run_id, case.id, attempt)
            return policy_block_trace(
                case,
                workspace=workspace,
                message=(
                    "[HARNESS_POLICY] 检测到 S3 数据：外部轨迹无法证明执行发生在本地端点，"
                    "已在 Harness 层阻断。"
                ),
                model=self.model,
                run_id=self.run_id,
                privacy_transform="s3_cloud_blocked",
                history_note="[harness] S3 外部轨迹未重放",
                latency_ms=(time.time() - started) * 1000,
            )

        exposed_tools, unsupported_tools = resolve_case_tools(
            case.allowed_tools, case.requires_human_confirm
        )
        unsupported_note = unsupported_tools
        workspace = workspace_for(self.workspace_root, self.run_id, case.id, attempt)

        fixture_issues = check_fixtures(case.fixtures)
        if fixture_issues:
            return precondition_failure_trace(
                case,
                workspace=workspace,
                error_message=f"fixture 预检未通过: {render_issues(fixture_issues)}",
                unsupported_tools=unsupported_note,
                model=self.model,
                run_id=self.run_id,
                attempt=attempt,
                latency_ms=(time.time() - started) * 1000,
            )

        try:
            sandbox = Sandbox(workspace, case.fixtures, egress_allowlist=case.egress_allowlist)
        except Exception as exc:
            return precondition_failure_trace(
                case,
                workspace=workspace,
                error_message=f"沙箱初始化失败: {exc}",
                unsupported_tools=unsupported_note,
                model=self.model,
                run_id=self.run_id,
                attempt=attempt,
                latency_ms=(time.time() - started) * 1000,
            )

        route = "s2_masked_cloud" if case.data_sensitivity == "S2" else "cloud"
        mask = _mask_if_s2(route)
        privacy_notes: List[str] = []
        if route == "s2_masked_cloud":
            # 与 runner 的输入侧同姿势：题面无 PII 也要留下「检查过」的记录。
            _, applied = mask_sensitive_data(case.input)
            privacy_notes.extend(applied or ["s2_no_pii_detected"])

        journal = OperationJournal(self.journal_path, case.id)
        state_before: Dict[str, str] = {}
        state_after: Dict[str, str] = {}
        try:
            _apply_initial_state(workspace, case.initial_state)
            state_before = snapshot_workspace(workspace)
        except OSError:
            state_before = {}

        tool_calls_audit: List[dict] = []
        tool_results: List[dict] = []
        tools_called: List[str] = []
        tool_call_sequence: List[str] = []
        round_history: List[str] = []
        policy_violations: List[str] = []
        declared_variables: List[dict] = []
        formalize_step: Optional[int] = None
        first_action_step: Optional[int] = None
        did_escalate = False
        precondition_failed = False
        executed_calls = 0

        try:
            for step, call in enumerate(self._normalized_calls(record), 1):
                call_name = canonical_tool_name(call["name"])
                arguments, parse_error = _parse_arguments(json.dumps(call["arguments"], ensure_ascii=False))
                tools_called.append(call_name)
                tool_call_sequence.append(call_name)

                if parse_error:
                    result = {"ok": False, "tool": call_name, "error": parse_error, "blocked": False}
                elif call_name not in exposed_tools:
                    message_text = f"模型调用了未暴露工具: {call_name}"
                    policy_violations.append(message_text)
                    result = {"ok": False, "tool": call_name, "error": message_text, "blocked": True}
                elif call_name == "formalize":
                    declared_variables.extend(_declared_variables(arguments))
                    if formalize_step is None:
                        formalize_step = step
                    result = {"ok": True, "tool": call_name, "result": "已登记变量抽象声明", "blocked": False}
                else:
                    attempt_id = journal.begin(
                        call_name,
                        arguments,
                        round=step,
                        target=str(arguments.get("path") or "") if call_name == "write_file" else "",
                    )
                    try:
                        result = sandbox.invoke(call_name, arguments)
                    except Exception:
                        journal.finish(attempt_id, STATUS_UNKNOWN)
                        raise
                    journal.finish(
                        attempt_id,
                        STATUS_DONE if result.get("ok") else STATUS_FAILED,
                        result.get("result") if result.get("ok") else result.get("error"),
                    )
                    executed_calls += 1
                    # 与 runner 同口径：首个执行的调用就撞上目标不存在/无权限，
                    # 用例前置条件不成立，后续失败不能算到外部 agent 头上。
                    if executed_calls == 1 and not result.get("ok"):
                        precondition_failed = is_precondition_error(result.get("error"))

                if call_name == "request_human_confirmation" and result.get("ok"):
                    did_escalate = True
                if call_name in ("write_file", "execute_command") and first_action_step is None:
                    first_action_step = step
                if result.get("blocked") and result.get("error"):
                    policy_violations.append(str(result["error"]))

                audit_item, tool_result_entry, history_line = build_audit_item(
                    step, call["id"], call_name, arguments, result, mask=mask
                )
                tool_calls_audit.append(audit_item)
                tool_results.append(tool_result_entry)
                round_history.append(history_line)

            final_response = str(record.final_response or "")
            if not final_response:
                final_response = NO_FINAL_RESPONSE_TEXT
            if route == "s2_masked_cloud" and record.final_response:
                masked, applied = mask_sensitive_data(record.final_response)
                privacy_notes.extend(applied)
                final_response = masked
            round_history.append(f"[replay] Agent: {final_response[:1200]}")

            return finalize_trace(
                case,
                workspace=workspace,
                sandbox=sandbox,
                tool_calls_audit=tool_calls_audit,
                tool_results=tool_results,
                tools_called=tools_called,
                tool_call_sequence=tool_call_sequence,
                round_history=round_history,
                policy_violations=policy_violations,
                privacy_notes=privacy_notes,
                unsupported_tools=unsupported_note,
                state_before=state_before,
                formalize_step=formalize_step,
                first_action_step=first_action_step,
                declared_variables=declared_variables,
                did_escalate=did_escalate,
                # canary 由本 harness 在系统提示词里注入，外部 agent 从未见过它：
                # 重放场景下没有「模型泄了自家机密」这回事，恒置 False。
                is_cheated=False,
                # 轨迹没有轮次/预算/墙钟概念：调用跑完即终，预算旗标恒 False。
                is_token_exceeded=False,
                is_step_exceeded=False,
                is_time_exceeded=False,
                precondition_failed=precondition_failed,
                final_response=final_response,
                finish_reason="",
                saw_content=bool(record.final_response.strip()),
                cancel_requested_at=None,
                stopped_at=None,
                attempt=attempt,
                seed=None,
                model=self.model,
                run_id=self.run_id,
                input_tokens=0,
                output_tokens=0,
                token_cost=0,
                backoff_ms=0.0,
                latency_ms=(time.time() - started) * 1000,
                mask_response=mask,
            )
        except Exception as exc:
            # 异常也要拍终态（与 runner 同姿势）：已发生的副作用不能看不见。
            journal.mark_open_unknown()
            state_after = snapshot_workspace(workspace)
            return AgentTrace(
                case_id=case.id,
                agent_response=mask(final_response) if record.final_response else "",
                tools_called=tools_called,
                tool_call_sequence=tool_call_sequence,
                tool_call_params=tool_calls_audit,
                tool_results=tool_results,
                tool_call_count=len(tool_calls_audit),
                should_escalate_to_human=case.requires_human_confirm,
                did_escalate_to_human=did_escalate,
                precondition_failed=precondition_failed,
                policy_violations=list(dict.fromkeys(policy_violations)),
                unsupported_tools=unsupported_note,
                round_history=round_history,
                execution_workspace=str(workspace),
                extracted_variables=extract_variables_from_results(tool_results),
                state_before=state_before,
                state_after=state_after,
                state_diff={},
                attempt=attempt,
                model=self.model,
                run_id=self.run_id,
                latency_ms=(time.time() - started) * 1000,
                is_success=False,
                error_message=str(exc),
            )

