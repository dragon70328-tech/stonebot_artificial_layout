"""Deterministic drawing intake workflow helpers.

This module extracts the read/profile/assignment/material-grouping phase out
of ``main.run()`` so it can be tested independently and reused by the future
workflow state machine without turning the CLI entrypoint into a god object.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.drawing_profile import (
    analyze_drawing,
    load_profiles,
    rank_profiles,
    read_dxf_with_profile,
)
from src.case_library import load_cases, load_case_profile, match_case
from src.dxf_reader import read_dxf
from src.input_guard import DXFInputLimits, guarded_read
from src.numbering import assign_numbers
from src.recognized_contract import parts_data_to_recognized_drawing


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIN_PROFILE_SCORE = 30.0
MIN_CASE_SCORE = 75.0


@dataclass
class PreparedDrawing:
    """Result of reading a DXF and preparing parts for nesting."""

    parts: list[Any] = field(default_factory=list)
    drawing_profile: Any | None = None
    recognized_drawing: Any | None = None
    skipped_material_numbers: list[str] = field(default_factory=list)
    groups: list[str | None] = field(default_factory=lambda: [None])
    material_group_enabled: bool = False
    total_area: float = 0.0
    error: str | None = None


def resolve_drawing_profile(dxf_path: str):
    """Match a validated case first, then fall back to profile ranking."""
    try:
        fingerprint = analyze_drawing(dxf_path)
        profiles = load_profiles(PROJECT_ROOT / "drawing_profiles")
        cases = load_cases(PROJECT_ROOT / "drawing_profiles" / "validated_cases")
        case_match = match_case(fingerprint, cases, min_score=MIN_CASE_SCORE)
        if case_match is not None:
            case, case_score = case_match
            profile = load_case_profile(case, profiles)
            if profile is not None:
                print(
                    f"matched validated case: {case.case_id} "
                    f"(score={case_score:.1f}, profile={profile.name})"
                )
                return profile
        ranked = rank_profiles(fingerprint, profiles)
    except Exception:
        return None
    if not ranked:
        return None
    profile, score = ranked[0]
    if score < MIN_PROFILE_SCORE:
        return None
    return profile


def material_group_for_number(number: str, drawing_profile) -> str | None:
    """Return the normalized material prefix when a drawing profile enables it."""
    if drawing_profile is None or not drawing_profile.material_group_enabled:
        return None
    match = re.match(drawing_profile.material_prefix_pattern, number or "")
    if not match:
        return None
    prefix = (
        match.group("prefix")
        if "prefix" in match.groupdict()
        else match.group(0)
    ).upper()
    allowed = {value.upper() for value in drawing_profile.allowed_material_prefixes}
    if allowed and prefix not in allowed:
        return None
    return prefix


def apply_material_grouping(parts, drawing_profile):
    """Assign material groups and remove parts without an allowed material prefix."""
    if drawing_profile is None or not drawing_profile.material_group_enabled:
        return parts, []

    grouped = []
    skipped = []
    for part in parts:
        prefix = material_group_for_number(part.number, drawing_profile)
        if prefix is None:
            skipped.append(part.number)
            continue
        part.material_group = prefix
        grouped.append(part)
    return grouped, skipped


def assign_group_ids(parts, drawing_profile, group_mode: str | None) -> list[Any]:
    """Assign ``Part.group_id`` for ``one_set_per_sheet`` without touching material groups."""
    if group_mode != "one_set_per_sheet":
        return parts

    pattern_text = (
        drawing_profile.group_id_pattern
        if drawing_profile is not None and drawing_profile.group_id_pattern
        else r"^(?P<group>[^-]+)"
    )
    pattern = re.compile(pattern_text)
    for part in parts:
        match = pattern.match(part.number or "")
        group = None
        if match:
            group = (
                match.groupdict().get("group")
                or match.groupdict().get("unit")
                or match.group(0)
            )
        part.group_id = group or (part.material_group or part.number or "UNGROUPED")
    return parts


def prepare_drawing(
    dxf_path: str,
    *,
    drawing_profile=None,
    skip_unnumbered: bool = True,
    layers: list[str] | None = None,
    exclude_layers: list[str] | None = None,
    reader_options: dict[str, Any] | None = None,
    group_mode: str | None = None,
    input_limits: DXFInputLimits | None = None,
) -> PreparedDrawing:
    """Resolve a profile, read DXF, number parts, and apply material grouping."""
    print(f"读取 DXF: {dxf_path}")

    if drawing_profile is None:
        drawing_profile = resolve_drawing_profile(dxf_path)
    reader_options = reader_options or {}

    if drawing_profile is not None:
        panel_layer_count = len(
            drawing_profile.panel_layers
            or (
                [drawing_profile.panel_layer]
                if drawing_profile.panel_layer else []
            )
        )
        print(f"matched drawing profile: {drawing_profile.name} "
              f"(panel_layers={panel_layer_count}, "
              f"number_layers={len(drawing_profile.number_layers)})")
        effective_panel_layers = (
            layers
            if layers is not None
            else (
                drawing_profile.panel_layers
                or (
                    [drawing_profile.panel_layer]
                    if drawing_profile.panel_layer else None
                )
            )
        )
        effective_number_layers = drawing_profile.number_layers or None
        effective_label_pattern = drawing_profile.label_pattern or None
        effective_exclude_linetypes = drawing_profile.exclude_linetypes or None
    else:
        effective_panel_layers = layers
        effective_number_layers = None
        effective_label_pattern = None
        effective_exclude_linetypes = None

    if drawing_profile is not None:
        read_profile = lambda path: read_dxf_with_profile(
            path,
            drawing_profile,
            panel_layers=effective_panel_layers,
            exclude_layers=exclude_layers,
        )
        if input_limits is not None:
            parts_data, _doc = guarded_read(dxf_path, read_profile, input_limits)
        else:
            parts_data, _doc = read_profile(dxf_path)
    else:
        exclude_linetypes = reader_options.get(
            "exclude_linetypes", effective_exclude_linetypes
        )
        read_generic = lambda path: read_dxf(
            path,
            panel_layers=effective_panel_layers,
            exclude_layers=exclude_layers,
            exclude_linetypes=exclude_linetypes,
            number_layers=effective_number_layers,
            label_pattern=effective_label_pattern,
            number_layer_keyword=reader_options.get(
                "number_layer_keyword", "编"
            ),
            room_label_keyword=reader_options.get(
                "room_label_keyword", "户型"
            ),
            room_label_exclude_keyword=reader_options.get(
                "room_label_exclude_keyword", "套"
            ),
            room_label_normalizations=reader_options.get(
                "room_label_normalizations"
            ),
            room_max_distance=reader_options.get("room_max_distance", 5000.0),
        )
        if input_limits is not None:
            parts_data, _doc = guarded_read(dxf_path, read_generic, input_limits)
        else:
            parts_data, _doc = read_generic(dxf_path)

    if not parts_data:
        return PreparedDrawing(
            drawing_profile=drawing_profile,
            error="未找到任何封闭图形。",
        )

    recognized_drawing = parts_data_to_recognized_drawing(
        parts_data,
        dxf_path,
        profile_name=drawing_profile.name if drawing_profile else None,
        closed_tolerance=(
            drawing_profile.closed_tolerance
            if drawing_profile is not None
            else 0.01
        ),
    )

    parts = assign_numbers(parts_data, skip_unnumbered=skip_unnumbered)
    skipped_material_numbers: list[str] = []
    if drawing_profile is not None and drawing_profile.material_group_enabled:
        parts, skipped_material_numbers = apply_material_grouping(
            parts, drawing_profile
        )
        if skipped_material_numbers:
            print(f"已跳过无材料前缀件 {len(skipped_material_numbers)} 个")
        group_counts = Counter(p.material_group for p in parts)
        print("材料分组: " + ", ".join(
            f"{key}: {value} ?" for key, value in sorted(group_counts.items())
        ))

    parts = assign_group_ids(parts, drawing_profile, group_mode)

    if not parts:
        return PreparedDrawing(
            drawing_profile=drawing_profile,
            recognized_drawing=recognized_drawing,
            skipped_material_numbers=skipped_material_numbers,
            error="材料分组后没有可排板零件。",
        )

    material_group_enabled = bool(
        drawing_profile is not None and drawing_profile.material_group_enabled
    )
    groups = (
        sorted({part.material_group for part in parts})
        if material_group_enabled
        else [None]
    )

    return PreparedDrawing(
        parts=parts,
        drawing_profile=drawing_profile,
        recognized_drawing=recognized_drawing,
        skipped_material_numbers=skipped_material_numbers,
        groups=groups,
        material_group_enabled=material_group_enabled,
        total_area=sum(p.area for p in parts),
    )
