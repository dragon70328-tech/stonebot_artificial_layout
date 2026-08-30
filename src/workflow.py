"""Deterministic drawing intake workflow helpers.

This module extracts the read/profile/assignment/material-grouping phase out
of ``main.run()`` so it can be tested independently and reused by the future
workflow state machine without turning the CLI entrypoint into a god object.
"""

from __future__ import annotations

import itertools
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import shapely
from shapely import affinity
from shapely.geometry import Point

from src.drawing_profile import (
    analyze_drawing,
    load_profiles,
    rank_profiles,
    read_dxf_with_profile,
)
from src.case_library import load_cases, load_case_profile, match_case
from src.deepnest_engine import nest_parts_deepnest
from src.dxf_reader import read_dxf
from src.input_guard import DXFInputLimits, guarded_read
from src.models import NestingResult, Part, Sheet
from src.nesting import nest_parts, validate_nesting
from src.numbering import assign_numbers
from src.pairing import nest_parts_deepnest_paired
from src.postprocess import PostProcessor, diagnose_postprocess, diagnose_waterjet
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



# ═══════════════════════════════════════════════════════════════
#  排板编排（从 main.py 拆出的确定性工具）
# ═══════════════════════════════════════════════════════════════

EPS = 1e-6

QUICK_CONFIGS = [
    ("short", "skyline", 0),
    ("short", "col", 0),
    ("area", "skyline", 0),
    ("long", "col", 0),
]


def part_fits(part, width: float, height: float, rotations: list[int]) -> bool:
    outer = part.outer_polygon
    origin = (outer.centroid.x, outer.centroid.y)
    for angle in rotations:
        rotated = outer if not angle else affinity.rotate(outer, angle,
                                                          origin=origin)
        b = rotated.bounds
        if b[2] - b[0] <= width + EPS and b[3] - b[1] <= height + EPS:
            return True
    return False


def _normalized_rotated_polygon(polygon, angle: float):
    """绕质心旋转多边形，并将其包围盒左下角平移到原点。"""
    if angle:
        rotated = affinity.rotate(
            polygon, angle, origin=(polygon.centroid.x, polygon.centroid.y)
        )
    else:
        rotated = polygon
    minx, miny, _, _ = rotated.bounds
    if abs(minx) > EPS or abs(miny) > EPS:
        rotated = affinity.translate(rotated, xoff=-minx, yoff=-miny)
    return rotated


def _candidate_offsets(polygon, sheet_size: float, axis: str) -> list[float]:
    """候选左下角偏移：板边对齐 + 多边形顶点对齐。"""
    bounds = polygon.bounds
    size = bounds[2] - bounds[0] if axis == "x" else bounds[3] - bounds[1]
    values = {0.0, sheet_size - size}
    for x, y in polygon.exterior.coords:
        v = x if axis == "x" else y
        values.add(-v)
        values.add(sheet_size - v)
    return sorted(v for v in values
                  if v >= -EPS and v <= sheet_size - size + EPS)


def _rotated_part_form(part, angle: float):
    """返回 (旋转后的外轮廓, (宽, 高))，外轮廓包围盒左下角已归一化。"""
    polygon = _normalized_rotated_polygon(part.outer_polygon, angle)
    bounds = polygon.bounds
    return polygon, (bounds[2] - bounds[0], bounds[3] - bounds[1])


def _place_part_copy(part, angle: float, x: float, y: float):
    """生成放置后的 Part 副本，几何与孔洞/标签一起做刚体变换。"""
    outer = part.outer_polygon
    origin = (outer.centroid.x, outer.centroid.y)
    rotated_outer = (affinity.rotate(outer, angle, origin=origin)
                     if angle else outer)
    ox = x - rotated_outer.bounds[0]
    oy = y - rotated_outer.bounds[1]

    def transform(geom):
        g = affinity.rotate(geom, angle, origin=origin) if angle else geom
        return affinity.translate(g, xoff=ox, yoff=oy)

    label = Point(part.label_position)
    if angle:
        label = affinity.rotate(label, angle, origin=origin)
    label = affinity.translate(label, xoff=ox, yoff=oy)

    return Part(
        id=part.id,
        number=part.number,
        polygon=transform(part.polygon),
        outer_polygon=affinity.translate(rotated_outer, xoff=ox, yoff=oy),
        holes=[transform(h) for h in part.holes],
        original_number=part.original_number,
        material_group=part.material_group,
        area=part.area,
        label_position=(label.x, label.y),
    )


