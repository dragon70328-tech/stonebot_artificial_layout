"""AI planning-layer boundary.

An AI may propose structured tool calls and configuration, but it must not
execute production code or bypass the workflow stage gate. This module
declares the allowed tools for each workflow stage and validates proposed
calls before they can be handed to the deterministic executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.workflow_session import WorkflowStage


TOOL_WHITELIST = {
    "analyze_drawing",
    "match_profile",
    "read_dxf",
    "audit_drawing",
    "assign_numbers",
    "confirm_numbering",
    "nest_parts",
    "postprocess",
    "write_dxf",
    "write_report",
}


STAGE_TOOLS: dict[WorkflowStage, set[str]] = {
    WorkflowStage.UPLOADED: {"analyze_drawing"},
    WorkflowStage.ANALYZED: {"match_profile"},
    WorkflowStage.PROFILE_MATCHED: {"read_dxf"},
    WorkflowStage.READ: {"audit_drawing"},
    WorkflowStage.AUDITED: {"assign_numbers"},
    WorkflowStage.NUMBERING_CONFIRMED: {"nest_parts"},
    WorkflowStage.NESTED_REPORTED: {"postprocess"},
    WorkflowStage.POSTPROCESS_CONFIRMED: {"write_dxf", "write_report"},
    WorkflowStage.COMPLETED: set(),
    WorkflowStage.BLOCKED: set(),
    WorkflowStage.CANCELLED: set(),
}

PRODUCTION_WRITE_TOOLS = {"write_dxf", "write_report"}


@dataclass
class ToolCall:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": self.arguments}


@dataclass
class PlanValidation:
    valid: bool
    errors: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "allowed_tools": self.allowed_tools,
        }


def allowed_tools_for_stage(stage: WorkflowStage | str) -> list[str]:
    stage = stage if isinstance(stage, WorkflowStage) else WorkflowStage(stage)
    return sorted(STAGE_TOOLS.get(stage, set()))


def validate_tool_call(
    stage: WorkflowStage | str,
    call: ToolCall,
    *,
    require_arguments: bool = True,
) -> PlanValidation:
    """Validate a single AI-proposed tool call for the current workflow stage."""
    stage = stage if isinstance(stage, WorkflowStage) else WorkflowStage(stage)
    errors: list[str] = []
    if call.tool not in TOOL_WHITELIST:
        errors.append(f"unknown tool: {call.tool}")
    if call.tool not in STAGE_TOOLS.get(stage, set()):
        errors.append(
            f"tool {call.tool} is not allowed in stage {stage.value}"
        )
    if call.tool in PRODUCTION_WRITE_TOOLS and stage != WorkflowStage.POSTPROCESS_CONFIRMED:
        errors.append(
            f"production write tool {call.tool} requires postprocess_confirmed"
        )
    if require_arguments and not call.arguments:
        errors.append(f"tool {call.tool} requires structured arguments")
    return PlanValidation(
        valid=not errors,
        errors=errors,
        allowed_tools=allowed_tools_for_stage(stage),
    )


def validate_plan(
    stage: WorkflowStage | str,
    calls: list[ToolCall],
) -> PlanValidation:
    """Validate a sequence of AI-proposed calls."""
    errors: list[str] = []
    for index, call in enumerate(calls):
        result = validate_tool_call(stage, call)
        if not result.valid:
            errors.extend(f"call[{index}] {error}" for error in result.errors)
    return PlanValidation(
        valid=not errors,
        errors=errors,
        allowed_tools=allowed_tools_for_stage(stage),
    )
