"""圆弧保真：源图用多段线画的弧，原位 DXF 与排板 DXF 都必须还原成弧。

用户口径（机床按 DXF 直接加工）：源图是弧线的，原位 DXF 必须一致；
源图本身是多段线的弧线，输出也必须保持同一条多段线（bulge 不变）。
"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
import pytest
from shapely.geometry import Polygon

from src.drawing_profile import DrawingProfile, read_dxf_with_profile
from src.dxf_writer import write_nested_dxf, write_numbered_parts_dxf
from src.confirmed_drawing import build_confirmed_drawing
from src.job_context import JobContext
from src.nesting import nest_parts
from src.nesting_readback import _entity_points, verify_nested_dxf, sheets_payload_from_result
from src.nesting_revisions import (
    build_nesting_request,
    build_nesting_revision,
    result_from_revision,
)
from src.numbering import assign_numbers
from src.units import UnitSystem
from src.workflow import GroupNestingOutcome


PROFILE = {
    "name": "arc_probe",
    "version": "1.0.0",
    "panel_layer": "PANEL",
    "panel_layers": ["PANEL"],
    "network_mode": False,
    "network_layers": ["PANEL"],
    "use_hatch": False,
    "hole_layers": ["PANEL"],
    "build_hierarchy": True,
    "number_layers": [],
    "exclude_linetypes": [],
}


def _bulge_panel(path: Path) -> None:
    """400x200 台面：右边缘是 bulge=1 的半圆；内部一个带圆弧段的孔。"""
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    msp = doc.modelspace()
    attribs = {"layer": "PANEL"}

    outer = msp.add_lwpolyline(
        [(0, 0, 0), (400, 0, 1.0), (400, 200, 0), (0, 200, 0)],
        format="xyb",
        dxfattribs=attribs,
    )
    outer.closed = True

    hole = msp.add_lwpolyline(
        [
            (100, 80, 0),
            (160, 80, 0.5),
            (160, 120, 0),
            (100, 120, 0),
        ],
        format="xyb",
        dxfattribs=attribs,
    )
    hole.closed = True
    doc.saveas(path)


@pytest.fixture()
def parts(tmp_path):
    source = tmp_path / "arc_panel.dxf"
    _bulge_panel(source)
    parts_data, _doc = read_dxf_with_profile(
        source, DrawingProfile.from_dict(PROFILE)
    )
    assert len(parts_data) == 1
    assert parts_data[0].get("outer_polyline")
    assert parts_data[0]["hole_shapes"][0]["type"] == "polyline"
    return assign_numbers(parts_data, skip_unnumbered=False)


def _bulges(entity) -> list[float]:
    return [
        round(float(bulge), 4)
        for _x, _y, bulge in entity.get_points("xyb")
    ]


def test_numbered_dxf_keeps_polyline_arcs(parts, tmp_path):
    output = tmp_path / "numbered.dxf"
    write_numbered_parts_dxf(parts, str(output))
    doc = ezdxf.readfile(output)
    msp = doc.modelspace()

    outers = [e for e in msp if e.dxf.layer == "OUTER"]
    assert len(outers) == 1
    assert outers[0].dxftype() == "LWPOLYLINE"
    outer_bulges = _bulges(outers[0])
    assert len(outer_bulges) == 4
    assert outer_bulges[1] == pytest.approx(1.0)

    holes = [e for e in msp if e.dxf.layer == "HOLES"]
    assert len(holes) == 1
    hole_bulges = _bulges(holes[0])
    assert hole_bulges[1] == pytest.approx(0.5)


@pytest.mark.parametrize("rotation", [(0,), (0, 90)])
def test_nested_dxf_keeps_arcs_and_passes_readback(parts, tmp_path, rotation):
    result = nest_parts(
        parts,
        1000,
        800,
        20,
        improve_budget=0,
        trials=1,
        rotations=rotation,
        min_gap=0.0,
    )
    output = tmp_path / f"nested_{rotation[-1]}.dxf"
    write_nested_dxf(result, str(output))

    doc = ezdxf.readfile(output)
    msp = doc.modelspace()
    outers = [e for e in msp if e.dxf.layer == "OUTER"]
    assert len(outers) == 1
    assert outers[0].dxftype() == "LWPOLYLINE"
    assert any(abs(value) > 1e-9 for value in _bulges(outers[0]))

    # 写出轮廓（展开 bulge 后）必须与落位多边形几何一致
    placed = result.sheets[0].parts[0]
    expected = Polygon(placed.outer_polygon.exterior.coords)
    points = _entity_points(outers[0], 0.0, 0.0)
    actual = Polygon(points)
    assert expected.hausdorff_distance(actual) <= 2.0

    # 回读门禁必须认弧线（不能因为"不是多段线折线"而误报）
    assert verify_nested_dxf(
        output, sheets_payload_from_result(result)
    ) == []


def test_rollback_delivery_keeps_arcs(parts, tmp_path):
    """回滚交付必须仍写真圆弧（版本布局载荷要带上源曲线谱）。"""
    result = nest_parts(
        parts,
        1000,
        800,
        20,
        improve_budget=0,
        trials=1,
        rotations=(0, 90),
    )
    context = JobContext(
        nesting_profile=None,
        parts=parts,
        unit=UnitSystem.METRIC,
        result=result,
        width_mm=1000,
        height_mm=800,
        thickness_mm=20,
    )
    context.nesting_outcome = GroupNestingOutcome(
        result=result,
        material_summary=[],
        total_min_sheets=1,
        total_normal_parts=len(parts),
        total_special_parts=0,
        total_special_pair_min_sheets=None,
        elapsed_seconds=0.0,
    )
    context.confirmed_drawing = build_confirmed_drawing(context)
    revision = build_nesting_revision(
        context, build_nesting_request(context)
    )

    restored = result_from_revision(revision)
    output = tmp_path / "rolled_back.dxf"
    write_nested_dxf(restored, str(output))
    doc = ezdxf.readfile(output)
    outers = [
        e for e in doc.modelspace() if e.dxf.layer == "OUTER"
    ]
    assert len(outers) == 1
    assert any(abs(value) > 1e-9 for value in _bulges(outers[0]))
