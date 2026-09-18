"""Graphical DXF diff for numbered in-place drawing corrections."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import units

from src.models import Part


LAYER_UNCHANGED = "CORRECTION_DIFF_UNCHANGED"
LAYER_ADDED = "CORRECTION_DIFF_ADDED"
LAYER_REMOVED = "CORRECTION_DIFF_REMOVED"
LAYER_BEFORE = "CORRECTION_DIFF_CHANGED_BEFORE"
LAYER_AFTER = "CORRECTION_DIFF_CHANGED_AFTER"
LAYER_NOTE = "CORRECTION_DIFF_NOTE"

COLOR_UNCHANGED = 8
COLOR_ADDED = 3
COLOR_REMOVED = 1
COLOR_BEFORE = 6
COLOR_AFTER = 2
COLOR_NOTE = 7


def _polygon_parts(geometry: Any) -> list[Any]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type == "MultiPolygon":
        return list(geometry.geoms)
    return [geometry]


def _draw_geometry(msp: Any, geometry: Any, layer: str, color: int) -> int:
    count = 0
    for polygon in _polygon_parts(geometry):
        exterior = getattr(polygon, "exterior", None)
        if exterior is None:
            continue
        points = [(float(x), float(y)) for x, y in exterior.coords]
        msp.add_lwpolyline(
            points,
            close=True,
            dxfattribs={"layer": layer, "color": color},
        )
        count += 1
    return count


def _draw_part(msp: Any, part: Part, layer: str, color: int) -> int:
    count = _draw_geometry(msp, part.outer_polygon, layer, color)
    for hole in part.holes:
        count += _draw_geometry(msp, hole, layer, color)
    bounds = part.outer_polygon.bounds
    height = max(15.0, min(60.0, (bounds[3] - bounds[1]) * 0.08))
    label = msp.add_text(
        part.number,
        dxfattribs={"layer": layer, "color": color, "height": height},
    )
    label.set_placement(
        part.outer_polygon.centroid.coords[0],
        align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER,
    )
    return count + 1


def _signature(part: Part) -> tuple[Any, ...]:
    return (
        part.number,
        part.original_number,
        part.material_group,
        part.outer_polygon.wkt,
        tuple(hole.wkt for hole in part.holes),
    )


def write_correction_diff_dxf(
    before_parts: list[Part],
    after_parts: list[Part],
    output_path: str | Path,
    *,
    revision: int,
    instruction: str,
) -> dict[str, int]:
    """Write a coloured before/after DXF and return change counts."""
    doc = ezdxf.new("R2010")
    doc.units = units.MM
    for layer, color in (
        (LAYER_UNCHANGED, COLOR_UNCHANGED),
        (LAYER_ADDED, COLOR_ADDED),
        (LAYER_REMOVED, COLOR_REMOVED),
        (LAYER_BEFORE, COLOR_BEFORE),
        (LAYER_AFTER, COLOR_AFTER),
        (LAYER_NOTE, COLOR_NOTE),
    ):
        doc.layers.add(layer, color=color)

    msp = doc.modelspace()
    before_by_id = {int(part.id): part for part in before_parts}
    after_by_id = {int(part.id): part for part in after_parts}
    counts = {"unchanged": 0, "added": 0, "removed": 0, "changed": 0}

    for part_id in sorted(set(before_by_id) | set(after_by_id)):
        before = before_by_id.get(part_id)
        after = after_by_id.get(part_id)
        if before is None and after is not None:
            _draw_part(msp, after, LAYER_ADDED, COLOR_ADDED)
            counts["added"] += 1
        elif before is not None and after is None:
            _draw_part(msp, before, LAYER_REMOVED, COLOR_REMOVED)
            counts["removed"] += 1
        elif before is not None and after is not None:
            if _signature(before) == _signature(after):
                _draw_part(msp, after, LAYER_UNCHANGED, COLOR_UNCHANGED)
                counts["unchanged"] += 1
            else:
                _draw_part(msp, before, LAYER_BEFORE, COLOR_BEFORE)
                _draw_part(msp, after, LAYER_AFTER, COLOR_AFTER)
                counts["changed"] += 1

    msp.add_text(
        f"Revision r{revision}: {instruction}",
        dxfattribs={"layer": LAYER_NOTE, "color": COLOR_NOTE, "height": 80.0},
    ).set_placement((0, -150))
    doc.saveas(output_path)
    return counts
