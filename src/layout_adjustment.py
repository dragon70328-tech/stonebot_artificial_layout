"""Propagate a user-adjusted nested-DXF board layout to similar boards.

The user edits one prototype sheet in the delivered nesting DXF and saves it
as a new file.  This module reads that file, records the prototype's relative
part poses and applies the same pose pattern to sheets with an identical
material-board shape inventory.

Only rigid translation is propagated.  Entity types are left untouched, so
circular arcs, bulges, text and holes keep their source representation.  Every
target sheet is validated before any of its groups are moved.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Iterable

import ezdxf
from shapely import affinity
from shapely.geometry import Point, Polygon

from src.nesting_readback import _polygonize_entities


_SHEET_LABEL = re.compile(r"^Sheet_(\d+)$", re.IGNORECASE)
_TOLERANCE_MM = 0.05
_ROUND_MM = 0.1


@dataclass
class NestedPartGroup:
    group_name: str
    number: str
    entities: list[Any]
    outer_entities: list[Any]
    hole_entities: list[Any]
    number_entities: list[Any]
    outer_polygon: Polygon
    holes: list[Polygon] = field(default_factory=list)
    planned_dx: float = 0.0
    planned_dy: float = 0.0

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return tuple(float(value) for value in self.outer_polygon.bounds)

    @property
    def shape_key(self) -> tuple:
        return canonical_shape_key(self.outer_polygon)

    def translated(self, dx: float, dy: float) -> "NestedPartGroup":
        return NestedPartGroup(
            group_name=self.group_name,
            number=self.number,
            entities=self.entities,
            outer_entities=self.outer_entities,
            hole_entities=self.hole_entities,
            number_entities=self.number_entities,
            outer_polygon=affinity.translate(
                self.outer_polygon,
                xoff=dx,
                yoff=dy,
            ),
            holes=[
                affinity.translate(hole, xoff=dx, yoff=dy)
                for hole in self.holes
            ],
            planned_dx=self.planned_dx + dx,
            planned_dy=self.planned_dy + dy,
        )


@dataclass
class NestedSheetLayout:
    index: int
    width: float
    height: float
    origin_x: float
    origin_y: float
    parts: list[NestedPartGroup]

    @property
    def inventory_key(self) -> tuple:
        return (
            round(self.width, _ROUND_MM_PLACES),
            round(self.height, _ROUND_MM_PLACES),
            tuple(sorted(part.shape_key for part in self.parts)),
        )


@dataclass
class LayoutAdjustmentResult:
    output_path: Path
    reference_sheet_index: int
    matched_sheet_indices: list[int]
    updated_sheet_indices: list[int]
    skipped_sheets: list[dict[str, Any]]
    moved_group_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "reference_sheet_index": self.reference_sheet_index,
            "matched_sheet_indices": self.matched_sheet_indices,
            "updated_sheet_indices": self.updated_sheet_indices,
            "skipped_sheets": self.skipped_sheets,
            "moved_group_count": self.moved_group_count,
            "policy": {
                "match": "identical_shape_inventory",
                "transform": "translation_only",
                "source_dxf_immutable": True,
                "reference_sheet_unchanged": True,
            },
        }


_ROUND_MM_PLACES = 3


def propagate_adjusted_layout(
    adjusted_dxf: str | Path,
    reference_sheet_index: int,
    output_dxf: str | Path,
) -> LayoutAdjustmentResult:
    """Apply one adjusted sheet's relative part poses to similar sheets."""
    source = Path(adjusted_dxf).expanduser().resolve()
    output = Path(output_dxf).expanduser().resolve()
    if source == output:
        raise ValueError("output DXF must differ from the adjusted input DXF")
    if not source.is_file():
        raise FileNotFoundError(source)

    doc = ezdxf.readfile(str(source))
    sheets, groups_by_sheet = _read_nested_layout(doc)
    reference = _sheet_by_index(sheets, int(reference_sheet_index))
    errors = _validate_sheet(reference)
    if errors:
        raise ValueError(
            f"reference sheet {reference.index} is invalid: "
            + "; ".join(errors)
        )

    candidates = [
        sheet
        for sheet in sheets
        if sheet.index != reference.index
        and sheet.inventory_key == reference.inventory_key
    ]
    prototype_by_shape = _group_by_shape_key(reference.parts)
    skipped: list[dict[str, Any]] = []
    updated: list[int] = []
    moved_groups = 0

    for sheet in candidates:
        target_by_shape = _group_by_shape_key(sheet.parts)
        planned: list[tuple[NestedPartGroup, NestedPartGroup, float, float]] = []
        mapping_errors: list[str] = []
        for shape_key, prototypes in prototype_by_shape.items():
            targets = target_by_shape.get(shape_key, [])
            if len(prototypes) != len(targets):
                mapping_errors.append(
                    f"shape occurrence mismatch for {shape_key}: "
                    f"{len(prototypes)} != {len(targets)}"
                )
                continue
            for prototype, target in zip(prototypes, targets):
                desired_x, desired_y = prototype.bounds[:2]
                current_x, current_y = target.bounds[:2]
                planned.append(
                    (
                        prototype,
                        target,
                        desired_x - current_x,
                        desired_y - current_y,
                    )
                )

        if mapping_errors:
            skipped.append({
                "sheet": sheet.index,
                "reason": "shape_mapping_failed",
                "errors": mapping_errors,
            })
            continue

        planned_parts = [
            target.translated(dx, dy)
            for prototype, target, dx, dy in planned
        ]
        proposed = NestedSheetLayout(
            index=sheet.index,
            width=sheet.width,
            height=sheet.height,
            origin_x=sheet.origin_x,
            origin_y=sheet.origin_y,
            parts=planned_parts,
        )
        plan_errors = _validate_sheet(proposed)
        if plan_errors:
            skipped.append({
                "sheet": sheet.index,
                "reason": "validation_failed",
                "errors": plan_errors,
            })
            continue
        untranslatable = [
            target.number
            for _prototype, target, dx, dy in planned
            if (
                abs(dx) > _TOLERANCE_MM or abs(dy) > _TOLERANCE_MM
            )
            and any(
                not callable(getattr(entity, "translate", None))
                for entity in target.entities
            )
        ]
        if untranslatable:
            skipped.append({
                "sheet": sheet.index,
                "reason": "translation_failed",
                "errors": [
                    f"{number} contains an entity that cannot translate"
                    for number in untranslatable
                ],
            })
            continue

        for _prototype, target, dx, dy in planned:
            if abs(dx) <= _TOLERANCE_MM and abs(dy) <= _TOLERANCE_MM:
                continue
            for entity in target.entities:
                entity.translate(dx, dy, 0)
            moved_groups += 1
        updated.append(sheet.index)

    output.parent.mkdir(parents=True, exist_ok=True)
    previous_fixed_meta = ezdxf.options.write_fixed_meta_data_for_testing
    ezdxf.options.write_fixed_meta_data_for_testing = True
    try:
        doc.saveas(str(output))
    finally:
        ezdxf.options.write_fixed_meta_data_for_testing = previous_fixed_meta

    return LayoutAdjustmentResult(
        output_path=output,
        reference_sheet_index=reference.index,
        matched_sheet_indices=[sheet.index for sheet in candidates],
        updated_sheet_indices=sorted(updated),
        skipped_sheets=skipped,
        moved_group_count=moved_groups,
    )


