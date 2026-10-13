import time
from src.models.schema import TestCase, AgentTrace

class AgentRunner:
    def __init__(self, api_base: str, api_key: str, model: str):
        self.api_base = api_base
        self.api_key = api_key
        self.model = model

    def run_case(self, case: TestCase) -> AgentTrace:
        start = time.time()
        # TODO: 替换为真实的 Agent 调用逻辑（如 WorkBuddy API、OpenAI Function Calling）
        # 这里用模拟实现，让流水线先跑通
        try:
            trace = AgentTrace(
                case_id=case.id,
                agent_response="模拟响应：已拒绝该请求",
                tools_called=[],
                is_success=True
            )
        except Exception as e:
            trace = AgentTrace(
                case_id=case.id, agent_response="", 
                is_success=False, error_message=str(e)
            )
        trace.latency_ms = (time.time() - start) * 1000
        return trace