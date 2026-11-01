"""椭圆与样条的读入 / 保真复写 / 不静默兜底。"""

from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest

from src.drawing_profile import DrawingProfile, read_dxf_with_profile
from src.dxf_writer import write_nested_dxf, write_numbered_parts_dxf
from src.nesting import nest_parts
from src.numbering import assign_numbers


PROFILE = {
    "name": "curve_probe2",
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


def _rect(msp, attribs, w=400, h=200):
    polyline = msp.add_lwpolyline(
        [(0, 0), (w, 0), (w, h), (0, h)], dxfattribs=attribs
    )
    polyline.closed = True
    return polyline


def _read(path: Path):
    parts_data, _doc = read_dxf_with_profile(
        path, DrawingProfile.from_dict(PROFILE)
    )
    return parts_data


def test_ellipse_hole_is_recognized_and_reproduced(tmp_path):
    source = tmp_path / "ellipse.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    msp = doc.modelspace()
    attribs = {"layer": "PANEL"}
    _rect(msp, attribs)
    msp.add_ellipse(
        center=(200, 100), major_axis=(60, 0), ratio=0.6, dxfattribs=attribs
    )
    doc.saveas(source)

    parts_data = _read(source)
    assert len(parts_data) == 1
    # 关键回归：椭圆孔此前会被静默丢掉
    assert len(parts_data[0]["holes"]) == 1
    assert parts_data[0]["hole_shapes"][0]["type"] == "ellipse"

    parts = assign_numbers(parts_data, skip_unnumbered=False)
    numbered = tmp_path / "numbered.dxf"
    write_numbered_parts_dxf(parts, str(numbered))
    doc2 = ezdxf.readfile(numbered)
    holes = [e for e in doc2.modelspace() if e.dxf.layer == "HOLES"]
    assert [e.dxftype() for e in holes] == ["ELLIPSE"]

    result = nest_parts(
        parts, 1000, 800, 20, improve_budget=0, trials=1, rotations=(0, 90)
    )
    nested = tmp_path / "nested.dxf"
    write_nested_dxf(result, str(nested))
    doc3 = ezdxf.readfile(nested)
    nested_holes = [
        e for e in doc3.modelspace() if e.dxf.layer == "HOLES"
    ]
    assert [e.dxftype() for e in nested_holes] == ["ELLIPSE"]


def _spline_panel(path: Path) -> None:
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    msp = doc.modelspace()
    attribs = {"layer": "PANEL"}
    spline = msp.add_spline(
        [(0, 0), (200, -40), (400, 0), (400, 200), (200, 240), (0, 200)],
        dxfattribs=attribs,
    )
    spline.closed = True
    doc.saveas(path)


def test_closed_spline_panel_is_reproduced(tmp_path):
    source = tmp_path / "spline.dxf"
    _spline_panel(source)
    parts_data = _read(source)
    assert len(parts_data) == 1
    assert parts_data[0]["outer_curve"]["type"] == "spline"

    parts = assign_numbers(parts_data, skip_unnumbered=False)
    numbered = tmp_path / "numbered.dxf"
    write_numbered_parts_dxf(parts, str(numbered))
    doc = ezdxf.readfile(numbered)
    outers = [e for e in doc.modelspace() if e.dxf.layer == "OUTER"]
    assert [e.dxftype() for e in outers] == ["SPLINE"]
    source_curve = list(ezdxf.readfile(source).modelspace())[0]
    original = [
        (round(p.x, 4), round(p.y, 4))
        for p in source_curve.flattening(0.1)
    ]
    written = [
        (round(p.x, 4), round(p.y, 4))
        for p in outers[0].flattening(0.1)
    ]
    assert written == original

    result = nest_parts(
        parts, 1000, 800, 20, improve_budget=0, trials=1, rotations=(0, 90)
    )
    nested = tmp_path / "nested.dxf"
    write_nested_dxf(result, str(nested))
    doc2 = ezdxf.readfile(nested)
    nested_outers = [
        e for e in doc2.modelspace() if e.dxf.layer == "OUTER"
    ]
    assert [e.dxftype() for e in nested_outers] == ["SPLINE"]


def test_partial_spline_is_reported_not_silently_flattened(tmp_path):
    """只贴着轮廓一段的曲线无法原样复写：必须报审图问题。"""
    source = tmp_path / "partial.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    msp = doc.modelspace()
    attribs = {"layer": "PANEL"}
    _rect(msp, attribs)
    # 从板上边缘 (200,200) 向板内弯的开放样条
    msp.add_spline(
        [(200, 200), (230, 170), (200, 140), (170, 170)],
        dxfattribs=attribs,
    )
    doc.saveas(source)

    parts_data = _read(source)
    issues = parts_data[0].get("curve_fidelity_issues") or []
    assert any(item["type"] == "spline" for item in issues)

    from src.drawing_profile import audit_drawing

    audit = audit_drawing(source, DrawingProfile.from_dict(PROFILE))
    assert any(issue.type == "curve_not_reproduced" for issue in audit)