def _find_special_pair_placement(part_a, part_b,
                                  sheet_w: float, sheet_h: float,
                                  rotations: list[int]):
    """搜索两块特殊板头尾相接的可行放置，返回旋转角度和左下角坐标。"""
    for ra in rotations:
        pa, (wa, ha) = _rotated_part_form(part_a, ra)
        if wa > sheet_w + EPS or ha > sheet_h + EPS:
            continue
        xs_a = _candidate_offsets(pa, sheet_w, "x")
        ys_a = _candidate_offsets(pa, sheet_h, "y")
        for rb in rotations:
            pb, (wb, hb) = _rotated_part_form(part_b, rb)
            if wb > sheet_w + EPS or hb > sheet_h + EPS:
                continue
            xs_b = _candidate_offsets(pb, sheet_w, "x")
            ys_b = _candidate_offsets(pb, sheet_h, "y")
            for xa, ya in itertools.product(xs_a, ys_a):
                ca = affinity.translate(pa, xoff=xa, yoff=ya)
                ba = ca.bounds
                if (ba[0] < -EPS or ba[1] < -EPS or
                        ba[2] > sheet_w + EPS or ba[3] > sheet_h + EPS):
                    continue
                for xb, yb in itertools.product(xs_b, ys_b):
                    cb = affinity.translate(pb, xoff=xb, yoff=yb)
                    bb = cb.bounds
                    if (bb[0] < -EPS or bb[1] < -EPS or
                            bb[2] > sheet_w + EPS or bb[3] > sheet_h + EPS):
                        continue
                    if shapely.relate_pattern(ca, cb, "T********"):
                        continue
                    return ra, xa, ya, rb, xb, yb
    return None


def _first_fit_single_special(part, sheet_w, sheet_h, rotations):
    """返回单个特殊板能放入大板的最小角度。"""
    for angle in rotations:
        polygon, (w, h) = _rotated_part_form(part, angle)
        if w <= sheet_w + EPS and h <= sheet_h + EPS:
            return angle, polygon, (w, h)
    return None, None, None


def _build_special_sheet(placements, sheet_w, sheet_h, thickness):
    """placements: [(part, angle, x, y), ...]"""
    placed_parts = [
        _place_part_copy(part, angle, x, y)
        for part, angle, x, y in placements
    ]
    return Sheet(index=0, width=sheet_w, height=sheet_h,
                 thickness=thickness, parts=placed_parts)


def nest_special_parts(parts, sheet_w, sheet_h, thickness, unit, rotations):
    """特殊板两两头尾相接排板；无法配对的再单独放一张。"""
    remaining = sorted(parts, key=lambda p: (p.area, p.number), reverse=True)
    used = [False] * len(remaining)
    sheets = []

    for i, part in enumerate(remaining):
        if used[i]:
            continue
        used[i] = True
        pair = None
        for j in range(i + 1, len(remaining)):
            if used[j]:
                continue
            placement = _find_special_pair_placement(
                part, remaining[j], sheet_w, sheet_h, rotations
            )
            if placement is not None:
                pair = (j, placement)
                break

        if pair is not None:
            j, (ra, xa, ya, rb, xb, yb) = pair
            used[j] = True
            sheets.append(_build_special_sheet(
                [(part, ra, xa, ya), (remaining[j], rb, xb, yb)],
                sheet_w, sheet_h, thickness,
            ))
        else:
            angle, polygon, dims = _first_fit_single_special(
                part, sheet_w, sheet_h, rotations
            )
            if angle is None:
                raise RuntimeError(f"特殊板 {part.number} 无法放入 "
                                   f"{sheet_w:.0f}x{sheet_h:.0f} 大板")
            x = (sheet_w - dims[0]) / 2.0
            y = (sheet_h - dims[1]) / 2.0
            sheets.append(_build_special_sheet(
                [(part, angle, x, y)], sheet_w, sheet_h, thickness
            ))

    total_area = sum(p.area for p in parts)
    total_sheet_area = sum(s.total_area for s in sheets)
    return NestingResult(
        sheets=sheets, unit=unit,
        total_parts=len(parts), total_sheets=len(sheets),
        total_part_area=total_area, total_sheet_area=total_sheet_area,
    )


