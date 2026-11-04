"""Learn reusable bridge-cutting board recipes from a reference DXF.

The reference drawing is treated as a corpus of already arranged boards.  A
recipe keeps only the board size and each component's shape family and local
pose.  It deliberately does not retain source coordinates or part identity, so
the recipe can be applied to another inventory containing the same shapes.

This is a warm-start layer, not a placement invariant: an imported recipe is
accepted only when every component can be reproduced under its own allowed
rotation and the resulting board passes boundary and overlap checks.  Parts
that cannot be reused are returned to the normal nesting engine.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Iterable

import ezdxf
from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from src.models import NestingResult, Part
from src.nesting import _Placement, _materialize, nest_parts


@dataclass(frozen=True)
class TemplatePart:
    shape_family: tuple[int, int, int, float]
    polygon: Polygon
    area: float


@dataclass(frozen=True)
class TemplateStructure:
    """A relative-placement method learned from one or more board slots.

    Structures describe why a collection of parts works together.  They are
    deliberately generic: a complementary pair, a repeated band or a core
    group with optional filler.  The current instantiation still validates the
    complete relative geometry before accepting a board.
    """

    kind: str
    part_indices: tuple[int, ...]
    fill_ratio: float
    relation: str


@dataclass(frozen=True)
class TemplateBoard:
    index: int
    width: float
    height: float
    parts: tuple[TemplatePart, ...]
    fill_ratio: float
    source_handle: str
    structures: tuple[TemplateStructure, ...] = ()
    optional_part_indices: tuple[int, ...] = ()


@dataclass
class TemplateReuseResult:
    sheets: list[list[_Placement]]
    remaining_parts: list[Part]
    used_templates: list[TemplateBoard]
    stats: dict


def shape_family(polygon: Polygon) -> tuple[int, int, int, float]:
    """Coarse, rotation-independent family key for matching equal geometries."""
    min_x, min_y, max_x, max_y = polygon.bounds
    width = max_x - min_x
    height = max_y - min_y
    box_area = width * height
    return (
        int(round(polygon.area / 100.0) * 100),
        int(round(min(width, height) / 5.0) * 5),
        int(round(max(width, height) / 5.0) * 5),
        round(polygon.area / box_area if box_area > 0 else 0.0, 1),
    )


def _normalized(polygon: Polygon) -> Polygon:
    min_x, min_y, _, _ = polygon.bounds
    return affinity.translate(polygon, xoff=-min_x, yoff=-min_y)


def _rotation_options(part: Part, fallback: Iterable[float]) -> tuple[float, ...]:
    allowed = getattr(part, "allowed_rotations", None)
    if allowed is None:
        allowed = fallback
    return tuple(float(value) for value in allowed)


def _is_sheet_rectangle(
    polygon: Polygon,
    width: float,
    height: float,
    tolerance: float,
) -> bool:
    min_x, min_y, max_x, max_y = polygon.bounds
    actual_width = max_x - min_x
    actual_height = max_y - min_y
    return (
        abs(actual_width - width) <= tolerance
        and abs(actual_height - height) <= tolerance
    )


def _bounds_fill_ratio(polygons: Iterable[Polygon]) -> float:
    geometries = [polygon for polygon in polygons if not polygon.is_empty]
    if not geometries:
        return 0.0
    union = unary_union(geometries)
    min_x, min_y, max_x, max_y = union.bounds
    envelope = box(min_x, min_y, max_x, max_y)
    return union.area / envelope.area if envelope.area > 0 else 0.0


def detect_complementary_pairs(
    parts: tuple[TemplatePart, ...],
    *,
    fill_tolerance: float = 0.98,
) -> tuple[TemplateStructure, ...]:
    """Find pairs that combine into an almost complete bounding rectangle."""
    structures = []
    for left in range(len(parts)):
        for right in range(left + 1, len(parts)):
            first = parts[left]
            second = parts[right]
            if first.polygon.intersection(second.polygon).area > 1e-6:
                continue
            fill = _bounds_fill_ratio((first.polygon, second.polygon))
            if fill + 1e-9 < fill_tolerance:
                continue
            structures.append(
                TemplateStructure(
                    kind="complementary_pair",
                    part_indices=(left, right),
                    fill_ratio=round(fill, 6),
                    relation="preserve_relative_pose",
                )
            )
    return tuple(structures)


def _aligned_bands(
    parts: tuple[TemplatePart, ...],
    *,
    axis: str,
    alignment_tolerance: float = 2.0,
) -> tuple[tuple[int, ...], ...]:
    groups: list[tuple[float, float, list[int]]] = []
    for index, part in enumerate(parts):
        min_x, min_y, max_x, max_y = part.polygon.bounds
        depth = max_y - min_y
        if axis == "horizontal":
            start, span = min_y, depth
        else:
            start, span = min_x, max_x - min_x
        for group in groups:
            if (
                abs(group[0] - start) <= alignment_tolerance
                and abs(group[1] - span) <= alignment_tolerance
            ):
                group[2].append(index)
                break
        else:
            groups.append((start, span, [index]))
    return tuple(
        tuple(group[2])
        for group in groups
        if len(group[2]) >= 3
    )


def detect_repeated_bands(
    parts: tuple[TemplatePart, ...],
    *,
    alignment_tolerance: float = 2.0,
) -> tuple[TemplateStructure, ...]:
    """Detect three-or-more-part horizontal or vertical bands."""
    structures = []
    for axis, relation in (
        ("horizontal", "same_depth_row"),
        ("vertical", "same_width_column"),
    ):
        groups = _aligned_bands(
            parts,
            axis=axis,
            alignment_tolerance=alignment_tolerance,
        )
        for indices in groups:
            fill = _bounds_fill_ratio(parts[index].polygon for index in indices)
            structures.append(
                TemplateStructure(
                    kind=f"{axis}_band",
                    part_indices=indices,
                    fill_ratio=round(fill, 6),
                    relation=relation,
                )
            )
    return tuple(structures)


def detect_template_structures(
    parts: tuple[TemplatePart, ...],
    *,
    fill_tolerance: float = 0.98,
    alignment_tolerance: float = 2.0,
) -> tuple[TemplateStructure, ...]:
    """Induce generic relative-placement structures from one learned board."""
    return tuple(
        sorted(
            (
                *detect_complementary_pairs(
                    parts,
                    fill_tolerance=fill_tolerance,
                ),
                *detect_repeated_bands(
                    parts,
                    alignment_tolerance=alignment_tolerance,
                ),
            ),
            key=lambda item: (
                item.kind,
                item.part_indices,
            ),
        )
    )


def infer_optional_fillers(
    parts: tuple[TemplatePart, ...],
    structures: tuple[TemplateStructure, ...],
) -> tuple[int, ...]:
    """Mark non-structural slots as optional only when a core structure exists."""
    if not structures or not parts:
        return ()
    core_indices = {
        index
        for structure in structures
        for index in structure.part_indices
    }
    remaining = [
        index for index in range(len(parts)) if index not in core_indices
    ]
    if not remaining:
        return ()
    largest = max((part.area for part in parts), default=0.0)
    return tuple(
        index
        for index in remaining
        if parts[index].area <= largest * 0.35 + 1e-6
    )


def structure_signature(
    board: TemplateBoard,
    structure: TemplateStructure,
) -> tuple:
    """Return a coordinate-free signature for counting cross-board support."""
    return (
        structure.kind,
        structure.relation,
        tuple(
            sorted(
                board.parts[index].shape_family
                for index in structure.part_indices
            )
        ),
        round(structure.fill_ratio, 2),
    )


def summarize_structure_support(
    boards: Iterable[TemplateBoard],
) -> list[dict]:
    """Count how often each generic structure occurs across learned boards."""
    counts = Counter(
        structure_signature(board, structure)
        for board in boards
        for structure in board.structures
    )
    return [
        {
            "kind": signature[0],
            "relation": signature[1],
            "shape_families": [list(value) for value in signature[2]],
            "fill_ratio": signature[3],
            "support": support,
        }
        for signature, support in sorted(
            counts.items(),
            key=lambda item: (
                -item[1],
                item[0][0],
                item[0][1],
            ),
        )
    ]


def learn_template_boards(
    template_dxf: str | Path,
    sheet_width: float,
    sheet_height: float,
    *,
    frame_tolerance: float = 1.0,
) -> tuple[list[TemplateBoard], dict]:
    """Extract board recipes from a flat or layered reference DXF."""
    doc = ezdxf.readfile(str(template_dxf))
    msp = doc.modelspace()
    polygons = [
        Polygon([(float(x), float(y)) for x, y in entity.get_points("xy")])
        for entity in msp
        if entity.dxftype() == "LWPOLYLINE" and bool(entity.closed)
    ]
    sheets = [
        polygon
        for polygon in polygons
        if _is_sheet_rectangle(
            polygon,
            sheet_width,
            sheet_height,
            frame_tolerance,
        )
    ]
    if not sheets:
        raise ValueError("reference DXF contains no matching sheet rectangles")
    part_polygons = [polygon for polygon in polygons if polygon not in sheets]

    boards: list[TemplateBoard] = []
    assigned = 0
    unassigned = 0
    for sheet_index, sheet in enumerate(sheets, start=1):
        min_x, min_y, _, _ = sheet.bounds
        members = [
            polygon
            for polygon in part_polygons
            if sheet.covers(polygon.centroid)
        ]
        if not members:
            continue
        assigned += len(members)
        items = tuple(
            TemplatePart(
                shape_family=shape_family(polygon),
                polygon=affinity.translate(
                    polygon,
                    xoff=-min_x,
                    yoff=-min_y,
                ),
                area=float(polygon.area),
            )
            for polygon in members
        )
        structures = detect_template_structures(items)
        boards.append(
            TemplateBoard(
                index=sheet_index,
                width=sheet_width,
                height=sheet_height,
                parts=items,
                fill_ratio=sum(item.area for item in items)
                / (sheet_width * sheet_height),
                source_handle=str(sheet_index),
                structures=structures,
                optional_part_indices=infer_optional_fillers(
                    items,
                    structures,
                ),
            )
        )
    unassigned = len(part_polygons) - assigned

    summary = {
        "template_path": str(Path(template_dxf).resolve()),
        "source_sheet_count": len(sheets),
        "source_part_count": len(part_polygons),
        "assigned_part_count": assigned,
        "unassigned_part_count": unassigned,
        "nonempty_recipe_count": len(boards),
        "mean_fill_ratio": (
            sum(board.fill_ratio for board in boards) / len(boards)
            if boards
            else 0.0
        ),
        "recipe_fill_counts": dict(
            Counter(round(board.fill_ratio, 3) for board in boards)
        ),
        "structure_counts": dict(
            sorted(
                Counter(
                    structure.kind
                    for board in boards
                    for structure in board.structures
                ).items()
            )
        ),
        "boards_with_structures": sum(
            1 for board in boards if board.structures
        ),
        "optional_filler_count": sum(
            len(board.optional_part_indices) for board in boards
        ),
        "structure_support": summarize_structure_support(boards),
    }
    return boards, summary


def _candidate_forms(
    part: Part,
    fallback_rotations: tuple[float, ...],
) -> list[tuple[float, Polygon]]:
    outer = part.outer_polygon
    origin = (outer.centroid.x, outer.centroid.y)
    return [
        (
            angle,
            _normalized(
                affinity.rotate(outer, angle, origin=origin)
                if angle
                else outer
            ),
        )
        for angle in _rotation_options(part, fallback_rotations)
    ]


def _instantiate_template_subset(
    board: TemplateBoard,
    indices: tuple[int, ...],
    by_family: dict[tuple, list[Part]],
    used_part_ids: set[int],
    *,
    fallback_rotations: tuple[float, ...],
    shape_tolerance: float,
) -> tuple[list[_Placement], set[int]] | None:
    """Instantiate one exact relative-pose subset of a learned board."""
    selected: list[_Placement] = []
    temporary_ids: set[int] = set()
    for template_index in indices:
        template_part = board.parts[template_index]
        best = None
        for candidate in by_family.get(template_part.shape_family, []):
            if (
                candidate.id in used_part_ids
                or candidate.id in temporary_ids
            ):
                continue
            for angle, form in _candidate_forms(
                candidate,
                fallback_rotations,
            ):
                error = form.hausdorff_distance(
                    _normalized(template_part.polygon)
                )
                if error > shape_tolerance:
                    continue
                key = (
                    error,
                    len(_rotation_options(candidate, fallback_rotations)),
                    candidate.number,
                )
                if best is None or key < best[0]:
                    best = (key, candidate, angle, form)
        if best is None:
            return None
        _, candidate, angle, form = best
        target = template_part.polygon
        min_x, min_y, _, _ = target.bounds
        form_min_x, form_min_y, _, _ = form.bounds
        polygon = affinity.translate(
            form,
            xoff=min_x - form_min_x,
            yoff=min_y - form_min_y,
        )
        temporary_ids.add(candidate.id)
        selected.append(
            _Placement(
                part=candidate,
                rot=angle,
                x=min_x,
                y=min_y,
                poly=polygon,
            )
        )
    if not selected:
        return None

    # CAD copies can sit a few millimetres outside their frame.  Translate the
    # learned relative layout as one rigid board, never by moving one part.
    min_x = min(item.poly.bounds[0] for item in selected)
    min_y = min(item.poly.bounds[1] for item in selected)
    max_x = max(item.poly.bounds[2] for item in selected)
    max_y = max(item.poly.bounds[3] for item in selected)
    dx = max(0.0, -min_x)
    if max_x + dx > board.width:
        dx = board.width - max_x
    dy = max(0.0, -min_y)
    if max_y + dy > board.height:
        dy = board.height - max_y
    if dx or dy:
        selected = [
            _Placement(
                part=item.part,
                rot=item.rot,
                x=item.x + dx,
                y=item.y + dy,
                poly=affinity.translate(item.poly, xoff=dx, yoff=dy),
            )
            for item in selected
        ]
    for item in selected:
        min_x, min_y, max_x, max_y = item.poly.bounds
        if (
            min_x < -1e-6
            or min_y < -1e-6
            or max_x > board.width + 1e-6
            or max_y > board.height + 1e-6
        ):
            return None
    for first_index, first in enumerate(selected):
        for second in selected[first_index + 1:]:
            if first.poly.intersection(second.poly).area > 1e-6:
                return None
    return selected, temporary_ids


def _reuse_variants(board: TemplateBoard) -> list[tuple[int, ...]]:
    """Prefer the full learned board, then remove optional fillers in bounded sets."""
    all_indices = tuple(range(len(board.parts)))
    variants = [all_indices]
    optional = tuple(
        index
        for index in board.optional_part_indices
        if index in all_indices
    )
    max_optional_removals = min(2, len(optional))
    for count in range(1, max_optional_removals + 1):
        for removed in combinations(optional, count):
            removed_set = set(removed)
            variant = tuple(
                index for index in all_indices if index not in removed_set
            )
            if len(variant) >= min(2, len(all_indices)):
                variants.append(variant)
    return variants


def reuse_template_boards(
    parts: list[Part],
    templates: list[TemplateBoard],
    *,
    fallback_rotations: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0),
    min_fill_ratio: float = 0.0,
    shape_tolerance: float = 0.2,
    max_template_boards: int | None = None,
) -> TemplateReuseResult:
    """Greedily instantiate compatible board recipes from the current parts."""
    by_family: dict[tuple, list[Part]] = defaultdict(list)
    for part in parts:
        by_family[shape_family(part.outer_polygon)].append(part)
    for values in by_family.values():
        values.sort(key=lambda part: part.number)

    ordered = sorted(
        (
            board
            for board in templates
            if board.fill_ratio >= min_fill_ratio
        ),
        key=lambda board: (-board.fill_ratio, -len(board.parts), board.index),
    )
    used_part_ids: set[int] = set()
    sheets: list[list[_Placement]] = []
    used_templates: list[TemplateBoard] = []
    rejected = 0
    optional_variants_used = 0
    optional_slots_skipped = 0
    reused_structure_counts: Counter[str] = Counter()
    for board in ordered:
        if (
            max_template_boards is not None
            and len(sheets) >= max_template_boards
        ):
            break
        matched = None
        all_indices = tuple(range(len(board.parts)))
        for variant in _reuse_variants(board):
            matched = _instantiate_template_subset(
                board,
                variant,
                by_family,
                used_part_ids,
                fallback_rotations=fallback_rotations,
                shape_tolerance=shape_tolerance,
            )
            if matched is not None:
                selected, temporary_ids = matched
                if len(variant) < len(all_indices):
                    optional_variants_used += 1
                    optional_slots_skipped += len(all_indices) - len(variant)
                break
        if matched is None:
            rejected += 1
            continue
        used_part_ids.update(temporary_ids)
        sheets.append(selected)
        used_templates.append(board)
        selected_indices = set(variant)
        for structure in board.structures:
            if set(structure.part_indices).issubset(selected_indices):
                reused_structure_counts[structure.kind] += 1

    remaining = [part for part in parts if part.id not in used_part_ids]
    stats = {
        "template_candidates": len(ordered),
        "template_sheets_reused": len(sheets),
        "template_parts_reused": len(parts) - len(remaining),
        "template_parts_remaining": len(remaining),
        "template_sheets_rejected": rejected,
        "template_mean_reused_fill": (
            sum(board.fill_ratio for board in used_templates)
            / len(used_templates)
            if used_templates
            else 0.0
        ),
        "template_optional_variants_used": optional_variants_used,
        "template_optional_slots_skipped": optional_slots_skipped,
        "template_reused_structure_counts": dict(
            sorted(reused_structure_counts.items())
        ),
    }
    return TemplateReuseResult(
        sheets=sheets,
        remaining_parts=remaining,
        used_templates=used_templates,
        stats=stats,
    )


def nest_with_bridge_templates(
    parts: list[Part],
    template_dxf: str | Path,
    sheet_width: float,
    sheet_height: float,
    sheet_thickness: float,
    *,
    unit: str = "metric",
    template_min_fill_ratio: float = 0.0,
    template_shape_tolerance: float = 0.2,
    fallback_rotations: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0),
    stats: dict | None = None,
    nest_kwargs: dict | None = None,
) -> NestingResult:
    """Reuse compatible recipes, then nest all remaining parts normally."""
    templates, template_summary = learn_template_boards(
        template_dxf,
        sheet_width,
        sheet_height,
    )
    reused = reuse_template_boards(
        parts,
        templates,
        fallback_rotations=fallback_rotations,
        min_fill_ratio=template_min_fill_ratio,
        shape_tolerance=template_shape_tolerance,
    )
    template_result = _materialize(
        reused.sheets,
        sheet_width,
        sheet_height,
        sheet_thickness,
        unit,
        len(parts),
    )
    sheets = list(template_result.sheets)
    rest_stats: dict = {}
    if reused.remaining_parts:
        kwargs = dict(nest_kwargs or {})
        rest = nest_parts(
            reused.remaining_parts,
            sheet_width,
            sheet_height,
            sheet_thickness,
            unit=unit,
            stats=rest_stats,
            **kwargs,
        )
        sheets.extend(rest.sheets)
    for index, sheet in enumerate(sheets, start=1):
        sheet.index = index
    result = NestingResult(
        sheets=sheets,
        unit=unit,
        total_parts=sum(len(sheet.parts) for sheet in sheets),
        total_sheets=len(sheets),
        total_part_area=sum(sheet.used_area for sheet in sheets),
        total_sheet_area=sum(sheet.total_area for sheet in sheets),
    )
    if stats is not None:
        stats.update(
            {
                **template_summary,
                **reused.stats,
                "template_reuse_rest_stats": rest_stats,
            }
        )
    return result
