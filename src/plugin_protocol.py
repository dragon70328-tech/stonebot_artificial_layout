"""Stable Agent-facing protocol for the Stonebot drawing and nesting plugin.

The protocol deliberately exposes a small set of business-level tools.  Agent
harnesses must not orchestrate internal implementation modules or write DXF
files directly.  The plugin service owns workflow gates and delegates all
deterministic work to the existing Stonebot runtime.
"""

from __future__ import annotations

from typing import Any


PLUGIN_VERSION = "0.2.0"
SCHEMA_VERSION = "0.1.0"
MCP_PROTOCOL_VERSION = "2025-06-18"


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
    }


def _job_id() -> dict[str, str]:
    return {"type": "string", "minLength": 1}


def _decision() -> dict[str, Any]:
    return {
        "type": "string",
        "enum": ["confirm", "cancel"],
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "stonebot_analyze_drawing",
        "description": (
            "Create a Stonebot job from a local DXF path, match a drawing "
            "profile, read geometry, audit the drawing, assign numbers and "
            "write an in-place numbered DXF. Returns the first human "
            "confirmation gate."
        ),
        "inputSchema": _object_schema(
            {
                "dxf_path": {"type": "string", "minLength": 1},
                "tenant_id": {"type": "string", "default": "default"},
                "project_id": {"type": "string"},
                "skip_unnumbered": {"type": "boolean", "default": True},
                "cold_start": {"type": "boolean", "default": False},
                "use_template": {"type": "boolean", "default": False},
            },
            ["dxf_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_configure_nesting",
        "description": (
            "Configure the sheet, process and optimizer parameters before a "
            "confirmed drawing is nested. This never starts nesting."
        ),
        "inputSchema": _object_schema(
            {
                "job_id": _job_id(),
                "width_mm": {"type": "number", "exclusiveMinimum": 0},
                "height_mm": {"type": "number", "exclusiveMinimum": 0},
                "thickness_mm": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 20,
                },
                "process": {
                    "type": "string",
                    "enum": ["bridge", "waterjet", "laser"],
                },
                "kerf_mm": {"type": "number", "minimum": 0, "default": 0},
                "profile": {
                    "type": "string",
                    "enum": [
                        "textured",
                        "min_sheets",
                        "balanced",
                        "quick",
                        "waterjet",
                        "laser",
                    ],
                },
                "rotation_degrees": {
                    "type": "array",
                    "items": {"type": "number"},
                },
                "free_rotation": {"type": "boolean", "default": False},
                "min_gap": {"type": "number", "minimum": 0},
                "usable_width_mm": {"type": "number", "exclusiveMinimum": 0},
                "usable_height_mm": {"type": "number", "exclusiveMinimum": 0},
                "oversize_enabled": {"type": "boolean", "default": False},
                "oversize_mode": {
                    "type": "string",
                    "enum": ["total", "per_side"],
                    "default": "total",
                },
                "oversize_width_mm": {
                    "type": "number",
                    "minimum": 0,
                    "default": 0,
                },
                "oversize_height_mm": {
                    "type": "number",
                    "minimum": 0,
                    "default": 0,
                },
                "allow_shared_edge": {"type": "boolean", "default": True},
                "group_mode": {
                    "type": ["string", "null"],
                    "enum": ["one_set_per_sheet", None],
                },
                "slide_to_edge": {"type": "boolean", "default": True},
                "align_edges": {"type": "boolean", "default": True},
                "first_part_left_edge": {
                    "type": "boolean",
                    "default": False,
                },
                "trials": {"type": "integer", "minimum": 1, "default": 1},
                "seed": {"type": "integer", "default": 0},
                "budget": {"type": "number", "minimum": 0, "default": 0},
                "quick": {"type": "boolean", "default": True},
                "pairing": {"type": "boolean", "default": False},
                "beam": {"type": "boolean", "default": True},
                "beam_width": {"type": "integer", "minimum": 1},
                "compress": {"type": "boolean", "default": True},
                "rect_fast": {"type": "boolean", "default": True},
                "low_fill_rework": {"type": "boolean", "default": False},
                "low_fill_threshold": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 1,
                    "default": 0.6,
                },
                "candidate_mode": {
                    "type": "string",
                    "enum": ["bbox", "nfp_lite"],
                    "default": "bbox",
                },
            },
            ["job_id", "width_mm", "height_mm", "process"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_list_drawing_issues",
        "description": (
            "Read a page of drawing audit issues. Use this instead of asking "
            "for all issues in the job status."
        ),
        "inputSchema": _object_schema(
            {
                "job_id": _job_id(),
                "type": {"type": "string"},
                "severity": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                },
            },
            ["job_id"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_list_correction_targets",
        "description": (
            "Read a page of recognized panels, holes and unrecognized closed "
            "entities with stable target IDs for correction plans."
        ),
        "inputSchema": _object_schema(
            {
                "job_id": _job_id(),
                "query": {"type": "string"},
                "include_unrecognized": {
                    "type": "boolean",
                    "default": True,
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                },
            },
            ["job_id"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_correct_drawing",
        "description": (
            "Apply a structured CorrectionPlan to the numbered drawing and "
            "generate a new immutable revision. Only use target IDs returned "
            "by stonebot_list_correction_targets."
        ),
        "inputSchema": _object_schema(
            {
                "job_id": _job_id(),
                "plan": {
                    "type": "object",
                    "required": ["instruction", "operations"],
                    "properties": {
                        "plan_id": {"type": "string"},
                        "instruction": {"type": "string", "minLength": 1},
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "object"},
                        },
                        "created_by": {
                            "type": "string",
                            "enum": ["user", "ai", "system"],
                        },
                        "feedback_scope": {
                            "type": "string",
                            "enum": ["project", "global"],
                        },
                    },
                },
            },
            ["job_id", "plan"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_rename_panels",
        "description": (
            "High-level correction for renaming several recognized panels. "
            "Use stable target IDs returned by "
            "stonebot_list_correction_targets."
        ),
        "inputSchema": _object_schema(
            {
                "job_id": _job_id(),
                "instruction": {
                    "type": "string",
                    "default": "按用户要求修正编号",
                },
                "renames": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["target_id", "new_number"],
                        "properties": {
                            "target_id": {"type": "string", "minLength": 1},
                            "new_number": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
            ["job_id", "renames"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_confirm_drawing",
        "description": (
            "Close the human drawing gate. A confirm decision also starts "
            "nesting using the parameters previously supplied to "
            "stonebot_configure_nesting."
        ),
        "inputSchema": _object_schema(
            {"job_id": _job_id(), "decision": _decision()},
            ["job_id", "decision"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_confirm_nesting",
        "description": (
            "Close the human board-count gate. A confirm decision runs "
            "postprocessing and deterministic geometry/process validation."
        ),
        "inputSchema": _object_schema(
            {"job_id": _job_id(), "decision": _decision()},
            ["job_id", "decision"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_choose_output_directory",
        "description": (
            "Open a local folder picker so the user can choose where "
            "deliverables are saved. Call this before final DXF delivery "
            "and before an explicitly requested Word report. Never infer "
            "or silently reuse a save directory."
        ),
        "inputSchema": _object_schema(
            {
                "purpose": {
                    "type": "string",
                    "enum": ["delivery", "word_report"],
                    "default": "delivery",
                }
            }
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_finalize_job",
        "description": (
            "Close the final gate and copy exactly two deliverables into the "
            "user-selected folder: the in-place numbered DXF and the nested "
            "DXF. A save directory is mandatory. No report is generated by "
            "this tool. The server blocks this action when hard validation "
            "fails."
        ),
        "inputSchema": _object_schema(
            {
                "job_id": _job_id(),
                "decision": _decision(),
                "output_directory": {
                    "type": "string",
                    "minLength": 1,
                },
                "overwrite": {"type": "boolean", "default": False},
            },
            ["job_id", "decision", "output_directory"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_export_word_report",
        "description": (
            "Generate a Word nesting report only when the user explicitly "
            "requests a Word report. First ask for a save folder with "
            "stonebot_choose_output_directory."
        ),
        "inputSchema": _object_schema(
            {
                "job_id": _job_id(),
                "output_directory": {
                    "type": "string",
                    "minLength": 1,
                },
                "overwrite": {"type": "boolean", "default": False},
            },
            ["job_id", "output_directory"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_advance_until_gate",
        "description": (
            "Compact-profile wrapper for one explicit human confirmation. "
            "Using the requested gate, it runs the same deterministic action "
            "as the corresponding confirm tool and stops at the next gate."
        ),
        "inputSchema": _object_schema(
            {
                "job_id": _job_id(),
                "gate": {
                    "type": "string",
                    "enum": ["drawing", "nesting", "final"],
                },
                "decision": _decision(),
                "output_directory": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["job_id", "gate", "decision"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_get_agent_contract",
        "description": (
            "Return the portable Stonebot Agent contract, including workflow "
            "gates, policies and tool profiles for Codex or a lower-level "
            "Harness adapter."
        ),
        "inputSchema": _object_schema(
            {
                "profile": {
                    "type": "string",
                    "enum": ["full", "compact"],
                    "default": "full",
                }
            }
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_list_profile_patches",
        "description": (
            "List project-scoped candidate profile patches generated from "
            "confirmed drawing corrections. Listing never activates a patch."
        ),
        "inputSchema": _object_schema(
            {
                "tenant_id": {"type": "string"},
                "project_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
                        "candidate",
                        "validated",
                        "active",
                        "rejected",
                    ],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                },
            }
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_accept_profile_patch",
        "description": (
            "Validate and activate one candidate profile patch as a "
            "project-scoped override. Call only after explicit user approval."
        ),
        "inputSchema": _object_schema(
            {"patch_id": {"type": "string", "minLength": 1}},
            ["patch_id"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_review_profile_patch",
        "description": (
            "Dry-run one candidate profile patch against the corrected "
            "source drawing and all available historical fixtures for the "
            "base profile. This never activates the patch."
        ),
        "inputSchema": _object_schema(
            {"patch_id": {"type": "string", "minLength": 1}},
            ["patch_id"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_reject_profile_patch",
        "description": (
            "Reject a candidate profile patch. Call only after explicit user "
            "decision."
        ),
        "inputSchema": _object_schema(
            {"patch_id": {"type": "string", "minLength": 1}},
            ["patch_id"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_list_profile_overrides",
        "description": (
            "List active project profile overrides and their activation "
            "history. This is read-only."
        ),
        "inputSchema": _object_schema(
            {
                "tenant_id": {"type": "string", "default": "default"},
                "project_id": {"type": "string", "minLength": 1},
                "base_profile_name": {"type": "string"},
            },
            ["project_id"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_rollback_profile_override",
        "description": (
            "Roll a project profile override back to its previous version or "
            "to the immutable base profile. Call only after explicit user "
            "approval."
        ),
        "inputSchema": _object_schema(
            {
                "tenant_id": {"type": "string", "default": "default"},
                "project_id": {"type": "string", "minLength": 1},
                "base_profile_name": {"type": "string", "minLength": 1},
                "target_patch_id": {"type": "string"},
            },
            ["project_id", "base_profile_name"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_job_status",
        "description": (
            "Return a compact job summary, validation state, next actions and "
            "artifact references."
        ),
        "inputSchema": _object_schema({"job_id": _job_id()}, ["job_id"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_list_jobs",
        "description": "List jobs stored by this Stonebot plugin service.",
        "inputSchema": _object_schema(
            {"tenant_id": {"type": "string"}},
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_cancel_job",
        "description": "Cancel a job only when the user explicitly asks.",
        "inputSchema": _object_schema(
            {
                "job_id": _job_id(),
                "reason": {
                    "type": "string",
                    "default": "用户取消任务",
                },
            },
            ["job_id"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
]


def next_actions(stage: str, *, nesting_configured: bool) -> list[str]:
    if stage == "audited":
        actions: list[str] = []
        if not nesting_configured:
            actions.append("stonebot_configure_nesting")
        actions.extend(
            [
                "stonebot_list_drawing_issues",
                "stonebot_list_correction_targets",
                "stonebot_correct_drawing",
                "stonebot_list_profile_patches",
                "stonebot_confirm_drawing",
                "stonebot_advance_until_gate",
                "stonebot_cancel_job",
            ]
        )
        return actions
    if stage == "nested_reported":
        return [
            "stonebot_confirm_nesting",
            "stonebot_advance_until_gate",
            "stonebot_cancel_job",
        ]
    if stage == "postprocess_confirmed":
        return [
            "stonebot_choose_output_directory",
            "stonebot_finalize_job",
            "stonebot_advance_until_gate",
            "stonebot_cancel_job",
        ]
    if stage in {"completed", "cancelled"}:
        return []
    if stage == "blocked":
        return ["stonebot_job_status", "stonebot_cancel_job"]
    return ["stonebot_job_status"]


def tool_result(
    structured: dict[str, Any],
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    """Build an MCP tools/call result while keeping a text fallback."""
    import json

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    structured,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        ],
        "structuredContent": structured,
        "isError": is_error,
    }
