from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["critical", "high", "medium", "low"]
DataSensitivity = Literal["S1", "S2", "S3"]
RunMode = Literal["enforce", "shadow"]
JudgeLayer = Literal["rule", "trajectory", "jcv_audit", "llm", "local_fallback", "harness"]
# 失败环节：用于把“答错”拆成可归属的流水线阶段。
FailureStage = Literal["understanding", "planning", "generation", "validation", "iteration"]
StateOp = Literal["exists", "not_exists", "equals", "contains", "sha256"]
# 断言期望值的同义写法：`absent` 就是 `not_exists`，词表里叫 expect，字段名仍是 op。
STATE_OP_ALIASES = {"absent": "not_exists", "not-exists": "not_exists", "notexists": "not_exists"}

# completion：由终态断言推导，禁止用 Agent 回复里的话当证据。
COMPLETION_STATES = ("completed", "partial", "aborted", "unknown")

# Runner 拿不到收尾答复时的占位文本；裁判与报告都按这个前缀识别“没测到答复”而不是“答错了”。
NO_FINAL_RESPONSE_PREFIX = "[NO_FINAL_RESPONSE]"
NO_FINAL_RESPONSE_TEXT = f"{NO_FINAL_RESPONSE_PREFIX} Agent 在预算内没有产生最终答复。"

# 属于外部因素、不应计入模型能力的错因。
# Output_Truncated：模型输出打满 max_tokens 导致一个字都没拿到，属于配置问题而不是答错。
OUTPUT_TRUNCATED_REASON = "Output_Truncated"
# 网关/云厂商的内容安全审核会把红队 prompt 直接拒在门外（403 safety review），
# 模型根本没被调用：不是模型行为，也不是 Harness 故障，重试与改 key 都无效。
PROVIDER_BLOCKED_REASON = "Provider_Blocked"
INFRA_FAIL_REASONS = {
    "Harness_Error",
    "Judge_Unavailable",
    OUTPUT_TRUNCATED_REASON,
    PROVIDER_BLOCKED_REASON,
}

# 终态断言 / 参数血缘 / 脏状态相关错因。它们都是模型侧问题，不算基础设施故障。
STATE_MISMATCH_REASON = "State_Mismatch"
COLLATERAL_DAMAGE_REASON = "Collateral_Damage"
FABRICATED_PARAMETER_REASON = "Fabricated_Parameter"
STATE_POLLUTION_REASON = "State_Pollution"

SEVERITY_ALIASES = {
    "critical": "critical",
    "严重": "critical",
    "高": "high",
    "high": "high",
    "中": "medium",
    "medium": "medium",
    "低": "low",
    "low": "low",
}

SENSITIVITY_ALIASES = {
    "s1": "S1",
    "s2": "S2",
    "s3": "S3",
    "低": "S1",
    "中": "S2",
    "高": "S3",
}

CATEGORY_ALIASES = {
    "permission_boundary": "身份与授权",
    "tool_misuse": "工具与行动安全",
    "prompt_injection": "数据与上下文安全",
    "hallucination": "事实可靠性",
    "Cross_Domain": "跨领域风险涌现",
    "cross_domain": "跨领域风险涌现",
}


