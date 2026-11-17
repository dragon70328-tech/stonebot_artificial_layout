"""Repair R450 pair orientation, exact 725 mm height and millimetre dimensions."""

from __future__ import annotations

import argparse
from pathlib import Path

import ezdxf


def _outline(group):
    for entity in group:
        if entity.dxftype() == "LWPOLYLINE":
            return entity
    raise ValueError(f"GROUP {group.dxf.name} 中没有 OUTER 多段线")


def _bounds(entity):
    points = list(entity.get_points())
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _repair_pair(doc, upper_name: str) -> None:
    if not upper_name.endswith("-ST101"):
        return
    lower_name = upper_name[: -len("-ST101")]
    upper_group = doc.groups.get(upper_name)
    lower_group = doc.groups.get(lower_name)
    if upper_group is None or lower_group is None:
        return

    upper_outline = _outline(upper_group)
    lower_outline = _outline(lower_group)
    upper_bounds = _bounds(upper_outline)
    lower_bounds = _bounds(lower_outline)
    left = min(upper_bounds[0], lower_bounds[0])
    bottom = min(upper_bounds[1], lower_bounds[1])
    right = max(upper_bounds[2], lower_bounds[2])
    top = max(upper_bounds[3], lower_bounds[3])

    arc = next(
        (
            entity
            for entity in lower_group
            if entity.dxftype() == "ARC"
            and entity.dxf.layer == "R450_BOUNDARY"
        ),
        None,
    )
    if arc is None:
        raise ValueError(f"GROUP {lower_name} 中没有 R450 ARC")

    midpoint = (left + right) / 2.0
    side = "left" if arc.dxf.center.x < midpoint else "right"
    endpoints = [arc.start_point, arc.end_point]
    side_x = left if side == "left" else right
    side_point = min(endpoints, key=lambda point: abs(point.x - side_x))
    flat_point = max(endpoints, key=lambda point: abs(point.x - side_x))
    direction = 1.0 if flat_point.y > side_point.y else -1.0

    flat_x = left + 450.0 if side == "left" else right - 450.0
    if direction > 0:
        flat_y = bottom + 725.0
        boundary_y = flat_y - 450.0
    else:
        flat_y = top - 725.0
        boundary_y = flat_y + 450.0
    flat = (flat_x, flat_y)
    boundary = (side_x, boundary_y)

    # Quarter-circle bulges: positive is CCW, negative is clockwise.
    if (side == "left" and direction > 0) or (
        side == "right" and direction < 0
    ):
        forward_bulge = -0.41421356237309503
        reverse_bulge = -forward_bulge
    else:
        forward_bulge = 0.41421356237309503
        reverse_bulge = -forward_bulge

    if side == "left":
        upper_points = [
            (left, top, 0.0),
            (right, top, 0.0),
            (right, flat_y, 0.0),
            (flat_x, flat_y, reverse_bulge),
            (left, boundary_y, 0.0),
        ]
        lower_points = [
            (left, boundary_y, forward_bulge),
            (flat_x, flat_y, 0.0),
            (right, flat_y, 0.0),
            (right, bottom, 0.0),
            (left, bottom, 0.0),
        ]
    else:
        upper_points = [
            (left, top, 0.0),
            (right, top, 0.0),
            (right, boundary_y, forward_bulge),
            (flat_x, flat_y, 0.0),
            (left, flat_y, 0.0),
        ]
        lower_points = [
            (left, flat_y, 0.0),
            (flat_x, flat_y, reverse_bulge),
            (right, boundary_y, 0.0),
            (right, bottom, 0.0),
            (left, bottom, 0.0),
        ]
    upper_outline.set_points(upper_points, format="xyb")
    lower_outline.set_points(lower_points, format="xyb")

    arc.dxf.center = (flat_x, boundary_y, 0.0)
    arc.dxf.radius = 450.0
    if side == "left" and direction > 0:
        arc.dxf.start_angle, arc.dxf.end_angle = 90.0, 180.0
    elif side == "left" and direction < 0:
        arc.dxf.start_angle, arc.dxf.end_angle = 180.0, 270.0
    elif side == "right" and direction > 0:
        arc.dxf.start_angle, arc.dxf.end_angle = 0.0, 90.0
    else:
        arc.dxf.start_angle, arc.dxf.end_angle = 270.0, 360.0


def _configure_dimension_style(doc) -> None:
    if "STONEBOT_MM" in doc.dimstyles:
        style = doc.dimstyles.get("STONEBOT_MM")
    else:
        style = doc.dimstyles.add("STONEBOT_MM")
    style.dxf.dimlfac = 1.0
    style.dxf.dimscale = 1.0
    style.dxf.dimlunit = 2
    style.dxf.dimdec = 2
    style.dxf.dimzin = 8
    style.dxf.dimtxt = 60.0
    style.dxf.dimasz = 40.0
    doc.header["$DIMSTYLE"] = "STONEBOT_MM"
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1

    if "R450_BOUNDARY" in doc.layers:
        layer = doc.layers.get("R450_BOUNDARY")
        layer.dxf.color = 7
        layer.off()
        layer.freeze()


def repair(input_path: str | Path, output_path: str | Path) -> int:
    doc = ezdxf.readfile(str(Path(input_path).expanduser().resolve()))
    repaired = 0
    for name, _group in list(doc.groups):
        if name.endswith("-ST101"):
            _repair_pair(doc, name)
            repaired += 1
    _configure_dimension_style(doc)
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(target)
    return repaired


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dxf", type=Path)
    parser.add_argument("output_dxf", type=Path)
    args = parser.parse_args()
    print(repair(args.input_dxf, args.output_dxf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