def combine_nesting_results(results: list) -> NestingResult:
    sheets = []
    total_parts = 0
    total_part_area = 0.0
    total_sheet_area = 0.0
    unit = results[0].unit if results else "metric"
    for result in results:
        sheets.extend(result.sheets)
        total_parts += result.total_parts
        total_part_area += result.total_part_area
        total_sheet_area += result.total_sheet_area
    for index, sheet in enumerate(sheets, start=1):
        sheet.index = index
    return NestingResult(sheets=sheets, unit=unit,
                         total_parts=total_parts,
                         total_sheets=len(sheets),
                         total_part_area=total_part_area,
                         total_sheet_area=total_sheet_area)


def _split_normal_parts_by_group(normal_parts, group_mode):
    """Split normal parts into independent nesting batches for one_set_per_sheet."""
    if group_mode != "one_set_per_sheet":
        return [normal_parts]

    groups: dict[str, list] = {}
    for part in normal_parts:
        key = part.group_id or f"__part_{id(part)}"
        groups.setdefault(key, []).append(part)
    return list(groups.values())


def _nest_one_group(parts, width_mm, height_mm, special_w, special_h,
                    effective_thickness, unit, profile, drawing_profile,
                    trials, seed, budget, quick, pairing=False):
    """Nest one material group and return result plus group stats."""
    normal_parts = []
    special_parts = []
    unfit_numbers = []
    for part in parts:
        if part_fits(part, width_mm, height_mm, profile.rotation):
            normal_parts.append(part)
        elif special_w and special_h and part_fits(
            part, special_w, special_h, profile.rotation
        ):
            special_parts.append(part)
        else:
            unfit_numbers.append(part.number)

    if unfit_numbers:
        raise RuntimeError(
            "以下零件无法放入可用大板：" + ", ".join(unfit_numbers[:20])
        )

    group_area = sum(part.area for part in parts)
    normal_area = sum(part.area for part in normal_parts)
    special_area = sum(part.area for part in special_parts)
    normal_sheet_area = width_mm * height_mm
    normal_groups = _split_normal_parts_by_group(
        normal_parts, profile.group_mode
    )
    normal_min_sheets = sum(
        math.ceil(sum(p.area for p in group_parts) / normal_sheet_area)
        if group_parts else 0
        for group_parts in normal_groups
    )
    if special_parts:
        special_sheet_area = special_w * special_h
        min_sheets = (
            normal_min_sheets
            + math.ceil(special_area / special_sheet_area)
        )
        special_pair_min_sheets = math.ceil(len(special_parts) / 2.0)
    else:
        min_sheets = math.ceil(group_area / normal_sheet_area)
        special_pair_min_sheets = None

    results = []
    for group_parts in normal_groups:
        if not group_parts:
            continue
        first_left = bool(
            profile.first_part_left_edge
            or (
                drawing_profile is not None
                and drawing_profile.first_part_left_edge
            )
        )
        group_min_sheets = (
            math.ceil(sum(p.area for p in group_parts) / normal_sheet_area)
            if group_parts else 0
        )
        if profile.uses_deepnest:
            if pairing:
                results.append(nest_parts_deepnest_paired(
                    group_parts, width_mm, height_mm, effective_thickness,
                    unit=unit.value, trials=trials, seed=seed,
                    rotations=profile.rotation,
                    arbitrary_rotation=profile.arbitrary_rotation,
                    first_part_left_edge=first_left,
                    rotation_step=15.0 if quick else 5.0))
            else:
                results.append(nest_parts_deepnest(
                    group_parts, width_mm, height_mm, effective_thickness,
                    unit=unit.value, improve_budget=budget,
                    trials=trials, seed=seed, rotations=profile.rotation,
                    arbitrary_rotation=profile.arbitrary_rotation,
                    first_part_left_edge=first_left,
                    rotation_step=15.0 if quick else 5.0,
                    configs=QUICK_CONFIGS if quick else None))
        else:
            results.append(nest_parts(
                group_parts, width_mm, height_mm, effective_thickness,
                unit=unit.value, improve_budget=budget,
                trials=trials, seed=seed, rotations=profile.rotation,
                first_part_left_edge=first_left,
                min_gap=profile.min_gap,
                min_sheets=group_min_sheets,
                configs=QUICK_CONFIGS if quick else None))
    if special_parts:
        results.append(nest_special_parts(
            special_parts, special_w, special_h, effective_thickness,
            unit=unit.value, rotations=profile.rotation))

    result = combine_nesting_results(results)
    return (result, len(normal_parts), len(special_parts), min_sheets,
            special_pair_min_sheets)


