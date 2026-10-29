"""Portable, Harness-neutral contract for Stonebot Agent integrations.

Codex, DeepSeek and future Harnesses consume the same workflow gates and tool
definitions.  Harnesses only differ in how many tools they can manage and
whether they drive the tool loop themselves.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.plugin_protocol import (
    PLUGIN_VERSION,
    SCHEMA_VERSION,
    TOOL_DEFINITIONS,
)


TOOL_PROFILES: dict[str, list[str]] = {
    "full": [
        "stonebot_analyze_drawing",
        "stonebot_configure_nesting",
        "stonebot_list_drawing_issues",
        "stonebot_list_correction_targets",
        "stonebot_correct_drawing",
        "stonebot_rename_panels",
        "stonebot_confirm_drawing",
        "stonebot_confirm_nesting",
        "stonebot_choose_output_directory",
        "stonebot_finalize_job",
        "stonebot_export_word_report",
        "stonebot_advance_until_gate",
        "stonebot_get_agent_contract",
        "stonebot_list_profile_patches",
        "stonebot_review_profile_patch",
        "stonebot_accept_profile_patch",
        "stonebot_reject_profile_patch",
        "stonebot_list_profile_overrides",
        "stonebot_rollback_profile_override",
        "stonebot_job_status",
        "stonebot_list_jobs",
        "stonebot_cancel_job",
    ],
    "compact": [
        "stonebot_analyze_drawing",
        "stonebot_configure_nesting",
        "stonebot_list_drawing_issues",
        "stonebot_list_correction_targets",
        "stonebot_correct_drawing",
        "stonebot_rename_panels",
        "stonebot_choose_output_directory",
        "stonebot_export_word_report",
        "stonebot_advance_until_gate",
        "stonebot_get_agent_contract",
        "stonebot_list_profile_patches",
        "stonebot_review_profile_patch",
        "stonebot_accept_profile_patch",
        "stonebot_reject_profile_patch",
        "stonebot_list_profile_overrides",
        "stonebot_rollback_profile_override",
        "stonebot_job_status",
        "stonebot_list_jobs",
        "stonebot_cancel_job",
    ],
}


def tool_definitions_for_profile(
    profile: str = "full",
) -> list[dict[str, Any]]:
    if profile not in TOOL_PROFILES:
        raise ValueError(f"unknown tool profile: {profile}")
    allowed = set(TOOL_PROFILES[profile])
    return [
        tool
        for tool in TOOL_DEFINITIONS
        if tool["name"] in allowed
    ]


def openai_tool_definitions(
    profile: str = "full",
) -> list[dict[str, Any]]:
    """Return OpenAI-compatible function definitions for fallback adapters."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["inputSchema"],
            },
        }
        for tool in tool_definitions_for_profile(profile)
    ]


def agent_contract(profile: str = "full") -> dict[str, Any]:
    if profile not in TOOL_PROFILES:
        raise ValueError(f"unknown tool profile: {profile}")
    return {
        "schema_version": SCHEMA_VERSION,
        "plugin_version": PLUGIN_VERSION,
        "workflow_version": "1.0.0",
        "name": "stonebot-artificial-stone-layout",
        "tool_profile": profile,
        "tools": tool_definitions_for_profile(profile),
        "tool_profiles": {
            name: {
                "tool_names": names,
                "recommended_for": recommended,
            }
            for name, names, recommended in [
                (
                    "full",
                    TOOL_PROFILES["full"],
                    "Codex or a Harness with a reliable multi-step tool loop",
                ),
                (
                    "compact",
                    TOOL_PROFILES["compact"],
                    (
                        "A lower-level Harness with limited context or "
                        "simpler tool orchestration"
                    ),
                ),
            ]
        },
        "workflow": {
            "stages": [
                "uploaded",
                "analyzed",
                "profile_matched",
                "read",
                "audited",
                "numbering_confirmed",
                "nested_reported",
                "postprocess_confirmed",
                "completed",
            ],
            "human_gates": [
                {
                    "gate": "drawing",
                    "stage": "audited",
                    "full_tool": "stonebot_confirm_drawing",
                    "compact_tool": "stonebot_advance_until_gate",
                    "requires_explicit_user_confirmation": True,
                },
                {
                    "gate": "nesting",
                    "stage": "nested_reported",
                    "full_tool": "stonebot_confirm_nesting",
                    "compact_tool": "stonebot_advance_until_gate",
                    "requires_explicit_user_confirmation": True,
                },
                {
                    "gate": "final",
                    "stage": "postprocess_confirmed",
                    "full_tool": "stonebot_finalize_job",
                    "compact_tool": "stonebot_advance_until_gate",
                    "requires_explicit_user_confirmation": True,
                    "requires_user_selected_output_directory": True,
                },
            ],
        },
        "modes": {
            "host_driven": (
                "The Harness selects individual tools and interprets "
                "next_actions returned by Stonebot."
            ),
            "service_driven": (
                "A weaker Harness uses compact tools; Stonebot executes one "
                "deterministic transition per explicit gate decision."
            ),
        },
        "policies": {
            "server_is_source_of_truth": True,
            "agent_may_edit_dxf": False,
            "agent_may_write_production_artifacts": False,
            "success_requires_structured_validation": True,
            "profile_patch_activation_requires_user": True,
            "base_profile_is_immutable": True,
            "maximum_automatic_retry_rounds": 3,
            "stop_when_blocked": True,
            "confirmation_decisions": ["confirm", "cancel"],
            "artifact_delivery": "user_selected_directory_only",
            "default_deliverables": [
                "numbered_dxf",
                "nested_dxf",
            ],
            "word_report_default": "disabled",
            "word_report_requires_user_request": True,
            "internal_staging_is_not_delivery": True,
            "geometry_transport": "plugin_internal_only",
        },
        "context_limits": {
            "pagination_default": 50,
            "pagination_max": 200,
            "inline_geometry": False,
        },
        "openai_compatible_tools": openai_tool_definitions(profile),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the portable Stonebot Agent contract",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(TOOL_PROFILES),
        default="full",
    )
    parser.add_argument(
        "--format",
        choices=["contract", "openai-tools"],
        default="contract",
    )
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    payload: Any = (
        openai_tool_definitions(args.profile)
        if args.format == "openai-tools"
        else agent_contract(args.profile)
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
