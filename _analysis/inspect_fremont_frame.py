"""Print contents and candidate part outlines of one red drawing frame."""

from __future__ import annotations

import argparse
from pathlib import Path

import ezdxf
from ezdxf import bbox


def entity_color(entity, doc) -> int:
    color = int(entity.dxf.get("color", 256))
    if color == 256:
        return int(doc.layers.get(entity.dxf.layer).dxf.color)
    return color


def inside_bbox(entity, frame: tuple[float, float, float, float]) -> bool:
    try:
        extents = bbox.extents([entity])
        if not extents.has_data:
            return False
        center_x = (extents.extmin.x + extents.extmax.x) / 2.0
        center_y = (extents.extmin.y + extents.extmax.y) / 2.0
        return frame[0] <= center_x <= frame[2] and frame[1] <= center_y <= frame[3]
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("handle")
    args = parser.parse_args()
    doc = ezdxf.readfile(args.source)
    msp = doc.modelspace()
    frame_entity = msp.entitydb[args.handle]
    points = list(frame_entity.get_points("xy"))
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    frame = (min(xs), min(ys), max(xs), max(ys))
    print("frame", frame, "size", frame[2] - frame[0], frame[3] - frame[1])

    for entity in msp:
        if not inside_bbox(entity, frame):
            continue
        xtype = entity.dxftype()
        layer = entity.dxf.get("layer", "0")
        color = entity_color(entity, doc)
        extents = bbox.extents([entity])
        bounds = None
        if extents.has_data:
            bounds = [
                round(extents.extmin.x, 3),
                round(extents.extmin.y, 3),
                round(extents.extmax.x, 3),
                round(extents.extmax.y, 3),
            ]
        if xtype == "LWPOLYLINE":
            print(
                "LWPOLYLINE",
                entity.dxf.handle,
                "color",
                color,
                "closed",
                bool(entity.closed),
                "verts",
                len(entity),
                "bbox",
                bounds,
                "size",
                None
                if bounds is None
                else [
                    round(bounds[2] - bounds[0], 3),
                    round(bounds[3] - bounds[1], 3),
                ],
            )
        elif xtype == "TEXT":
            print(
                "TEXT",
                entity.dxf.handle,
                "color",
                color,
                "text",
                entity.dxf.text,
                "bbox",
                bounds,
            )
        elif xtype == "DIMENSION":
            print(
                "DIMENSION",
                entity.dxf.handle,
                "color",
                color,
                "measurement",
                entity.get_measurement(),
                "bbox",
                bounds,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