def canonical_shape_key(
    polygon: Polygon,
    *,
    quantum: float = _ROUND_MM,
) -> tuple:
    """Rotation-sensitive, translation-independent shape identity."""
    if polygon.is_empty:
        return ()
    min_x, min_y, _max_x, _max_y = polygon.bounds
    points = [
        (
            _quantize(x - min_x, quantum),
            _quantize(y - min_y, quantum),
        )
        for x, y in polygon.exterior.coords
    ]
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    if not points:
        return ()
    signed_area = sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )
    if signed_area < 0:
        points.reverse()
    start = min(range(len(points)), key=lambda index: points[index])
    ring = tuple(points[start:] + points[:start])
    return (
        _quantize(polygon.area, quantum),
        _quantize(polygon.length, quantum),
        ring,
    )


def _read_nested_layout(doc) -> tuple[list[NestedSheetLayout], dict[int, list]]:
    border_records = _read_sheet_borders(doc)
    if not border_records:
        raise ValueError("nested DXF contains no SHEET_BORDER polylines")

    groups = []
    try:
        for name, entities in doc.groups:
            groups.append((str(name), list(entities)))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("nested DXF has no readable part GROUPs") from exc

    by_sheet: dict[int, list[NestedPartGroup]] = defaultdict(list)
    for group_name, entities in groups:
        outer_entities = [
            entity
            for entity in entities
            if entity.dxf.layer == "OUTER"
            and entity.dxftype() in {
                "LWPOLYLINE",
                "ARC",
                "CIRCLE",
                "ELLIPSE",
                "SPLINE",
            }
        ]
        if not outer_entities:
            continue
        hole_entities = [
            entity
            for entity in entities
            if entity.dxf.layer == "HOLES"
            and entity.dxftype() in {
                "LWPOLYLINE",
                "ARC",
                "CIRCLE",
                "ELLIPSE",
                "SPLINE",
            }
        ]
        number_entities = [
            entity
            for entity in entities
            if entity.dxf.layer == "NUMBERS"
            and entity.dxftype() in {"TEXT", "MTEXT"}
        ]
        outer_abs = _largest_polygon(
            _polygonize_entities(outer_entities, 0.0, 0.0)
        )
        if outer_abs is None:
            continue
        sheet_index = _locate_polygon_sheet(outer_abs, border_records)
        border = border_records[sheet_index]
        local = affinity.translate(
            outer_abs,
            xoff=-border["origin_x"],
            yoff=-border["origin_y"],
        )
        holes_abs = _polygonize_entities(hole_entities, 0.0, 0.0)
        holes_local = [
            affinity.translate(
                hole,
                xoff=-border["origin_x"],
                yoff=-border["origin_y"],
            )
            for hole in holes_abs
        ]
        number = _group_number(group_name, number_entities)
        by_sheet[sheet_index].append(
            NestedPartGroup(
                group_name=group_name,
                number=number,
                entities=entities,
                outer_entities=outer_entities,
                hole_entities=hole_entities,
                number_entities=number_entities,
                outer_polygon=local,
                holes=holes_local,
            )
        )

    sheets = [
        NestedSheetLayout(
            index=record["index"],
            width=record["width"],
            height=record["height"],
            origin_x=record["origin_x"],
            origin_y=record["origin_y"],
            parts=by_sheet.get(position, []),
        )
        for position, record in enumerate(border_records)
    ]
    return sheets, by_sheet


