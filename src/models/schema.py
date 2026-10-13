from pydantic import BaseModel, Field
from typing import List, Optional

class TestCase(BaseModel):
    id: str = Field(..., description="用例唯一ID")
    category: str = Field(..., description="风险类别：权限边界/提示注入/幻觉/工具误用")
    input: str = Field(..., description="发给Agent的指令")
    allowed_tools: List[str] = Field(default=[], description="允许调用的工具白名单")
    forbidden_actions: List[str] = Field(default=[], description="禁止的动作")
    expected_behavior: str = Field(..., description="期望行为")
    severity: str = Field(..., description="严重等级：low/medium/high")

class AgentTrace(BaseModel):
    case_id: str
    agent_response: str
    tools_called: List[str] = []
    latency_ms: float = 0.0
    token_cost: int = 0
    is_success: bool = True
    error_message: Optional[str] = None

class EvalResult(BaseModel):
    case_id: str
    category: str
    passed: bool
    risk_level: str
    judge_layer: str  # 记录是哪一层裁判做出的决定：rule/llm
    judge_reason: str
    token_cost: int = 0