from pydantic import BaseModel, Field
from typing import List, Optional


class TestCase(BaseModel):
    """测试用例定义"""
    id: str = Field(..., description="用例唯一ID")
    category: str = Field(..., description="风险类别：身份/数据/工具/环境/幻觉/记忆串户")
    input: str = Field(..., description="发给Agent的指令")
    allowed_tools: List[str] = Field(default=[], description="允许调用的工具白名单")
    forbidden_actions: List[str] = Field(default=[], description="禁止的动作")
    expected_behavior: str = Field(..., description="期望行为")
    severity: str = Field(..., description="严重等级：critical/high/medium/low")


class AgentTrace(BaseModel):
    """Agent 执行轨迹（一次评测的原始数据）"""
    case_id: str
    agent_response: str
    tools_called: List[str] = []

    # ================= 轨迹审计字段（新增） =================
    tool_call_sequence: List[str] = Field(default=[], description="工具调用顺序")
    tool_call_params: List[dict] = Field(default=[], description="每次调用的参数")
    tool_call_count: int = Field(default=0, description="工具调用总次数")
    retry_count: int = Field(default=0, description="失败重试次数")
    should_escalate_to_human: bool = Field(default=False, description="是否应转人工")
    did_escalate_to_human: bool = Field(default=False, description="是否实际转人工")

    latency_ms: float = 0.0
    token_cost: int = 0
    is_success: bool = True
    error_message: Optional[str] = None


class EvalResult(BaseModel):
    """评测结果（裁判的输出）"""
    case_id: str
    category: str
    passed: bool
    risk_level: str
    judge_layer: str = Field(..., description="哪一层裁判做出的决定：rule/trajectory/llm")
    judge_reason: str
    token_cost: int = 0

    # ================= 结构化诊断字段（新增） =================
    phenomenon: str = Field(default="", description="现象：Agent 做了什么")
    evidence: str = Field(default="", description="依据：具体回复或工具调用")
    impact: str = Field(default="", description="影响：对业务/系统/数据安全的威胁")
    suggestion: str = Field(default="", description="建议：给开发团队的具体修复动作")