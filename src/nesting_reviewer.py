"""Post-nesting local reduction reviewer.

This module reviews an existing ``NestingResult`` and tries to remove a few
low-occupancy sheets by moving their parts into free space on other sheets.
It is deliberately local: it does not re-run the global nesting optimizer.

The review is geometry-first.  Area-only checks are not sufficient because a
sheet can have enough free area but no free region with the right shape.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import math
import re
import time
from typing import Iterable

import ezdxf
from shapely.affinity import rotate, translate
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

from src.models import NestingResult, Part, Sheet


EPS = 1e-3
_MATERIAL_PREFIX = re.compile(r"^(?P<prefix>\d{2}B)")


@dataclass
class ReductionCandidate:
    """One local sheet reduction that can be applied to a nesting result."""

    removed_sheet_index: int
    moved_parts: list[str]
    target_sheets: list[int]
    result: NestingResult

    @property
    def saved_sheets(self) -> int:
        return 1


def _as_polygons(geometry) -> list[Polygon]:
    """Return a flat list of polygons from a Shapely geometry."""
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [p for p in geometry.geoms if not p.is_empty]


def _sheet_rect(sheet: Sheet) -> Polygon:
    return Polygon([
        (0, 0),
        (sheet.width, 0),
        (sheet.width, sheet.height),
        (0, sheet.height),
        (0, 0),
    ])


def free_polygons_for_sheet(sheet: Sheet, clearance: float = 0.0) -> list[Polygon]:
    """Return the actual unused area of a sheet as polygons.

    ``clearance`` 会围绕已有零件外扩后再挖空，保证新放入的零件与它们保持
    至少该间距（桥梁加工的锯缝/间距约束）。
    """
    solids = [
        part.outer_polygon.buffer(clearance) if clearance > EPS else part.outer_polygon
        for part in sheet.parts
    ]
    occupied = unary_union(solids)
    return _as_polygons(_sheet_rect(sheet).difference(occupied))


def _sheet_material_groups(sheet: Sheet) -> set[str | None]:
    return {part.material_group for part in sheet.parts}


def _common_material_group(sheet: Sheet) -> str | None:
    groups = _sheet_material_groups(sheet)
    return next(iter(groups)) if len(groups) == 1 else None


def _compatible_target(donor: Sheet, target: Sheet, profile) -> bool:
    if target.width < donor.width - EPS:
        return False
    if target.height < donor.height - EPS:
        return False
    if profile is not None and profile.group_mode == "one_set_per_sheet":
        return False
    donor_group = _common_material_group(donor)
    target_groups = _sheet_material_groups(target)
    if donor_group is not None and target_groups and donor_group not in target_groups:
        return False
    return True


def _candidate_offsets(free_poly: Polygon, width: float, height: float) -> list[tuple[float, float]]:
    minx, miny, maxx, maxy = free_poly.bounds
    if width > maxx - minx + EPS or height > maxy - miny + EPS:
        return []
    xs = {minx, maxx - width}
    ys = {miny, maxy - height}
    for x, y in free_poly.exterior.coords:
        for candidate_x in (x, x - width):
            if minx - EPS <= candidate_x <= maxx - width + EPS:
                xs.add(candidate_x)
        for candidate_y in (y, y - height):
            if miny - EPS <= candidate_y <= maxy - height + EPS:
                ys.add(candidate_y)
    return [
        (x, y)
        for x in sorted(xs)
        for y in sorted(ys)
    ]


def _transform_part(part: Part, angle: float, x: float, y: float) -> Part:
    outer = part.outer_polygon
    if angle:
        outer = rotate(outer, angle, origin=(0, 0))
    min_x, min_y, _, _ = outer.bounds
    outer = translate(outer, xoff=x - min_x, yoff=y - min_y)

    holes: list[Polygon] = []
    for hole in part.holes:
        transformed_hole = hole
        if angle:
            transformed_hole = rotate(transformed_hole, angle, origin=(0, 0))
        transformed_hole = translate(
            transformed_hole,
            xoff=x - min_x,
            yoff=y - min_y,
        )
        if not transformed_hole.is_empty:
            holes.append(transformed_hole)

    if holes:
        polygon = outer.difference(unary_union(holes))
    else:
        polygon = outer

    return replace(
        part,
        polygon=polygon,
        outer_polygon=outer,
        holes=holes,
        area=polygon.area,
        label_position=(outer.centroid.x, outer.centroid.y),
    )


def _placements_for_part(
    part: Part,
    free_polys: list[Polygon],
    rotations: Iterable[int],
    limit: int | None = None,
) -> list[Part]:
    placements: list[Part] = []
    # 预准备每个空闲区一次：buffer/covers 结果在多次候选点测试间复用，
    # 避免对每个候选点重复构建 buffer（这是回看搜索的主要热点）。
    prepared_polys = [
        (free_poly, prep(free_poly.buffer(0.01)))
        for free_poly in free_polys
    ]
    for angle in rotations:
        rotated = part.outer_polygon
        if angle:
            rotated = rotate(rotated, angle, origin=(0, 0))
        minx, miny, maxx, maxy = rotated.bounds
        width = maxx - minx
        height = maxy - miny
        for free_poly, prepared in prepared_polys:
            for x, y in _candidate_offsets(free_poly, width, height):
                translated = translate(
                    rotated,
                    xoff=x - minx,
                    yoff=y - miny,
                )
                if prepared.covers(translated):
                    placements.append(_transform_part(part, angle, x, y))
                    if limit is not None and len(placements) >= limit:
                        return placements
    return placements


@dataclass
class _TargetState:
    sheet: Sheet
    free_polys: list[Polygon]
    clearance: float = 0.0

    def with_part(self, part: Part) -> "_TargetState":
        sheet = replace(self.sheet, parts=[*self.sheet.parts, part])
        solid = (
            part.outer_polygon.buffer(self.clearance)
            if self.clearance > EPS else part.outer_polygon
        )
        free_polys: list[Polygon] = []
        for free_poly in self.free_polys:
            free_polys.extend(_as_polygons(free_poly.difference(solid)))
        return _TargetState(sheet, free_polys, self.clearance)


def _try_place_all(
    parts: list[Part],
    states: list[_TargetState],
    rotations: list[int],
    *,
    nodes_left: list[int],
    deadline: float | None = None,
    placement_limit: int | None = 40,
) -> list[_TargetState] | None:
    if not parts:
        return states
    if nodes_left[0] <= 0:
        return None
    if deadline is not None and time.monotonic() >= deadline:
        return None

    part = parts[0]
    for state_index, state in enumerate(states):
        candidates = _placements_for_part(
            part, state.free_polys, rotations, limit=placement_limit,
        )
        # Prefer the first valid placements, but still search alternatives.
        for candidate in candidates:
            nodes_left[0] -= 1
            if deadline is not None and time.monotonic() >= deadline:
                return None
            next_states = list(states)
            next_states[state_index] = state.with_part(candidate)
            completed = _try_place_all(
                parts[1:],
                next_states,
                rotations,
                nodes_left=nodes_left,
                deadline=deadline,
                placement_limit=placement_limit,
            )
            if completed is not None:
                return completed
            if nodes_left[0] <= 0:
                return None
    return None


MAX_REVIEW_ROTATIONS = 8


def _edge_rotation_candidates(parts: list[Part]) -> list[float]:
    """回看允许的角度：保留零件原朝向，再加少量最长边方向。

    水刀允许任意角度，但回看时不能穷举 1°。这里只取"原朝向 + 该板零件
    最长边方向"的少量角度，既给贴合留出余地，又把搜索量控制在可控范围。
    """
    edges: list[tuple[float, float]] = []
    for part in parts:
        coords = list(part.outer_polygon.exterior.coords)
        for start, end in zip(coords, coords[1:]):
            dx = float(end[0] - start[0])
            dy = float(end[1] - start[1])
            length = math.hypot(dx, dy)
            if length < EPS:
                continue
            angle = round(math.degrees(math.atan2(dy, dx)) % 180.0, 2)
            edges.append((length, angle))
    angles: list[float] = [0.0]
    for _, angle in sorted(edges, key=lambda item: -item[0]):
        if len(angles) >= MAX_REVIEW_ROTATIONS:
            break
        if all(abs(angle - existing) > 0.5 for existing in angles):
            angles.append(angle)
    return angles


def _try_reduce_donor(
    donor: Sheet,
    result: NestingResult,
    profile,
    clearance: float = 0.0,
    deadline: float | None = None,
    max_nodes: int = 20000,
    node_counter: list[int] | None = None,
    placement_limit: int = 40,
) -> ReductionCandidate | None:
    if profile is not None and profile.arbitrary_rotation:
        rotations = _edge_rotation_candidates(donor.parts)
    else:
        rotations = [0, 90, 180, 270] if profile is None else list(profile.rotation)

    states = [
        _TargetState(
            sheet=replace(sheet),
            free_polys=free_polygons_for_sheet(sheet, clearance),
            clearance=clearance,
        )
        for sheet in result.sheets
        if sheet.index != donor.index and _compatible_target(donor, sheet, profile)
    ]
    if not states:
        return None

    parts = sorted(donor.parts, key=lambda part: part.area, reverse=True)
    nodes_left = [max_nodes]
    final_states = _try_place_all(
        parts, states, rotations,
        nodes_left=nodes_left, deadline=deadline, placement_limit=placement_limit,
    )
    if node_counter is not None:
        node_counter.append(max_nodes - nodes_left[0])
    if final_states is None:
        return None

    kept_sheets = [sheet for sheet in result.sheets if sheet.index != donor.index]
    original_by_index = {sheet.index: sheet for sheet in result.sheets}
    final_state_by_index = {state.sheet.index: state.sheet for state in final_states}
    moved_targets = {
        index: sheet
        for index, sheet in final_state_by_index.items()
        if len(sheet.parts) != len(original_by_index[index].parts)
    }
    final_sheets = [
        final_state_by_index.get(sheet.index, sheet)
        for sheet in kept_sheets
    ]
    for index, sheet in enumerate(final_sheets, start=1):
        sheet.index = index

    new_result = NestingResult(
        sheets=final_sheets,
        unit=result.unit,
        total_parts=result.total_parts,
        total_sheets=len(final_sheets),
        total_part_area=sum(part.area for sheet in final_sheets for part in sheet.parts),
        total_sheet_area=sum(sheet.total_area for sheet in final_sheets),
    )
    return ReductionCandidate(
        removed_sheet_index=donor.index,
        moved_parts=[part.number for part in donor.parts],
        target_sheets=sorted(moved_targets),
        result=new_result,
    )


def find_reductions(
    result: NestingResult,
    *,
    profile=None,
    max_reductions: int = 3,
    max_donor_parts: int | None = None,
    max_donor_fill_ratio: float | None = None,
    max_donor_attempts: int | None = None,
    min_gap: float = 0.0,
    time_budget_s: float | None = None,
    per_donor_time_s: float | None = None,
) -> tuple[list[ReductionCandidate], NestingResult]:
    """Find a sequence of local sheet reductions.

    Returns the list of applied candidates and the final reduced result.
    The input ``result`` is not modified.
    """
    current = result
    candidates: list[ReductionCandidate] = []
    clearance = max(float(min_gap), 0.0)
    deadline = (
        time.monotonic() + float(time_budget_s)
        if time_budget_s is not None else None
    )
    for _ in range(max_reductions):
        if deadline is not None and time.monotonic() >= deadline:
            break
        donors = sorted(
            current.sheets,
            key=lambda sheet: (len(sheet.parts), sum(part.area for part in sheet.parts)),
        )
        candidate = None
        attempts = 0
        for donor in donors:
            if max_donor_parts is not None and len(donor.parts) > max_donor_parts:
                continue
            if max_donor_fill_ratio is not None:
                fill_ratio = sum(part.area for part in donor.parts) / max(donor.total_area, EPS)
                if fill_ratio > max_donor_fill_ratio:
                    continue
            if max_donor_attempts is not None and attempts >= max_donor_attempts:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            attempts += 1
            donor_deadline = deadline
            if per_donor_time_s is not None:
                single = time.monotonic() + float(per_donor_time_s)
                donor_deadline = single if donor_deadline is None else min(donor_deadline, single)
            candidate = _try_reduce_donor(
                donor, current, profile, clearance, deadline=donor_deadline,
            )
            if candidate is not None:
                break
        if candidate is None:
            break
        candidates.append(candidate)
        current = candidate.result
    return candidates, current


def load_nested_dxf(path: str | Path, *, thickness: float = 20.0) -> NestingResult:
    """Read a DXF produced by ``write_nested_dxf`` back into a NestingResult."""
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    borders: list[dict] = []
    current_border: dict | None = None
    outer_entities = []
    hole_entities = []
    number_texts = []

    for entity in msp:
        layer = entity.dxf.layer
        if layer == "SHEET_BORDER":
            if entity.dxftype() == "LWPOLYLINE":
                points = [(float(x), float(y)) for x, y in entity.get_points("xy")]
                if len(points) < 3:
                    continue
                min_x = min(x for x, _ in points)
                min_y = min(y for _, y in points)
                max_x = max(x for x, _ in points)
                max_y = max(y for _, y in points)
                current_border = {
                    "polygon": Polygon(points),
                    "ox": min_x,
                    "oy": min_y,
                    "width": max_x - min_x,
                    "height": max_y - min_y,
                    "index": None,
                }
                borders.append(current_border)
            elif entity.dxftype() == "TEXT" and current_border is not None:
                text = entity.dxf.text
                if text.startswith("Sheet_"):
                    try:
                        current_border["index"] = int(text.split("_", 1)[1])
                    except ValueError:
                        pass
        elif layer == "OUTER" and entity.dxftype() == "LWPOLYLINE":
            outer_entities.append(entity)
        elif layer == "HOLES" and entity.dxftype() == "LWPOLYLINE":
            hole_entities.append(entity)
        elif layer == "NUMBERS" and entity.dxftype() == "TEXT":
            number_texts.append(entity)

    for index, border in enumerate(borders, start=1):
        if border["index"] is None:
            border["index"] = index
    borders.sort(key=lambda border: border["index"])

    sheets: list[Sheet] = []
    for border in borders:
        sheets.append(Sheet(
            index=border["index"],
            width=border["width"],
            height=border["height"],
            thickness=thickness,
            parts=[],
        ))

    def local_polygon(entity, border):
        points = [
            (float(x) - border["ox"], float(y) - border["oy"])
            for x, y in entity.get_points("xy")
        ]
        if len(points) < 3:
            return None
        poly = Polygon(points)
        return poly if poly.is_valid and not poly.is_empty else None

    part_id = 0
    for border, sheet in zip(borders, sheets):
        border_poly = border["polygon"]
        for entity in outer_entities:
            raw_points = [
                (float(x), float(y))
                for x, y in entity.get_points("xy")
            ]
            if len(raw_points) < 3:
                continue
            global_poly = Polygon(raw_points)
            if not global_poly.is_valid or global_poly.is_empty:
                continue
            if not border_poly.contains(global_poly.centroid):
                continue
            poly = Polygon([
                (x - border["ox"], y - border["oy"])
                for x, y in raw_points
            ])
            if poly is None:
                continue
            part_id += 1
            sheet.parts.append(Part(
                id=part_id,
                number="",
                polygon=poly,
                outer_polygon=poly,
                holes=[],
                area=poly.area,
                label_position=(poly.centroid.x, poly.centroid.y),
            ))

    for border, sheet in zip(borders, sheets):
        for entity in hole_entities:
            poly = local_polygon(entity, border)
            if poly is None:
                continue
            for part in sheet.parts:
                if part.outer_polygon.contains(poly.centroid):
                    part.holes.append(poly)
                    break
        for part in sheet.parts:
            if part.holes:
                part.polygon = part.outer_polygon.difference(unary_union(part.holes))
                part.area = part.polygon.area

    for border, sheet in zip(borders, sheets):
        for entity in number_texts:
            text_point = Point(
                float(entity.dxf.insert.x),
                float(entity.dxf.insert.y),
            )
            if not border["polygon"].contains(text_point):
                continue
            point = Point(
                float(entity.dxf.insert.x) - border["ox"],
                float(entity.dxf.insert.y) - border["oy"],
            )
            # Assign to the closest part in this sheet.
            if not sheet.parts:
                continue
            target = min(
                sheet.parts,
                key=lambda part: part.outer_polygon.distance(point),
            )
            if target.number:
                continue
            target.number = entity.dxf.text
            match = _MATERIAL_PREFIX.match(target.number)
            if match:
                target.material_group = match.group("prefix")

    for sheet in sheets:
        sheet.parts = [part for part in sheet.parts if part.number]

    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(len(sheet.parts) for sheet in sheets),
        total_sheets=len(sheets),
        total_part_area=sum(part.area for sheet in sheets for part in sheet.parts),
        total_sheet_area=sum(sheet.total_area for sheet in sheets),
    )
