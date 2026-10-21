from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Severity = Literal["critical", "high", "medium", "low"]
DataSensitivity = Literal["S1", "S2", "S3"]
RunMode = Literal["enforce", "shadow"]
JudgeLayer = Literal["rule", "trajectory", "jcv_audit", "llm", "local_fallback", "harness"]

# Runner 拿不到收尾答复时的占位文本；裁判与报告都按这个前缀识别“没测到答复”而不是“答错了”。
NO_FINAL_RESPONSE_PREFIX = "[NO_FINAL_RESPONSE]"
NO_FINAL_RESPONSE_TEXT = f"{NO_FINAL_RESPONSE_PREFIX} Agent 在预算内没有产生最终答复。"

# 属于外部因素、不应计入模型能力的错因。
# Output_Truncated：模型输出打满 max_tokens 导致一个字都没拿到，属于配置问题而不是答错。
OUTPUT_TRUNCATED_REASON = "Output_Truncated"
INFRA_FAIL_REASONS = {"Harness_Error", "Judge_Unavailable", OUTPUT_TRUNCATED_REASON}

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


def normalize_category(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "未分类"
    return CATEGORY_ALIASES.get(raw, raw)


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

    @field_validator("allowed_tools", "forbidden_actions", "expected_variables", "tags", mode="before")
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

    model: str = ""
    run_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    token_cost: int = 0
    is_success: bool = True
    error_message: Optional[str] = None


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
