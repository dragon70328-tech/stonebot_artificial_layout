"""原位 DXF = 源图副本 + 重分层 + 加编号。

要求：原图的尺寸标注、文字说明、填充、单位与精度必须原样保留；
没有分层的图纸要按语义重新分层；不同材料用不同颜色实线并注明厚度。
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest

from src.drawing_profile import DrawingProfile, read_dxf_with_profile
from src.dxf_writer import write_numbered_parts_dxf
from src.numbering import assign_numbers


PROFILE = {
    "name": "source_copy_probe",
    "version": "1.0.0",
    "panel_layer": "0",
    "panel_layers": ["0"],
    "network_mode": False,
    "network_layers": ["0"],
    "use_hatch": False,
    "hole_layers": ["0"],
    "build_hierarchy": True,
    "number_layers": [],
    "exclude_linetypes": [],
}


def _source(path: Path) -> None:
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4  # 毫米
    doc.header["$DIMDEC"] = 2    # 两位小数
    msp = doc.modelspace()
    attribs = {"layer": "0"}

    panel = msp.add_lwpolyline(
        [(0, 0), (400, 0), (400, 200), (0, 200)], dxfattribs=attribs
    )
    panel.closed = True

    text = msp.add_text("台面 20mm 厚，圆边", dxfattribs={**attribs, "height": 12})
    text.set_placement((20, 230))

    dim = msp.add_linear_dim(
        base=(0, 260), p1=(0, 0), p2=(400, 0), dxfattribs=attribs
    )
    dim.render()

    hatch = msp.add_hatch(dxfattribs=attribs)
    hatch.set_pattern_fill("ANSI31", scale=2.0)
    hatch.paths.add_polyline_path(
        [(0, 0), (400, 0), (400, 200), (0, 200)], is_closed=True
    )
    doc.saveas(path)


def _numbered(tmp_path: Path):
    source = tmp_path / "source.dxf"
    _source(source)
    parts_data, _doc = read_dxf_with_profile(
        source, DrawingProfile.from_dict(PROFILE)
    )
    parts = assign_numbers(parts_data, skip_unnumbered=False)
    for part in parts:
        part.material_group = "ST-8"
    output = tmp_path / "numbered.dxf"
    write_numbered_parts_dxf(
        parts,
        str(output),
        source_path=str(source),
        material_headers=None,
    )
    return source, output, parts, ezdxf.readfile(output)


def test_source_annotations_and_fills_are_preserved(tmp_path):
    _source_path, _output, _parts, doc = _numbered(tmp_path)
    msp = doc.modelspace()

    # 单位与精度随源文件保留
    assert doc.header["$INSUNITS"] == 4
    assert doc.header["$DIMDEC"] == 2

    # 尺寸标注、文字说明、填充都还在，且已归到标准图层
    assert [e.dxftype() for e in msp.query('*[layer=="DIMENSIONS"]')] == [
        "DIMENSION"
    ]
    text_layer = [e.dxftype() for e in msp.query('*[layer=="TEXT"]')]
    assert "TEXT" in text_layer
    assert [e.dxftype() for e in msp.query('*[layer=="HATCH"]')] == ["HATCH"]


def test_panel_is_relayered_and_colored_by_material(tmp_path):
    _source_path, _output, _parts, doc = _numbered(tmp_path)
    outers = list(doc.modelspace().query('*[layer=="OUTER"]'))
    assert len(outers) == 1
    # 单一材料 → 调色板第一个颜色（红）
    assert outers[0].dxf.color == 1


def test_numbers_and_material_thickness_are_annotated(tmp_path):
    _source_path, _output, _parts, doc = _numbered(tmp_path)
    msp = doc.modelspace()
    numbers = [e.dxf.text for e in msp.query('TEXT[layer=="NUMBERS"]')]
    assert numbers
    materials = [e.dxf.text for e in msp.query('TEXT[layer=="MATERIALS"]')]
    assert any("ST-8" in text and "20mm" in text for text in materials)


def test_default_thickness_noted_when_drawing_has_none(tmp_path):
    source = tmp_path / "plain.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    panel = msp.add_lwpolyline([(0, 0), (300, 0), (300, 150), (0, 150)])
    panel.closed = True
    doc.saveas(source)

    parts_data, _doc = read_dxf_with_profile(
        source, DrawingProfile.from_dict(PROFILE)
    )
    parts = assign_numbers(parts_data, skip_unnumbered=False)
    assert parts[0].thickness_source == "default"
    assert parts[0].thickness_mm == pytest.approx(20.0)
