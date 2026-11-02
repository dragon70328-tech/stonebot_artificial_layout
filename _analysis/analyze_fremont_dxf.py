"""Inspect the Fremont unit-plan DXF without modifying the source drawing.

The report is intentionally self-contained so annotation extraction can be
reviewed before a production DXF is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox


def _color(entity: Any, doc: ezdxf.document.Drawing) -> int:
    color = int(getattr(entity.dxf, "color", 256))
    if color == 256:
        layer_name = str(getattr(entity.dxf, "layer", "0"))
        try:
            return int(doc.layers.get(layer_name).dxf.color)
        except Exception:
            return 256
    return color


def _point(entity: Any) -> list[float] | None:
    for attr in ("insert", "location", "defpoint", "text_midpoint"):
        value = getattr(entity.dxf, attr, None)
        if value is not None:
            try:
                return [round(float(value.x), 6), round(float(value.y), 6)]
            except Exception:
                pass
    return None


def _bbox(entity: Any) -> list[float] | None:
    try:
        extents = bbox.extents([entity])
        if not extents.has_data:
            return None
        return [
            round(float(extents.extmin.x), 6),
            round(float(extents.extmin.y), 6),
            round(float(extents.extmax.x), 6),
            round(float(extents.extmax.y), 6),
        ]
    except Exception:
        return None


def _text_payload(entity: Any, doc: ezdxf.document.Drawing) -> dict[str, Any]:
    entity_type = entity.dxftype()
    text = ""
    height = None
    if entity_type == "TEXT":
        text = str(entity.dxf.text)
        height = float(entity.dxf.get("height", 0.0) or 0.0)
    elif entity_type == "MTEXT":
        text = str(entity.plain_text())
        height = float(entity.dxf.get("char_height", 0.0) or 0.0)
    elif entity_type == "ATTRIB":
        text = str(entity.dxf.text)
        height = float(entity.dxf.get("height", 0.0) or 0.0)
    return {
        "type": entity_type,
        "handle": entity.dxf.handle,
        "layer": str(entity.dxf.get("layer", "0")),
        "color": _color(entity, doc),
        "linetype": str(entity.dxf.get("linetype", "BYLAYER")),
        "text": text,
        "height": height,
        "point": _point(entity),
        "bbox": _bbox(entity),
        "rotation": float(entity.dxf.get("rotation", 0.0) or 0.0),
    }


def _dimension_payload(entity: Any, doc: ezdxf.document.Drawing) -> dict[str, Any]:
    text_override = str(entity.dxf.get("text", "<>") or "<>")
    measurement = entity.get_measurement()
    if not isinstance(measurement, (int, float)):
        measurement = None
    return {
        "handle": entity.dxf.handle,
        "layer": str(entity.dxf.get("layer", "0")),
        "color": _color(entity, doc),
        "dimtype": int(entity.dxf.get("dimtype", 0) or 0),
        "text_override": text_override,
        "measurement": measurement,
        "defpoint": _point(entity),
        "bbox": _bbox(entity),
        "dimstyle": str(entity.dxf.get("dimstyle", "")),
    }


def _closed_polyline_points(entity: Any) -> list[tuple[float, float]] | None:
    entity_type = entity.dxftype()
    try:
        if entity_type == "LWPOLYLINE" and bool(entity.closed):
            return [
                (float(point[0]), float(point[1])) for point in entity.get_points("xy")
            ]
        if entity_type == "POLYLINE" and bool(entity.is_closed):
            return [
                (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
                for vertex in entity.vertices
            ]
        if entity_type == "LINE":
            pass
    except Exception:
        return None
    return None


def _frame_candidate(entity: Any, doc: ezdxf.document.Drawing) -> dict[str, Any] | None:
    points = _closed_polyline_points(entity)
    if not points:
        return None
    color = _color(entity, doc)
    if color not in {1, 256}:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "handle": entity.dxf.handle,
        "type": entity.dxftype(),
        "layer": str(entity.dxf.get("layer", "0")),
        "color": color,
        "linetype": str(entity.dxf.get("linetype", "BYLAYER")),
        "vertex_count": len(points),
        "closed": True,
        "bbox": [min(xs), min(ys), max(xs), max(ys)],
        "width": max(xs) - min(xs),
        "height": max(ys) - min(ys),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dxf_path", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    source = args.dxf_path.resolve()
    raw = source.read_bytes()
    doc = ezdxf.readfile(source)
    msp = doc.modelspace()

    entity_counts = Counter()
    layer_counts = Counter()
    type_layer_counts: dict[str, Counter[str]] = defaultdict(Counter)
    text_items: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    leaders: list[dict[str, Any]] = []
    inserts: list[dict[str, Any]] = []
    frame_candidates: list[dict[str, Any]] = []

    for entity in msp:
        entity_type = entity.dxftype()
        layer = str(entity.dxf.get("layer", "0"))
        entity_counts[entity_type] += 1
        layer_counts[layer] += 1
        type_layer_counts[entity_type][layer] += 1

        if entity_type in {"TEXT", "MTEXT", "ATTRIB", "MULTILEADER"}:
            try:
                text_items.append(_text_payload(entity, doc))
            except Exception as exc:
                text_items.append(
                    {
                        "type": entity_type,
                        "handle": entity.dxf.handle,
                        "layer": layer,
                        "error": str(exc),
                    }
                )
        elif entity_type == "DIMENSION":
            dimensions.append(_dimension_payload(entity, doc))
        elif entity_type == "LEADER":
            leaders.append(
                {
                    "handle": entity.dxf.handle,
                    "layer": layer,
                    "color": _color(entity, doc),
                    "vertices": [
                        [float(vertex[0]), float(vertex[1])]
                        for vertex in entity.vertices
                    ],
                }
            )
        elif entity_type == "INSERT":
            inserts.append(
                {
                    "handle": entity.dxf.handle,
                    "layer": layer,
                    "name": str(entity.dxf.name),
                    "point": _point(entity),
                    "rotation": float(entity.dxf.get("rotation", 0.0) or 0.0),
                    "scale": [
                        float(entity.dxf.get("xscale", 1.0) or 1.0),
                        float(entity.dxf.get("yscale", 1.0) or 1.0),
                        float(entity.dxf.get("zscale", 1.0) or 1.0),
                    ],
                }
            )

        candidate = _frame_candidate(entity, doc)
        if candidate is not None:
            frame_candidates.append(candidate)

    text_items.sort(
        key=lambda item: (
            item.get("point", [0, 0])[1],
            item.get("point", [0, 0])[0],
            item.get("text", ""),
        )
    )
    dimensions.sort(
        key=lambda item: (
            item.get("defpoint", [0, 0])[1],
            item.get("defpoint", [0, 0])[0],
        )
    )

    report = {
        "source": str(source),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "dxf_version": doc.dxfversion,
        "encoding": doc.encoding,
        "header": {
            "INSUNITS": doc.header.get("$INSUNITS"),
            "MEASUREMENT": doc.header.get("$MEASUREMENT"),
            "DWGCODEPAGE": doc.header.get("$DWGCODEPAGE"),
            "LUNITS": doc.header.get("$LUNITS"),
            "LUPREC": doc.header.get("$LUPREC"),
        },
        "entity_counts": dict(entity_counts.most_common()),
        "layer_counts": dict(layer_counts.most_common()),
        "type_layer_counts": {
            entity_type: dict(counter.most_common())
            for entity_type, counter in sorted(type_layer_counts.items())
        },
        "modelspace_bbox": _bbox_for_space(msp),
        "texts": text_items,
        "dimensions": dimensions,
        "leaders": leaders,
        "inserts": inserts,
        "closed_frame_candidates": frame_candidates,
    }

    output = args.output
    if output is None:
        output = source.with_suffix(".analysis.json")
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({key: report[key] for key in (
        "source",
        "sha256",
        "dxf_version",
        "header",
        "entity_counts",
        "layer_counts",
        "modelspace_bbox",
    )}, ensure_ascii=False, indent=2))
    print(f"texts={len(text_items)} dimensions={len(dimensions)} leaders={len(leaders)}")
    print(f"inserts={len(inserts)} frame_candidates={len(frame_candidates)}")
    print(f"report={output}")
    return 0


def _bbox_for_space(msp: Any) -> list[float] | None:
    try:
        extents = bbox.extents(msp)
        if not extents.has_data:
            return None
        return [
            round(float(extents.extmin.x), 6),
            round(float(extents.extmin.y), 6),
            round(float(extents.extmax.x), 6),
            round(float(extents.extmax.y), 6),
        ]
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
