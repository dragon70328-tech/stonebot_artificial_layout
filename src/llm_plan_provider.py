"""Convert structured LLM output into the next agent tool plan."""

from __future__ import annotations

import json
from typing import Any

from src.ai_planning import ToolCall, allowed_tools_for_stage
from src.agent_loop import PlanProvider
from src.tool_schemas import tool_schema_payload
from src.workflow_session import WorkflowSession


SYSTEM_PROMPT = """\
You are the planning layer for a stone fabrication nesting system.
You must not execute code or write production files yourself.
Return only a JSON object with a "tool_calls" array. Each item must contain:
  {"tool": "<allowed_tool>", "arguments": { ... }}
Use only the tools listed in the current stage. Stop at human confirmation gates.
"""


class PlanParseError(ValueError):
    """Raised when LLM output cannot be converted into valid ToolCall objects."""


def build_plan_prompt(session: WorkflowSession, context: Any = None) -> dict[str, Any]:
    allowed_tools = allowed_tools_for_stage(session.stage)
    all_schemas = tool_schema_payload()
    stage_schemas = {
        name: all_schemas[name]
        for name in allowed_tools
        if name in all_schemas
    }
    job_snapshot = context.snapshot() if hasattr(context, "snapshot") else context
    return {
        "stage": session.current_stage,
        "allowed_tools": allowed_tools,
        "tool_schemas": stage_schemas,
        "job": job_snapshot,
    }


def parse_tool_calls(raw: Any) -> list[ToolCall]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlanParseError("LLM output is not valid JSON") from exc
    if isinstance(raw, dict):
        items = raw.get("tool_calls", raw.get("calls", raw.get("plan")))
    elif isinstance(raw, list):
        items = raw
    else:
        raise PlanParseError("LLM output must be a JSON object or array")
    if not isinstance(items, list):
        raise PlanParseError("tool_calls must be an array")

    calls: list[ToolCall] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PlanParseError(f"tool_calls[{index}] is not an object")
        tool = item.get("tool")
        arguments = item.get("arguments", {})
        if not isinstance(tool, str) or not tool:
            raise PlanParseError(f"tool_calls[{index}] is missing tool")
        if not isinstance(arguments, dict):
            raise PlanParseError(f"tool_calls[{index}] arguments must be an object")
        calls.append(ToolCall(tool=tool, arguments=arguments))
    return calls


class LLMPlanProvider:
    def __init__(self, client) -> None:
        self.client = client

    def plan(self, session: WorkflowSession, context: Any = None) -> list[ToolCall]:
        prompt = build_plan_prompt(session, context)
        raw = self.client.complete_json(SYSTEM_PROMPT, json.dumps(prompt, ensure_ascii=False))
        return parse_tool_calls(raw)