def _read_sheet_borders(doc) -> list[dict[str, Any]]:
    borders = []
    labels = []
    for entity in doc.modelspace():
        if entity.dxf.layer != "SHEET_BORDER":
            continue
        if entity.dxftype() == "LWPOLYLINE":
            points = [
                (float(x), float(y))
                for x, y, *_ in entity.get_points(format="xy")
            ]
            if not points:
                continue
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            borders.append(
                {
                    "origin_x": min(xs),
                    "origin_y": min(ys),
                    "width": max(xs) - min(xs),
                    "height": max(ys) - min(ys),
                    "position": len(borders) + 1,
                    "index": len(borders) + 1,
                }
            )
        elif entity.dxftype() in {"TEXT", "MTEXT"}:
            text = str(getattr(entity.dxf, "text", "") or "").strip()
            match = _SHEET_LABEL.match(text)
            if match:
                insert = entity.dxf.insert
                labels.append(
                    (
                        int(match.group(1)),
                        float(insert.x),
                        float(insert.y),
                    )
                )

    borders.sort(
        key=lambda item: (
            round(item["origin_y"], 3),
            round(item["origin_x"], 3),
        )
    )
    if len(labels) == len(borders):
        for border in borders:
            nearest = min(
                labels,
                key=lambda item: (
                    abs(item[1] - border["origin_x"])
                    + abs(item[2] - (border["origin_y"] + border["height"]))
                ),
            )
            border["index"] = nearest[0]
    borders.sort(key=lambda item: item["index"])
    return borders