def validate_mixed_nesting(result: NestingResult, min_gap: float = 0.0) -> list:
    errors = []
    for sheet in result.sheets:
        single = NestingResult(
            sheets=[sheet], unit=result.unit,
            total_parts=len(sheet.parts), total_sheets=1,
            total_part_area=sum(p.area for p in sheet.parts),
            total_sheet_area=sheet.total_area,
        )
        errors.extend(validate_nesting(single, sheet.width, sheet.height,
                                       min_gap=min_gap))
    return errors


@dataclass
class GroupNestingOutcome:
    """Combined nesting result plus per-group statistics for reporting."""

    result: Any
    material_summary: list
    total_min_sheets: int
    total_normal_parts: int
    total_special_parts: int
    total_special_pair_min_sheets: int | None
    elapsed_seconds: float


def nest_groups(parts, groups, width_mm, height_mm, special_w, special_h,
                effective_thickness, unit, profile, drawing_profile,
                trials, seed, budget, quick, pairing=False) -> GroupNestingOutcome:
    """Nest every material group and combine into a single NestingResult."""
    t0 = time.time()
    group_results = []
    material_summary = []
    total_min_sheets = 0
    total_normal_parts = 0
    total_special_parts = 0
    total_special_pair_min_sheets = None

    for group in groups:
        group_parts = (
            [part for part in parts if part.material_group == group]
            if group is not None else parts
        )
        group_label = group or "ALL"
        group_result, normal_count, special_count, min_sheets, special_pair_min = (
            _nest_one_group(
                group_parts, width_mm, height_mm, special_w, special_h,
                effective_thickness, unit, profile, drawing_profile,
                trials, seed, budget, quick, pairing,
            )
        )
        group_results.append(group_result)
        total_min_sheets += min_sheets
        total_normal_parts += normal_count
        total_special_parts += special_count
        if special_pair_min is not None:
            total_special_pair_min_sheets = (
                (total_special_pair_min_sheets or 0) + special_pair_min
            )
        material_summary.append({
            "material_group": group_label,
            "part_count": len(group_parts),
            "sheets": group_result.total_sheets,
            "yield_rate": round(group_result.yield_rate, 2),
            "theoretical_min_sheets": min_sheets,
        })
        print(
            f"  {group_label}: {len(group_parts)} 件 -> "
            f"{group_result.total_sheets} 张大板（理论最少 {min_sheets} 张）"
        )

    result = combine_nesting_results(group_results)
    return GroupNestingOutcome(
        result=result,
        material_summary=material_summary,
        total_min_sheets=total_min_sheets,
        total_normal_parts=total_normal_parts,
        total_special_parts=total_special_parts,
        total_special_pair_min_sheets=total_special_pair_min_sheets,
        elapsed_seconds=time.time() - t0,
    )


def postprocess_result(result, profile) -> dict:
    """Run slide/align/gap post-processing in place and return metrics."""
    manufacturing_metrics = {"edge_contact_mm": 0.0, "through_cut_mm": 0.0}
    if profile.slide_to_edge or profile.align_edges or profile.min_gap > 0:
        for sheet in result.sheets:
            pp = PostProcessor(sheet.width, sheet.height)
            pp.run(
                [sheet],
                slide=profile.slide_to_edge,
                align=profile.align_edges,
                gap_mm=profile.min_gap,
            )
            metrics = pp.measure([sheet])
            manufacturing_metrics["edge_contact_mm"] += metrics["edge_contact_mm"]
            manufacturing_metrics["through_cut_mm"] += metrics["through_cut_mm"]
    return manufacturing_metrics


