import pytest

from src.llm_plan_provider import LLMPlanProvider, PlanParseError, parse_tool_calls
from src.workflow_session import WorkflowSession


class FakeLLMClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete_json(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return self.response


def test_parse_tool_calls_accepts_object_with_tool_calls():
    calls = parse_tool_calls(
        {
            "tool_calls": [
                {"tool": "analyze_drawing", "arguments": {"path": "a.dxf"}}
            ]
        }
    )

    assert len(calls) == 1
    assert calls[0].tool == "analyze_drawing"
    assert calls[0].arguments == {"path": "a.dxf"}


def test_parse_tool_calls_rejects_missing_tool():
    with pytest.raises(PlanParseError):
        parse_tool_calls({"tool_calls": [{"arguments": {"path": "a.dxf"}}]})


def test_llm_plan_provider_builds_prompt_and_parses_response():
    session = WorkflowSession(session_id="llm-plan-1")
    client = FakeLLMClient(
        {
            "tool_calls": [
                {"tool": "analyze_drawing", "arguments": {"path": "a.dxf"}}
            ]
        }
    )
    provider = LLMPlanProvider(client)

    plan = provider.plan(session, {"project": "demo"})

    assert plan[0].tool == "analyze_drawing"
    assert len(client.calls) == 1
    system_prompt, user_prompt = client.calls[0]
    assert "planning layer" in system_prompt
    assert '"allowed_tools"' in user_prompt
