"""Build a lightweight, clickable preview model for the current drawing.

The preview is intentionally derived from the same correction context used by
the deterministic correction engine. Every rendered entity therefore carries
the exact target id accepted by ``POST /jobs/{job_id}/corrections``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon

from src.drawing_corrections import build_correction_context
from src.job_context import JobContext


def _polygons(geometry: Any) -> Iterator[Polygon]:
    """Yield polygonal components from a Shapely geometry."""
    if geometry is None or getattr(geometry, "is_empty", True):
        return
    geometry_type = getattr(geometry, "geom_type", "")
    if geometry_type == "Polygon":
        yield geometry
        return
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for item in geometry.geoms:
            yield from _polygons(item)


def _ring_points(ring: Any) -> list[list[float]]:
    points = [[round(float(x), 3), round(float(y), 3)] for x, y in ring.coords]
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    return points


def _target_id(value: Any) -> str:
    return str(value or "").strip()


def _panel_targets(ctx: JobContext) -> dict[int, str]:
    panels = (
        ctx.recognized_drawing.panels
        if ctx.recognized_drawing is not None
        else []
    )
    mapping: dict[int, str] = {}
    for index, part in enumerate(ctx.parts or []):
        panel_id = panels[index].panel_id if index < len(panels) else None
        mapping[int(part.id)] = _target_id(panel_id or part.id)
    return mapping


def _hole_target(part: Any, hole_index: int) -> str:
    if hole_index < len(part.hole_handles) and part.hole_handles[hole_index]:
        return f"handle:{part.hole_handles[hole_index]}"
    return f"p{part.id}:h{hole_index}"


def _raw_entities(ctx: JobContext) -> list[dict[str, Any]]:
    targets = _panel_targets(ctx)
    entities: list[dict[str, Any]] = []

    for part in ctx.parts or []:
        part_id = int(part.id)
        target_id = targets[part_id]
        label_position = tuple(float(value) for value in part.label_position)
        for polygon in _polygons(part.outer_polygon):
            entities.append(
                {
                    "target_id": target_id,
                    "kind": "panel",
                    "role": "outer",
                    "number": part.number,
                    "part_id": part_id,
                    "material_group": part.material_group,
                    "outer_handle": part.outer_handle,
                    "area": round(float(polygon.area), 3),
                    "points": _ring_points(polygon.exterior),
                    "label_position": list(label_position),
                    "bounds": [round(float(value), 3) for value in polygon.bounds],
                }
            )
            for interior in polygon.interiors:
                interior_polygon = Polygon(interior)
                entities.append(
                    {
                        "target_id": target_id,
                        "kind": "panel",
                        "role": "inner",
                        "number": part.number,
                        "part_id": part_id,
                        "area": round(float(interior_polygon.area), 3),
                        "points": _ring_points(interior),
                        "bounds": [
                            round(float(value), 3)
                            for value in interior_polygon.bounds
                        ],
                    }
                )

        for hole_index, hole in enumerate(part.holes):
            hole_target = _hole_target(part, hole_index)
            handle = (
                part.hole_handles[hole_index]
                if hole_index < len(part.hole_handles)
                else None
            )
            for polygon in _polygons(hole):
                entities.append(
                    {
                        "target_id": hole_target,
                        "kind": "hole",
                        "role": "hole",
                        "number": part.number,
                        "parent_target_id": target_id,
                        "part_id": part_id,
                        "entity_handle": handle,
                        "area": round(float(polygon.area), 3),
                        "points": _ring_points(polygon.exterior),
                        "bounds": [
                            round(float(value), 3)
                            for value in polygon.bounds
                        ],
                    }
                )
                for interior in polygon.interiors:
                    interior_polygon = Polygon(interior)
                    entities.append(
                        {
                            "target_id": hole_target,
                            "kind": "hole",
                            "role": "island",
                            "number": part.number,
                            "parent_target_id": target_id,
                            "part_id": part_id,
                            "entity_handle": handle,
                            "area": round(float(interior_polygon.area), 3),
                            "points": _ring_points(interior),
                            "bounds": [
                                round(float(value), 3)
                                for value in interior_polygon.bounds
                            ],
                        }
                    )

    known_handles = {
        handle
        for part in ctx.parts or []
        for handle in [part.outer_handle, *part.hole_handles]
        if handle
    }
    for candidate in getattr(ctx, "closed_entities", []):
        handle = candidate.get("handle")
        if not handle or handle in known_handles:
            continue
        target_id = f"handle:{handle}"
        for polygon in _polygons(candidate.get("polygon")):
            entities.append(
                {
                    "target_id": target_id,
                    "kind": "unrecognized",
                    "role": "outer",
                    "entity_handle": handle,
                    "entity_type": candidate.get("entity_type"),
                    "layer": candidate.get("layer"),
                    "linetype": candidate.get("linetype"),
                    "area": round(float(polygon.area), 3),
                    "points": _ring_points(polygon.exterior),
                    "bounds": [round(float(value), 3) for value in polygon.bounds],
                }
            )
            for interior in polygon.interiors:
                interior_polygon = Polygon(interior)
                entities.append(
                    {
                        "target_id": target_id,
                        "kind": "unrecognized",
                        "role": "inner",
                        "entity_handle": handle,
                        "entity_type": candidate.get("entity_type"),
                        "layer": candidate.get("layer"),
                        "linetype": candidate.get("linetype"),
                        "area": round(float(interior_polygon.area), 3),
                        "points": _ring_points(interior),
                        "bounds": [
                            round(float(value), 3)
                            for value in interior_polygon.bounds
                        ],
                    }
                )

    return entities


def _bounds(entities: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    if not entities:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(float(entity["bounds"][0]) for entity in entities),
        min(float(entity["bounds"][1]) for entity in entities),
        max(float(entity["bounds"][2]) for entity in entities),
        max(float(entity["bounds"][3]) for entity in entities),
    )


def _to_preview_points(
    points: list[list[float]],
    *,
    min_x: float,
    max_y: float,
) -> list[list[float]]:
    # SVG and DXF use opposite vertical directions. Convert once on the server
    # so the browser can render plain polygons without a nested transform.
    return [
        [round(float(x) - min_x, 3), round(max_y - float(y), 3)]
        for x, y in points
    ]


def build_drawing_preview(ctx: JobContext) -> dict[str, Any]:
    """Return the current in-place drawing as a clickable preview model."""
    entities = _raw_entities(ctx)
    min_x, min_y, max_x, max_y = _bounds(entities)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    padding = max(span_x, span_y) * 0.015 if entities else 0.0

    for entity in entities:
        entity["points"] = _to_preview_points(
            entity["points"],
            min_x=min_x,
            max_y=max_y,
        )
        label_position = entity.get("label_position")
        if label_position:
            entity["label_position"] = _to_preview_points(
                [label_position],
                min_x=min_x,
                max_y=max_y,
            )[0]

    correction_context = build_correction_context(ctx)
    source_name = ctx.dxf_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return {
        "revision": int(getattr(ctx, "correction_revision", 0)),
        "source_name": source_name,
        "view_box": [
            round(-padding, 3),
            round(-padding, 3),
            round(span_x + padding * 2, 3),
            round(span_y + padding * 2, 3),
        ],
        "bounds": [
            round(min_x, 3),
            round(min_y, 3),
            round(max_x, 3),
            round(max_y, 3),
        ],
        "counts": {
            "panels": len(correction_context.get("parts", [])),
            "holes": sum(
                len(part.get("holes", []))
                for part in correction_context.get("parts", [])
            ),
            "unrecognized": len(
                correction_context.get("unrecognized_entities", [])
            ),
            "entities": len(entities),
        },
        "entities": entities,
    }
