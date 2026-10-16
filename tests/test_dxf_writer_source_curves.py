"""原位 DXF 的"如实复写源实体类型"回归测试（2026-10-16 用户要求）。

用户要的是：源图里是圆/圆弧的，原位 DXF 就写 CIRCLE/ARC（能在 CAD 里直接
量半径）；源图本身是多段线的（哪怕画成一个圆），仍旧写多段线。

判断在读图阶段做（``dxf_reader.attach_source_curves`` → ``Part.outer_shape``
/ ``Part.outer_arcs`` / ``Part.hole_shapes``），写入端按它出实体。
"""

import math

import ezdxf
import pytest

from src.drawing_profile import DrawingProfile, read_dxf_with_profile
from src.dxf_writer import write_numbered_parts_dxf
from src.numbering import assign_numbers


PROFILE = {
    "name": "curve_probe",
    "version": "1.0.0",
    "panel_layer": "PANEL",
    "panel_layers": ["PANEL"],
    "network_mode": True,
    "network_layers": ["PANEL"],
    "use_hatch": False,
    "hole_layers": ["PANEL"],
    "build_hierarchy": True,
    "number_layers": [],
    "exclude_linetypes": [],
}


def _stadium_with_holes(path) -> None:
    """外轮廓 = 两条直线 + 两段圆弧；孔 = 一个 CIRCLE + 一个多段线圆。"""
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    msp = doc.modelspace()
    attribs = {"layer": "PANEL"}

    msp.add_line((0, 0), (100, 0), dxfattribs=attribs)
    msp.add_arc((100, 50), 50, 270, 90, dxfattribs=attribs)
    msp.add_line((100, 100), (0, 100), dxfattribs=attribs)
    msp.add_arc((0, 50), 50, 90, 270, dxfattribs=attribs)

    # 真圆孔（源图是 CIRCLE）
    msp.add_circle((30, 50), 10, dxfattribs=attribs)

    # 多段线圆孔（源图是 LWPOLYLINE，即使画成正圆也要保持多段线）
    points = [
        (70 + 10 * math.cos(a), 50 + 10 * math.sin(a))
        for a in [index * 2 * math.pi / 64 for index in range(64)]
    ]
    polyline = msp.add_lwpolyline(points, dxfattribs=attribs)
    polyline.closed = True

    doc.saveas(path)


@pytest.fixture()
def numbered(tmp_path):
    path = tmp_path / "stadium.dxf"
    _stadium_with_holes(path)
    parts_data, _doc = read_dxf_with_profile(
        path, DrawingProfile.from_dict(PROFILE)
    )
    assert len(parts_data) == 1, parts_data
    parts = assign_numbers(parts_data, skip_unnumbered=False)

    output = tmp_path / "numbered.dxf"
    write_numbered_parts_dxf(parts, str(output))
    return parts_data[0], ezdxf.readfile(output)


def test_source_circle_hole_stays_circle_and_polyline_hole_stays_polyline(numbered):
    _panel, doc = numbered
    holes = [e for e in doc.modelspace() if e.dxf.layer == "HOLES"]
    assert sorted(entity.dxftype() for entity in holes) == ["CIRCLE", "LWPOLYLINE"]

    circle = next(entity for entity in holes if entity.dxftype() == "CIRCLE")
    assert circle.dxf.radius == pytest.approx(10.0)
    assert (circle.dxf.center.x, circle.dxf.center.y) == pytest.approx((30.0, 50.0))

    polyline = next(entity for entity in holes if entity.dxftype() == "LWPOLYLINE")
    # 多段线孔仍旧是多段线（首尾顶点重合的闭环，写入端历来如此）
    points = [(x, y) for x, y in polyline.get_points("xy")]
    assert points[0] == pytest.approx(points[-1])
    assert len(points) == 65


def test_source_arc_on_outline_is_written_as_arc(numbered):
    panel, doc = numbered
    assert len(panel.get("outer_arcs") or []) == 2

    outline = [e for e in doc.modelspace() if e.dxf.layer == "OUTER"]
    arcs = [e for e in outline if e.dxftype() == "ARC"]
    assert len(arcs) == 2
    assert {round(arc.dxf.radius, 3) for arc in arcs} == {50.0}
    for arc in arcs:
        span = (arc.dxf.end_angle - arc.dxf.start_angle) % 360.0
        assert span == pytest.approx(180.0, abs=1.0)

    # 直线部分仍是折线（两条直线各一段，端点是环上的顶点）
    polylines = [e for e in outline if e.dxftype() == "LWPOLYLINE"]
    assert polylines
    total_points = sum(len(list(entity.get_points("xy"))) for entity in polylines)
    assert 4 <= total_points <= 8