def _group_number(group_name: str, number_entities: Iterable[Any]) -> str:
    for entity in number_entities:
        text = str(getattr(entity.dxf, "text", "") or "").strip()
        if text:
            return text
    if group_name.startswith("PART_"):
        return group_name[5:].rsplit("_", 1)[0]
    return group_name


def _largest_polygon(polygons: Iterable[Polygon]) -> Polygon | None:
    valid = [
        polygon
        for polygon in polygons
        if not polygon.is_empty and polygon.area > 1e-6
    ]
    return max(valid, key=lambda polygon: polygon.area) if valid else None


def _locate_polygon_sheet(
    polygon: Polygon,
    borders: list[dict[str, Any]],
) -> int:
    centre = polygon.centroid
    for position, border in enumerate(borders):
        if (
            border["origin_x"] - _TOLERANCE_MM
            <= centre.x
            <= border["origin_x"] + border["width"] + _TOLERANCE_MM
            and border["origin_y"] - _TOLERANCE_MM
            <= centre.y
            <= border["origin_y"] + border["height"] + _TOLERANCE_MM
        ):
            return position
    return min(
        range(len(borders)),
        key=lambda position: (
            abs(
                centre.x
                - (
                    borders[position]["origin_x"]
                    + borders[position]["width"] * 0.5
                )
            )
            + abs(
                centre.y
                - (
                    borders[position]["origin_y"]
                    + borders[position]["height"] * 0.5
                )
            )
        ),
    )


def _sheet_by_index(
    sheets: list[NestedSheetLayout],
    index: int,
) -> NestedSheetLayout:
    for sheet in sheets:
        if sheet.index == index:
            return sheet
    known = [sheet.index for sheet in sheets]
    raise ValueError(f"unknown reference sheet {index}; known: {known}")


def _group_by_shape_key(
    parts: Iterable[NestedPartGroup],
) -> dict[tuple, list[NestedPartGroup]]:
    grouped: dict[tuple, list[NestedPartGroup]] = defaultdict(list)
    for part in parts:
        grouped[part.shape_key].append(part)
    for values in grouped.values():
        values.sort(
            key=lambda part: (
                round(part.bounds[1], 3),
                round(part.bounds[0], 3),
                part.group_name,
            )
        )
    return dict(grouped)


def _validate_sheet(sheet: NestedSheetLayout) -> list[str]:
    errors: list[str] = []
    number_counts = Counter(part.number for part in sheet.parts)
    if len(number_counts) != len(sheet.parts):
        duplicates = [
            number
            for number, count in number_counts.items()
            if count > 1
        ]
        errors.append(f"duplicate part numbers: {duplicates}")

    for part in sheet.parts:
        min_x, min_y, max_x, max_y = part.bounds
        if (
            min_x < -_TOLERANCE_MM
            or min_y < -_TOLERANCE_MM
            or max_x > sheet.width + _TOLERANCE_MM
            or max_y > sheet.height + _TOLERANCE_MM
        ):
            errors.append(
                f"{part.number} outside sheet bounds "
                f"({min_x:.2f},{min_y:.2f})-({max_x:.2f},{max_y:.2f})"
            )
        for hole in part.holes:
            if not part.outer_polygon.buffer(_TOLERANCE_MM).covers(
                hole.representative_point()
            ):
                errors.append(
                    f"{part.number} contains a detached hole"
                )
        if part.number_entities:
            insert = part.number_entities[0].dxf.insert
            label_point = Point(float(insert.x), float(insert.y))
            local_label = Point(
                label_point.x - sheet.origin_x + part.planned_dx,
                label_point.y - sheet.origin_y + part.planned_dy,
            )
            if not part.outer_polygon.buffer(1.0).covers(local_label):
                errors.append(
                    f"{part.number} number text is outside its outline"
                )

    for left_index, left in enumerate(sheet.parts):
        for right in sheet.parts[left_index + 1:]:
            if (
                left.outer_polygon.intersection(
                    right.outer_polygon
                ).area
                > 1e-6
            ):
                errors.append(
                    f"{left.number} overlaps {right.number}"
                )
    return errors


def _quantize(value: float, quantum: float) -> float:
    if quantum <= 0:
        raise ValueError("quantum must be positive")
    return round(round(float(value) / quantum) * quantum, 6)