def build_check_payload(result, profile, drawing_profile, width_mm, height_mm,
                        special_w, special_h, effective_thickness, unit_label,
                        errors, manufacturing_metrics) -> tuple[dict, list, dict]:
    """Build the pre-DXF check payload; returns (payload, warnings, waterjet_metrics)."""
    postprocess_warnings = diagnose_postprocess(
        result.sheets,
        slide_expected=profile.slide_to_edge,
        align_expected=profile.align_edges,
        gap_mm=profile.min_gap,
    )
    waterjet_metrics = {
        "first_part_left_edge_checked": 0,
        "first_part_left_edge_failed": 0,
        "collinear_edge_pairs": 0,
    }
    if profile.uses_deepnest:
        waterjet_first_left = bool(
            profile.first_part_left_edge
            or (
                drawing_profile is not None
                and drawing_profile.first_part_left_edge
            )
        )
        waterjet_warnings, waterjet_metrics = diagnose_waterjet(
            result.sheets,
            first_part_left_edge=waterjet_first_left,
            arbitrary_rotation=profile.arbitrary_rotation,
            rotations=tuple(profile.rotation),
        )
        postprocess_warnings.extend(waterjet_warnings)
    payload = {
        "sheet_dimensions": {
            "width": width_mm,
            "height": height_mm,
            "special_width": special_w,
            "special_height": special_h,
            "thickness": effective_thickness,
            "unit": unit_label,
        },
        "geometry_validation_errors": errors,
        "geometry_validation_passed": not errors,
        "manufacturability": {
            "edge_contact_mm": round(manufacturing_metrics["edge_contact_mm"], 1),
            "through_cut_mm": round(manufacturing_metrics["through_cut_mm"], 1),
            "waterjet": waterjet_metrics,
        },
        "postprocess_warning_count": len(postprocess_warnings),
        "postprocess_warnings": postprocess_warnings,
    }
    return payload, postprocess_warnings, waterjet_metrics


def build_report(result, profile, width_mm, height_mm, special_w, special_h,
                 effective_thickness, unit_label, total_min_sheets,
                 total_special_pair_min_sheets, material_summary, elapsed,
                 errors, manufacturing_metrics, trials, seed, budget,
                 skip_unnumbered) -> dict:
    """Build the final nesting report payload."""
    return {
        "sheet_dimensions": {
            "width": width_mm, "height": height_mm,
            "special_width": special_w, "special_height": special_h,
            "thickness": effective_thickness, "unit": unit_label,
        },
        "total_sheets": result.total_sheets,
        "total_parts": result.total_parts,
        "total_part_area": round(result.total_part_area, 1),
        "total_sheet_area": result.total_sheet_area,
        "yield_rate": round(result.yield_rate, 2),
        "theoretical_min_sheets": total_min_sheets,
        "special_pair_min_sheets": total_special_pair_min_sheets,
        "material_summary": material_summary,
        "elapsed_seconds": round(elapsed, 1),
        "validation_errors": len(errors),
        "manufacturability": {
            "edge_contact_mm": round(manufacturing_metrics["edge_contact_mm"], 1),
            "through_cut_mm": round(manufacturing_metrics["through_cut_mm"], 1),
        },
        "profile": {
            "processing_class": profile.processing_class,
            "arbitrary_rotation": profile.arbitrary_rotation,
            "rotation": profile.rotation,
            "min_gap": profile.min_gap,
            "group_mode": profile.group_mode,
            "slide_to_edge": profile.slide_to_edge,
            "align_edges": profile.align_edges,
        },
        "parameters": {
            "trials": trials, "seed": seed, "budget_s": budget,
            "skip_unnumbered": skip_unnumbered,
        },
        "sheets": [
            {
                "index": s.index,
                "width": s.width,
                "height": s.height,
                "part_count": len(s.parts),
                "parts": [p.number for p in s.parts],
            }
            for s in result.sheets
        ],
    }
