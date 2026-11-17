from __future__ import annotations

import ezdxf

from scripts.repair_r450_inplace_dxf import (
    _configure_dimension_style,
    _repair_pair,
)


def _bounds(outline) -> tuple[float, float, float, float]:
    points = list(outline.get_points())
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def test_repair_right_r450_pair_and_millimetre_dimension_style():
    doc = ezdxf.new("R2018")
    doc.layers.add("OUTER")
    doc.layers.add("R450_BOUNDARY", color=1)
    msp = doc.modelspace()

    lower_outline = msp.add_lwpolyline(
        [
            (0.0, 475.0, 0.0),
            (450.0, 925.0, 0.414213562),
            (1197.0, 475.0, 0.0),
            (1197.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
        ],
        format="xyb",
        close=True,
        dxfattribs={"layer": "OUTER"},
    )
    upper_outline = msp.add_lwpolyline(
        [
            (0.0, 1197.0, 0.0),
            (1197.0, 1197.0, 0.0),
            (1197.0, 475.0, -0.414213562),
            (450.0, 925.0, 0.0),
            (0.0, 925.0, 0.0),
        ],
        format="xyb",
        close=True,
        dxfattribs={"layer": "OUTER"},
    )
    arc = msp.add_arc(
        center=(747.0, 475.0),
        radius=450.0,
        start_angle=0.0,
        end_angle=90.0,
        dxfattribs={"layer": "R450_BOUNDARY"},
    )
    lower = doc.groups.new("D1F-2")
    lower.extend((lower_outline, arc))
    upper = doc.groups.new("D1F-2-ST101")
    upper.extend((upper_outline, arc))

    _repair_pair(doc, "D1F-2-ST101")
    _configure_dimension_style(doc)

    lower_bounds = _bounds(lower_outline)
    upper_bounds = _bounds(upper_outline)
    assert lower_bounds[3] - lower_bounds[1] == 725.0
    assert min(lower_bounds[2], upper_bounds[2]) - max(
        lower_bounds[0], upper_bounds[0]
    ) == 1197.0
    assert max(lower_bounds[3], upper_bounds[3]) - min(
        lower_bounds[1], upper_bounds[1]
    ) == 1197.0

    lower_bulges = [float(point[4]) for point in lower_outline.get_points()]
    upper_bulges = [float(point[4]) for point in upper_outline.get_points()]
    assert lower_bulges[1] < 0
    assert upper_bulges[2] > 0
    assert arc.dxf.center.x == 747.0
    assert arc.dxf.center.y == 275.0
    assert arc.dxf.start_angle == 0.0
    assert arc.dxf.end_angle == 90.0

    style = doc.dimstyles.get("STONEBOT_MM")
    assert style.dxf.dimlfac == 1.0
    assert style.dxf.dimscale == 1.0
    assert doc.layers.get("R450_BOUNDARY").is_off()
    assert doc.layers.get("R450_BOUNDARY").is_frozen()
