"""Deterministic same-sheet-count repair for circular-saw collisions."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Iterable

import ezdxf
from shapely import affinity
from shapely.geometry import Point

from src.layout_adjustment import (
    NestedPartGroup,
    NestedSheetLayout,
    _read_nested_layout,
    _validate_sheet,
)
from src.saw_process import diagnose_saw_access


@dataclass
class SawRepairResult:
    output_path: Path
    diagnosis: dict[str, Any]
    updated_sheets: list[dict[str, Any]] = field(default_factory=list)
    unresolved_sheets: list[dict[str, Any]] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        return bool(self.updated_sheets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "improved": self.improved,
            "updated_sheets": self.updated_sheets,
            "unresolved_sheets": self.unresolved_sheets,
            "diagnosis": self.diagnosis,
            "policy": {
                "same_sheet_count": True,
                "translation_only": True,
                "geometry_number_holes_rotation_unchanged": True,
                "unsafe_unresolved_sheet_is_not_modified": True,
            },
        }


@dataclass
class InMemorySawRepairSummary:
    before_insufficient: int
    after_insufficient: int
    updated_sheets: list[dict[str, Any]] = field(default_factory=list)
    unresolved_sheets: list[dict[str, Any]] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        return bool(self.updated_sheets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "improved": self.improved,
            "before_insufficient": self.before_insufficient,
            "after_insufficient": self.after_insufficient,
            "updated_sheets": self.updated_sheets,
            "unresolved_sheets": self.unresolved_sheets,
            "policy": {
                "same_sheet_count": True,
                "translation_only": True,
                "geometry_number_holes_rotation_unchanged": True,
            },
        }


def repair_nesting_result_saw_clearance(
    result,
    *,
    clearance_mm: float,
    min_gap_mm: float = 0.0,
    max_moves_per_sheet: int = 12,
) -> InMemorySawRepairSummary:
    """Repair a materialized NestingResult in place at fixed sheet count."""
    before_total = 0
    after_total = 0
    updated = []
    unresolved = []
    for sheet in result.sheets:
        wrappers = []
        for part in sheet.parts:
            wrappers.append(
                NestedPartGroup(
                    group_name=f"{sheet.index}:{part.number}",
                    number=part.number,
                    entities=[],
                    outer_entities=[],
                    hole_entities=[],
                    number_entities=[],
                    outer_polygon=part.outer_polygon,
                    holes=list(part.holes),
                )
            )
        layout = NestedSheetLayout(
            index=sheet.index,
            width=sheet.width,
            height=sheet.height,
            origin_x=0.0,
            origin_y=0.0,
            parts=wrappers,
        )
        before = _sheet_summary(layout, clearance_mm)
        before_total += before["points_insufficient"]
        if before["points_insufficient"] == 0:
            continue
        repaired, moves = _repair_sheet(
            layout,
            clearance_mm,
            min_gap_mm=min_gap_mm,
            max_moves=max(0, max_moves_per_sheet),
        )
        resolved_layout = NestedSheetLayout(
            index=sheet.index,
            width=sheet.width,
            height=sheet.height,
            origin_x=0.0,
            origin_y=0.0,
            parts=repaired,
        )
        after = _sheet_summary(resolved_layout, clearance_mm)
        after_total += after["points_insufficient"]
        if not moves or after["points_insufficient"] >= before["points_insufficient"]:
            unresolved.append(before)
            continue
        original_by_group = {
            wrapper.group_name: (wrapper, part)
            for wrapper, part in zip(wrappers, sheet.parts)
        }
        for repaired_part in repaired:
            original_wrapper, original_part = original_by_group[
                repaired_part.group_name
            ]
            dx = repaired_part.bounds[0] - original_wrapper.bounds[0]
            dy = repaired_part.bounds[1] - original_wrapper.bounds[1]
            if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
                continue
            _translate_part(original_part, dx, dy)
        sheet.remaining_area = sheet.total_area - sheet.used_area
        updated.append({
            "sheet": sheet.index,
            "before": before,
            "after": after,
            "moves": moves,
        })
    return InMemorySawRepairSummary(
        before_insufficient=before_total,
        after_insufficient=after_total,
        updated_sheets=updated,
        unresolved_sheets=unresolved,
    )


def repair_nested_saw_clearance(
    nested_dxf: str | Path,
    output_dxf: str | Path,
    *,
    diameter_mm: float,
    thickness_mm: float,
    plunge_extra_mm: float = 2.0,
    safety_margin_mm: float = 0.0,
    min_gap_mm: float = 0.0,
    max_moves_per_sheet: int = 12,
) -> SawRepairResult:
    """Repair local saw collisions without changing sheet count."""
    from src.saw_audit import audit_nested_saw_clearance
    from src.saw_process import calculate_saw_clearance

    source = Path(nested_dxf).expanduser().resolve()
    output = Path(output_dxf).expanduser().resolve()
    if source == output:
        raise ValueError("repair output must differ from the input DXF")
    if not source.is_file():
        raise FileNotFoundError(source)
    clearance = calculate_saw_clearance(
        diameter_mm,
        thickness_mm,
        plunge_extra_mm=plunge_extra_mm,
        safety_margin_mm=safety_margin_mm,
    )
    doc = ezdxf.readfile(str(source))
    sheets, _groups = _read_nested_layout(doc)
    updated: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for sheet in sheets:
        before = _sheet_summary(sheet, clearance.clearance_mm)
        if before["points_insufficient"] == 0:
            continue
        repaired_parts, moves = _repair_sheet(
            sheet,
            clearance.clearance_mm,
            min_gap_mm=min_gap_mm,
            max_moves=max(0, max_moves_per_sheet),
        )
        after_sheet = NestedSheetLayout(
            index=sheet.index,
            width=sheet.width,
            height=sheet.height,
            origin_x=sheet.origin_x,
            origin_y=sheet.origin_y,
            parts=repaired_parts,
        )
        after = _sheet_summary(
            after_sheet,
            clearance.clearance_mm,
        )
        if after["points_insufficient"] >= before["points_insufficient"]:
            unresolved.append(before)
            continue
        if not moves:
            unresolved.append(before)
            continue

        original_by_group = {
            part.group_name: part for part in sheet.parts
        }
        for part in repaired_parts:
            original = original_by_group[part.group_name]
            dx = part.bounds[0] - original.bounds[0]
            dy = part.bounds[1] - original.bounds[1]
            if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
                continue
            for entity in original.entities:
                entity.translate(dx, dy, 0)
        updated.append({
            "sheet": sheet.index,
            "before": before,
            "after": after,
            "moves": moves,
        })

    output.parent.mkdir(parents=True, exist_ok=True)
    previous_fixed_meta = ezdxf.options.write_fixed_meta_data_for_testing
    ezdxf.options.write_fixed_meta_data_for_testing = True
    try:
        doc.saveas(str(output))
    finally:
        ezdxf.options.write_fixed_meta_data_for_testing = previous_fixed_meta

    diagnosis = audit_nested_saw_clearance(
        output,
        diameter_mm=diameter_mm,
        thickness_mm=thickness_mm,
        plunge_extra_mm=plunge_extra_mm,
        safety_margin_mm=safety_margin_mm,
    )
    return SawRepairResult(
        output_path=output,
        diagnosis=diagnosis,
        updated_sheets=updated,
        unresolved_sheets=unresolved,
    )


def _repair_sheet(
    sheet: NestedSheetLayout,
    clearance_mm: float,
    *,
    min_gap_mm: float = 0.0,
    max_moves: int,
) -> tuple[list[NestedPartGroup], list[dict[str, Any]]]:
    current = list(sheet.parts)
    current_summary = _summary_for_parts(
        sheet,
        current,
        clearance_mm,
    )
    moves: list[dict[str, Any]] = []
    for _iteration in range(max_moves):
        if current_summary["points_insufficient"] == 0:
            break
        risks = _risk_pairs(current, clearance_mm)
        best = None
        for current_index, neighbor_index, risk in risks:
            part = current[current_index]
            neighbor = current[neighbor_index]
            for dx, dy in _candidate_offsets(
                part,
                neighbor,
                clearance_mm=clearance_mm,
                actual_clearance=risk["actual_clearance_mm"],
                sheet_width=sheet.width,
                sheet_height=sheet.height,
            ):
                candidate = list(current)
                candidate[current_index] = part.translated(dx, dy)
                if _validate_sheet(
                    NestedSheetLayout(
                        index=sheet.index,
                        width=sheet.width,
                        height=sheet.height,
                        origin_x=sheet.origin_x,
                        origin_y=sheet.origin_y,
                        parts=candidate,
                    ),
                    min_gap_mm=min_gap_mm,
                ):
                    continue
                summary = _summary_for_parts(
                    sheet,
                    candidate,
                    clearance_mm,
                )
                key = _summary_key(summary)
                if key >= _summary_key(current_summary):
                    continue
                move_key = (
                    key,
                    round(dx * dx + dy * dy, 3),
                    current_index,
                    dx,
                    dy,
                )
                if best is None or move_key < best[0]:
                    best = (
                        move_key,
                        current_index,
                        dx,
                        dy,
                        candidate,
                        summary,
                    )
        if best is None:
            break
        _key, index, dx, dy, current, current_summary = best
        moves.append({
            "part": current[index].number,
            "dx_mm": round(dx, 3),
            "dy_mm": round(dy, 3),
        })
    return current, moves


def _risk_pairs(
    parts: list[NestedPartGroup],
    clearance_mm: float,
) -> list[tuple[int, int, dict[str, float]]]:
    risks = []
    for index, part in enumerate(parts):
        polygon = part.outer_polygon
        coords = list(polygon.exterior.coords)
        if len(coords) > 1 and coords[0] == coords[-1]:
            coords.pop()
        if len(coords) < 3:
            continue
        clockwise = _signed_area(coords) < 0
        for vertex_index, point in enumerate(coords):
            previous = Point(coords[(vertex_index - 1) % len(coords)])
            current = Point(point)
            following = Point(coords[(vertex_index + 1) % len(coords)])
            if not _is_reflex(previous, current, following, clockwise):
                continue
            distances = sorted(
                (
                    float(current.distance(other.outer_polygon)),
                    other_index,
                )
                for other_index, other in enumerate(parts)
                if other_index != index
            )
            if not distances:
                continue
            actual, neighbor_index = distances[0]
            if actual + 1e-7 >= clearance_mm:
                continue
            risks.append(
                (
                    index,
                    neighbor_index,
                    {
                        "actual_clearance_mm": actual,
                        "x": current.x,
                        "y": current.y,
                    },
                )
            )
    return risks


def _candidate_offsets(
    part: NestedPartGroup,
    neighbor: NestedPartGroup,
    *,
    clearance_mm: float,
    actual_clearance: float,
    sheet_width: float,
    sheet_height: float,
) -> Iterable[tuple[float, float]]:
    need = max(1.0, clearance_mm - actual_clearance + 2.0)
    part_centroid = part.outer_polygon.centroid
    neighbor_centroid = neighbor.outer_polygon.centroid
    vx = part_centroid.x - neighbor_centroid.x
    vy = part_centroid.y - neighbor_centroid.y
    length = math.hypot(vx, vy)
    directions = [
        (1.0, 0.0),
        (-1.0, 0.0),
        (0.0, 1.0),
        (0.0, -1.0),
        (0.70710678, 0.70710678),
        (0.70710678, -0.70710678),
        (-0.70710678, 0.70710678),
        (-0.70710678, -0.70710678),
    ]
    if length > 1e-7:
        directions.insert(0, (vx / length, vy / length))
    distances = sorted({
        need,
        need * 1.5,
        need * 2.0,
        10.0,
        25.0,
        50.0,
        100.0,
    })

    candidates = []
    for direction_x, direction_y in directions:
        for distance in distances:
            dx = direction_x * distance
            dy = direction_y * distance
            min_x, min_y, max_x, max_y = part.bounds
            if (
                min_x + dx < -1e-6
                or min_y + dy < -1e-6
                or max_x + dx > sheet_width + 1e-6
                or max_y + dy > sheet_height + 1e-6
            ):
                continue
            candidates.append((dx, dy))
    candidates.extend(_edge_candidates(part, sheet_width, sheet_height))
    return sorted(set(candidates))


def _edge_candidates(
    part: NestedPartGroup,
    sheet_width: float,
    sheet_height: float,
) -> list[tuple[float, float]]:
    min_x, min_y, max_x, max_y = part.bounds
    return [
        (-min_x, 0.0),
        (sheet_width - max_x, 0.0),
        (0.0, -min_y),
        (0.0, sheet_height - max_y),
    ]


def _summary_for_parts(
    sheet: NestedSheetLayout,
    parts: list[NestedPartGroup],
    clearance_mm: float,
) -> dict[str, Any]:
    return _sheet_summary(
        NestedSheetLayout(
            index=sheet.index,
            width=sheet.width,
            height=sheet.height,
            origin_x=sheet.origin_x,
            origin_y=sheet.origin_y,
            parts=parts,
        ),
        clearance_mm,
    )


def _sheet_summary(
    sheet: NestedSheetLayout,
    clearance_mm: float,
) -> dict[str, Any]:
    summary = diagnose_saw_access([sheet], clearance_mm)
    return {
        "sheet": sheet.index,
        "points_checked": summary.points_checked,
        "points_insufficient": summary.points_insufficient,
        "total_deficit_mm": round(summary.total_deficit_mm, 3),
        "min_clearance_mm": (
            round(summary.min_clearance_mm, 3)
            if summary.min_clearance_mm is not None
            else None
        ),
    }


def _summary_key(summary: dict[str, Any]) -> tuple[float, float, float]:
    minimum = summary["min_clearance_mm"]
    return (
        float(summary["points_insufficient"]),
        float(summary["total_deficit_mm"]),
        -float(minimum) if minimum is not None else 0.0,
    )


def _translate_part(part, dx: float, dy: float) -> None:
    part.outer_polygon = affinity.translate(
        part.outer_polygon,
        xoff=dx,
        yoff=dy,
    )
    part.polygon = affinity.translate(
        part.polygon,
        xoff=dx,
        yoff=dy,
    )
    part.holes = [
        affinity.translate(hole, xoff=dx, yoff=dy)
        for hole in part.holes
    ]
    label_x, label_y = part.label_position
    part.label_position = (label_x + dx, label_y + dy)


def _is_reflex(
    previous: Point,
    current: Point,
    following: Point,
    clockwise: bool,
) -> bool:
    ax = current.x - previous.x
    ay = current.y - previous.y
    bx = following.x - current.x
    by = following.y - current.y
    cross = ax * by - ay * bx
    length = math.hypot(ax, ay) * math.hypot(bx, by)
    if length <= 1e-7:
        return False
    turn = math.degrees(
        math.atan2(abs(cross), ax * bx + ay * by)
    )
    if turn < 60.0:
        return False
    normalized = cross / length
    return normalized > 1e-7 if clockwise else normalized < -1e-7


def _signed_area(coords: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        coords[index][0] * coords[(index + 1) % len(coords)][1]
        - coords[(index + 1) % len(coords)][0] * coords[index][1]
        for index in range(len(coords))
    )
