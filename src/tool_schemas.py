"""Tool registry for the AI planning boundary.

Each deterministic tool declares the workflow stage where it may run, the
stage it advances to, and a small JSON Schema for its structured arguments.
The schema is intentionally simple: field names and primitive types only.
Cross-service execution should validate the same schema before invoking the
real Python handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.ai_planning import ToolCall
from src.workflow_session import WorkflowStage


@dataclass(frozen=True)
class ToolSpec:
    name: str
    stage: WorkflowStage
    advances_to: WorkflowStage | None
    description: str
    fields: dict[str, str] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    production_write: bool = False

    def input_schema(self) -> dict[str, Any]:
        properties = {
            name: {"type": field_type}
            for name, field_type in self.fields.items()
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(self.required),
            "properties": properties,
        }

    def validate_arguments(self, call: ToolCall) -> list[str]:
        errors: list[str] = []
        arguments = call.arguments or {}
        unknown = sorted(set(arguments) - set(self.fields))
        if unknown:
            errors.append(
                f"tool {self.name} received unknown arguments: "
                + ", ".join(unknown)
            )
        for name in self.required:
            if name not in arguments:
                errors.append(f"tool {self.name} requires argument {name!r}")
        return errors


def _path_tool(
    name: str,
    stage: WorkflowStage,
    advances_to: WorkflowStage | None,
    description: str,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        stage=stage,
        advances_to=advances_to,
        description=description,
        fields={"path": "string"},
        required=("path",),
    )


TOOL_SPECS: dict[str, ToolSpec] = {
    "analyze_drawing": _path_tool(
        "analyze_drawing",
        WorkflowStage.UPLOADED,
        WorkflowStage.ANALYZED,
        "Compute the drawing fingerprint for profile matching.",
    ),
    "match_profile": _path_tool(
        "match_profile",
        WorkflowStage.ANALYZED,
        WorkflowStage.PROFILE_MATCHED,
        "Match a validated case or rank drawing profiles.",
    ),
    "read_dxf": _path_tool(
        "read_dxf",
        WorkflowStage.PROFILE_MATCHED,
        WorkflowStage.READ,
        "Read DXF geometry into recognized parts.",
    ),
    "audit_drawing": _path_tool(
        "audit_drawing",
        WorkflowStage.READ,
        WorkflowStage.AUDITED,
        "Run drawing audit and return structured issues.",
    ),
    "assign_numbers": ToolSpec(
        name="assign_numbers",
        stage=WorkflowStage.AUDITED,
        advances_to=None,
        description="Assign original/automatic numbers; human confirms next.",
        fields={"path": "string"},
        required=("path",),
    ),
    "confirm_numbering": ToolSpec(
        name="confirm_numbering",
        stage=WorkflowStage.AUDITED,
        advances_to=WorkflowStage.NUMBERING_CONFIRMED,
        description="Record human confirmation of numbering.",
        fields={"decision": "string"},
        required=("decision",),
    ),
    "write_numbered_dxf": ToolSpec(
        name="write_numbered_dxf",
        stage=WorkflowStage.AUDITED,
        advances_to=None,
        description="Write the numbered in-place check DXF.",
        fields={"path": "string"},
        required=("path",),
        production_write=False,
    ),
    "nest_parts": ToolSpec(
        name="nest_parts",
        stage=WorkflowStage.NUMBERING_CONFIRMED,
        advances_to=WorkflowStage.NESTED_REPORTED,
        description="Run the deterministic nesting engine.",
        fields={"sheet_width": "number", "sheet_height": "number"},
        required=("sheet_width", "sheet_height"),
    ),
    "postprocess": ToolSpec(
        name="postprocess",
        stage=WorkflowStage.NESTED_REPORTED,
        advances_to=WorkflowStage.POSTPROCESS_CONFIRMED,
        description="Run postprocessing and geometry validation.",
        fields={"result": "object"},
        required=("result",),
    ),
    "write_dxf": ToolSpec(
        name="write_dxf",
        stage=WorkflowStage.POSTPROCESS_CONFIRMED,
        advances_to=None,
        description="Write the final nested DXF.",
        fields={"path": "string"},
        required=("path",),
        production_write=True,
    ),
    "write_report": ToolSpec(
        name="write_report",
        stage=WorkflowStage.POSTPROCESS_CONFIRMED,
        advances_to=None,
        description="Write the final nesting report.",
        fields={"path": "string"},
        required=("path",),
        production_write=True,
    ),
}


def get_tool_spec(tool: str) -> ToolSpec | None:
    return TOOL_SPECS.get(tool)


def tool_schema_payload() -> dict[str, Any]:
    """Return all tool schemas as a JSON-serializable registry."""
    return {
        name: {
            "stage": spec.stage.value,
            "advances_to": (
                spec.advances_to.value if spec.advances_to is not None else None
            ),
            "description": spec.description,
            "input_schema": spec.input_schema(),
            "production_write": spec.production_write,
        }
        for name, spec in sorted(TOOL_SPECS.items())
    }
