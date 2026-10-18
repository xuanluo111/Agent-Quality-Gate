from pydantic import BaseModel, Field
from typing import List, Optional

class TestCase(BaseModel):
    """测试用例定义"""
    id: str = Field(..., description="用例唯一ID")
    category: str = Field(..., description="风险类别")
    input: str = Field(..., description="发给Agent的指令")
    allowed_tools: List[str] = Field(default_factory=list, description="允许调用的工具白名单")
    forbidden_actions: List[str] = Field(default_factory=list, description="禁止的动作")
    expected_behavior: str = Field(..., description="期望行为")
    severity: str = Field(..., description="严重等级")
    # 潘老师：因果归因
    attack_logic: str = Field(default="", description="攻击底层逻辑")

class AgentTrace(BaseModel):
    """Agent 执行轨迹"""
    case_id: str
    agent_response: str
    tools_called: List[str] = Field(default_factory=list)
    tool_call_sequence: List[str] = Field(default_factory=list)
    
    # 你的原版审计字段
    tool_call_params: List[dict] = Field(default_factory=list, description="每次调用的参数")
    tool_call_count: int = Field(default=0, description="工具调用总次数")
    retry_count: int = Field(default=0, description="失败重试次数")
    should_escalate_to_human: bool = Field(default=False, description="是否应转人工")
    did_escalate_to_human: bool = Field(default=False, description="是否实际转人工")

    # 姚老师：防作弊与长程监控
    is_cheated: bool = Field(default=False, description="是否触发防作弊探针")
    round_history: List[str] = Field(default_factory=list, description="MockUser 多轮施压记录")

    latency_ms: float = 0.0
    token_cost: int = 0
    is_success: bool = True
    error_message: Optional[str] = None

class EvalResult(BaseModel):
    """评测结果"""
    case_id: str
    category: str
    # 姚老师：双轨制
    passed: bool = Field(..., description="Standard 宽松模式")
    strict_passed: bool = Field(default=True, description="Strict 严格模式")
    risk_level: str
    judge_layer: str = Field(..., description="rule/trajectory/llm")
    judge_reason: str = Field(default="", description="简要理由")
    
    # 姚老师：错因分类
    fail_reason: str = Field(default="None", description="Failed_Reasoning / Lack_of_Knowledge / Fail_Instruction_Following / Safety_Refusal / Eval_Awareness_Cheating")

    # 潘老师：结构化诊断
    phenomenon: str = Field(default="", description="现象")
    evidence: str = Field(default="", description="依据")
    impact: str = Field(default="", description="影响")
    suggestion: str = Field(default="", description="建议")
    
    token_cost: int = 0