def normalize_severity(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in SEVERITY_ALIASES:
        return SEVERITY_ALIASES[raw]
    raise ValueError(f"未知严重等级: {value!r}")


def normalize_sensitivity(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "S1"
    if raw.upper() in {"S1", "S2", "S3"}:
        return raw.upper()
    if raw in SENSITIVITY_ALIASES:
        return SENSITIVITY_ALIASES[raw]
    raise ValueError(f"未知数据敏感度: {value!r}")


def _dump(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def normalize_category(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "未分类"
    return CATEGORY_ALIASES.get(raw, raw)


class StateAssertion(BaseModel):
    """单条终态断言。

    `equals` / `contains` 需要读文件正文，因此只在运行时能读到工作区时生效；
    读不到按“无法验证”处理，不做静默通过。
    """

    path: str = Field(..., min_length=1, description="工作区相对路径")
    op: StateOp = Field(..., description="exists/not_exists/equals/contains/sha256")
    value: Optional[str] = Field(default=None, description="期望值；exists/not_exists 可省略")
    weight: float = Field(default=1.0, ge=0.0, description="该条断言在完成度里的权重")

    @model_validator(mode="before")
    @classmethod
    def _accept_expect_alias(cls, data: object) -> object:
        """允许用例写 `expect: exists|absent|sha256|contains`，内部仍存到 op。"""
        if isinstance(data, dict) and not data.get("op") and data.get("expect"):
            return {**data, "op": data["expect"]}
        return data

    @field_validator("op", mode="before")
    @classmethod
    def _normalize_op(cls, value: object) -> str:
        raw = str(value or "").strip().lower()
        return STATE_OP_ALIASES.get(raw, raw)

    @model_validator(mode="after")
    def _require_value(self) -> "StateAssertion":
        if self.op in {"equals", "contains", "sha256"} and (self.value is None or self.value == ""):
            raise ValueError(f"state_assertions.{self.op} 必须提供 value")
        return self


class TestCase(BaseModel):
    """测试用例定义。"""

    id: str = Field(..., min_length=1, description="用例唯一ID")
    category: str = Field(..., description="风险类别")
    input: str = Field(..., min_length=1, description="发给 Agent 的指令")
    allowed_tools: List[str] = Field(default_factory=list, description="允许调用的工具白名单")
    forbidden_actions: List[str] = Field(default_factory=list, description="禁止的动作")
    expected_behavior: str = Field(..., min_length=1, description="期望行为")
    severity: Severity = Field(..., description="严重等级")

    attack_logic: str = Field(default="", description="攻击底层逻辑")
    expected_variables: List[str] = Field(default_factory=list, description="预期提取的变量集合")

    data_sensitivity: DataSensitivity = Field(default="S1", description="S1 上云 / S2 脱敏上云 / S3 仅本地")
    requires_human_confirm: bool = Field(default=False, description="高危操作是否必须请求人工确认")
    fixtures: Dict[str, str] = Field(default_factory=dict, description="运行前注入沙箱的文件")

    source: str = Field(default="", description="用例来源，便于追踪生成批次")
    tags: List[str] = Field(default_factory=list, description="检索标签")

    # ---- iRTE 2026：终态断言 / 参数血缘 / 改口脏状态 ----
    initial_state: Dict[str, str] = Field(default_factory=dict, description="用例开始前写入沙箱的初始文件")
    expected_final_state: Dict[str, str] = Field(
        default_factory=dict, description="期望终态：路径 -> sha256 或期望正文（正文会先哈希）"
    )
    state_assertions: List[StateAssertion] = Field(default_factory=list, description="终态断言列表")
    allowed_changes: List[str] = Field(default_factory=list, description="允许变更的相对路径 glob，其余变更记 Collateral_Damage")
    deprecated_values: List[str] = Field(default_factory=list, description="用户改口后已废弃的旧值")
    correction_round: int = Field(
        default=1,
        ge=1,
        description="改口从第几轮起生效（1 起，与 tool_call_params.step 对齐）；1 表示全程以新值为准",
    )

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, value: object) -> str:
        return normalize_severity(value)

    @field_validator("data_sensitivity", mode="before")
    @classmethod
    def _normalize_sensitivity(cls, value: object) -> str:
        return normalize_sensitivity(value)

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: object) -> str:
        return normalize_category(value)

    @field_validator("initial_state", "expected_final_state", mode="before")
    @classmethod
    def _coerce_state_files(cls, value: object) -> Dict[str, str]:
        if not value:
            return {}
        if not isinstance(value, dict):
            raise ValueError("initial_state / expected_final_state 必须是 path -> value 的对象")
        return {str(path): content if isinstance(content, str) else _dump(content) for path, content in value.items()}

    @field_validator("allowed_tools", "forbidden_actions", "expected_variables", "tags", "allowed_changes", "deprecated_values", mode="before")
    @classmethod
    def _coerce_string_list(cls, value: object) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    @field_validator("fixtures", mode="before")
    @classmethod
    def _coerce_fixtures(cls, value: object) -> Dict[str, str]:
        if not value:
            return {}
        if not isinstance(value, dict):
            raise ValueError("fixtures 必须是 path -> content 的对象")
        return {str(path): str(content) for path, content in value.items()}


class AgentTrace(BaseModel):
    """Agent 执行轨迹。"""

    case_id: str
    agent_response: str = ""
    tools_called: List[str] = Field(default_factory=list)
    tool_call_sequence: List[str] = Field(default_factory=list)
    tool_call_params: List[dict] = Field(default_factory=list, description="每次调用的名称、参数、结果")
    tool_results: List[dict] = Field(default_factory=list, description="工具返回的可审计结果")
    tool_call_count: int = Field(default=0, description="工具调用总次数")
    retry_count: int = Field(default=0, description="同一工具与参数重复调用次数")
    should_escalate_to_human: bool = Field(default=False)
    did_escalate_to_human: bool = Field(default=False)

    is_cheated: bool = Field(default=False)
    is_token_exceeded: bool = Field(default=False)
    is_step_exceeded: bool = Field(default=False)
    finish_reason: str = Field(default="", description="最后一轮模型返回的 finish_reason；length 表示被 max_tokens 截断")
    output_truncated: bool = Field(
        default=False, description="输出打满 max_tokens，且全程没有正文与工具调用——该样本没测到模型真实能力"
    )
    policy_blocked: bool = Field(default=False, description="Harness 是否因数据路由策略直接阻断")
    policy_violations: List[str] = Field(default_factory=list)
    sandbox_limitations: List[str] = Field(
        default_factory=list, description="沙箱能力缺口：良性但未实现的命令/参数，不计入策略违规"
    )
    privacy_transform: str = Field(default="", description="S2 脱敏等隐私变换记录")
    unsupported_tools: List[str] = Field(default_factory=list, description="数据集中声明但 Harness 未实现的工具")

    round_history: List[str] = Field(default_factory=list)
    execution_workspace: str = Field(default="default_workspace")
    extracted_variables: List[str] = Field(default_factory=list)

    # 工作区快照：相对路径 -> sha256，用于终态断言与副作用（Collateral_Damage）判定。
    state_before: Dict[str, str] = Field(default_factory=dict)
    state_after: Dict[str, str] = Field(default_factory=dict)
    state_diff: Dict[str, List[str]] = Field(default_factory=dict)
    # 逐条终态断言的判定明细：{assertion, passed, expected, actual, evidence_span}。
    assertion_results: List[dict] = Field(default_factory=list)
    # completed / partial / aborted / unknown，由断言结果推导，不看回复措辞。
    completion: str = Field(default="unknown", description="任务完成度，未知时不填 completed")
    attempt: int = Field(default=0, description="repeat 场景下的第几次独立尝试（0 起）")

    model: str = ""
    run_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    token_cost: int = 0
    is_success: bool = True
    error_message: Optional[str] = None


class AttemptResult(BaseModel):
    """单次独立尝试的结论，pass^k 与稳定性指标都由它汇总。"""

    attempt: int = 1
    passed: bool = False
    strict_passed: bool = False
    risk_level: Severity = "high"
    judge_layer: JudgeLayer = "rule"
    fail_reason: str = "None"
    data_valid: bool = True
    state_diff: Dict[str, List[str]] = Field(default_factory=dict)
    latency_ms: float = 0.0


class EvalResult(BaseModel):
    """评测结果。"""

    case_id: str
    category: str

    passed: bool = Field(..., description="Standard：最终未造成破坏")
    strict_passed: bool = Field(default=True, description="Strict：全过程无违规尝试")
    risk_level: Severity
    judge_layer: JudgeLayer
    judge_reason: str = Field(default="", description="简要理由")
    fail_reason: str = Field(default="None")

    phenomenon: str = Field(default="")
    evidence: str = Field(default="")
    impact: str = Field(default="")
    suggestion: str = Field(default="")

    # 失败归因：规则层能判就先判，判不了由 LLM 裁判按契约给出。
    failure_stage: Optional[FailureStage] = Field(default=None, description="understanding/planning/generation/validation/iteration")
    evidence_span: str = Field(default="", description="证据定位：round:<i> | tool:<name> | state:<path>")

    state_diff: Dict[str, List[str]] = Field(default_factory=dict, description="created/modified/deleted")
    attempts: List[AttemptResult] = Field(default_factory=list, description="repeat>1 时每次独立尝试的结论")
    provenance_violations: List[dict] = Field(default_factory=list, description="参数血缘违规：tool/param/value/reason")

    jcv_score: Optional[float] = Field(default=None, description="Jaccard 相似度；未执行 JCV 时为 None")
    data_valid: bool = Field(default=True, description="该样本是否真正测到了被测模型；Runner/网关异常时为 False")
    no_final_response: bool = Field(default=False, description="Harness 未能从模型拿到收尾答复")
    output_truncated: bool = Field(default=False, description="输出被 max_tokens 截断，样本不可信")
    sandbox_limited: bool = Field(default=False, description="本轮撞上沙箱能力缺口（不计违规，但需在报告中单列）")
    hitl_mode: str = Field(
        default="",
        description="HITL 用例的处置方式：escalated/refused/bypassed/silent/unassessed/not_required",
    )
    judge_model: str = ""
    judge_mode: RunMode = "enforce"
    judge_attempts: int = 0
    judge_disagreement: bool = False
    is_fallback: bool = False
    model: str = ""
    run_id: str = ""
    token_cost: int = 0
