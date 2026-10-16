import csv
import json

import ezdxf
from shapely.geometry import Polygon

from src.numbering import assign_numbers
from src.spec_list import (
    build_spec_list,
    extract_annotations,
    write_spec_list_files,
)


def _rect(x0, y0, w, h=None):
    h = w if h is None else h
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]


def _part_data(index, number, w, h=None, *, x0=0.0, material=None,
               column=None, holes=0):
    outer = Polygon(_rect(x0, 0.0, w, h))
    return {
        "index": index,
        "polygon": outer,
        "outer_polygon": outer,
        "holes": [Polygon(_rect(0, 0, 10))] * holes,
        "centroid": (outer.centroid.x, outer.centroid.y),
        "area": outer.area,
        "original_number": number,
        "outer_handle": f"H{index}",
        "material_group": material,
        "material_column": column,
        "closure_gap": 0.0,
    }


# ---------------------------------------------------------------------------
# 清单构建
# ---------------------------------------------------------------------------


def test_build_spec_list_groups_by_material_zone_and_size():
    parts = assign_numbers([
        _part_data(1, "1001-01", 1800, 900, material="ST-6", column="L-01"),
        _part_data(2, "1001-02", 1800, 900, x0=2000, material="ST-6",
                   column="L-01"),
        _part_data(3, "1002-01", 1200, 600, x0=4000, material="ST-6",
                   column="L-02"),
        _part_data(4, "2001-01", 1800, 900, x0=6000, material="ST-7",
                   column="L-03"),
    ])
    spec = build_spec_list(parts, source_name="t.dxf")

    assert spec["totals"]["parts"] == 4
    assert spec["totals"]["groups"] == 3
    assert spec["totals"]["per_material"] == {
        "ST-6": {"parts": 3, "area_m2": round(
            (1.8 * 0.9 * 2 + 1.2 * 0.6), 4)},
        "ST-7": {"parts": 1, "area_m2": round(1.8 * 0.9, 4)},
    }

    first = spec["groups"][0]
    assert first["material"] == "ST-6"
    assert first["zone"] == "L-01"
    assert first["quantity"] == 2
    assert first["numbers"] == ["1001-01", "1001-02"]
    assert first["length_mm"] == 1800
    assert first["width_mm"] == 900
    # 图纸未注明的要求一律为空
    assert spec["annotations"] == {
        "surface_finish": "",
        "edge_finish": "",
        "grain_direction": "",
    }


def test_build_spec_list_normalizes_orientation_and_flags_irregular():
    # 横放矩形：长边在前
    data = [_part_data(1, "A-1", 900, 1800)]
    # L 形件：外轮廓面积 / 外包络 = 0.75 -> 异形
    l_shape = Polygon([
        (0, 0), (2000, 0), (2000, 1000), (1000, 1000),
        (1000, 2000), (0, 2000), (0, 0),
    ])
    data.append({
        "index": 2, "polygon": l_shape, "outer_polygon": l_shape,
        "holes": [], "centroid": (500.0, 500.0), "area": l_shape.area,
        "original_number": "A-2", "outer_handle": "H2",
        "material_group": None, "material_column": None,
        "closure_gap": 0.0,
    })
    spec = build_spec_list(assign_numbers(data))

    rect_group = next(g for g in spec["groups"] if "A-1" in g["numbers"])
    assert (rect_group["length_mm"], rect_group["width_mm"]) == (1800, 900)
    assert rect_group["irregular"] is False

    l_group = next(g for g in spec["groups"] if "A-2" in g["numbers"])
    assert l_group["irregular"] is True


# ---------------------------------------------------------------------------
# 图纸文字要求提取
# ---------------------------------------------------------------------------


def _doc_with_texts(texts):
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for i, text in enumerate(texts):
        entity = msp.add_text(text, dxfattribs={"height": 40.0})
        entity.set_placement((0.0, i * 100.0))
    return doc


def test_extract_annotations_fills_from_drawing_texts():
    doc = _doc_with_texts([
        "台面表面：抛光",
        "边型：1/4圆边",
        "纹理走向：顺纹",
        "101-A",  # 零件编号，不是要求
    ])
    result = extract_annotations(doc)
    assert "抛光" in result["surface_finish"]
    assert "圆边" in result["edge_finish"]
    assert "顺纹" in result["grain_direction"]
    assert result["evidence"]["surface_finish"] == ["台面表面：抛光"]


def test_extract_annotations_preserves_negation_context():
    doc = _doc_with_texts(["本页台面不磨边"])
    result = extract_annotations(doc)
    assert "不磨边" in result["edge_finish"]


def test_extract_annotations_empty_when_drawing_silent():
    doc = _doc_with_texts(["101-A", "102-B", "3525"])
    result = extract_annotations(doc)
    assert result["surface_finish"] == ""
    assert result["edge_finish"] == ""
    assert result["grain_direction"] == ""


# ---------------------------------------------------------------------------
# 输出文件
# ---------------------------------------------------------------------------


def test_write_spec_list_files(tmp_path):
    parts = assign_numbers([
        _part_data(1, "1001-01", 1800, 900, material="ST-6", column="L-01",
                   holes=1),
        _part_data(2, "1001-02", 1800, 900, x0=2000, material="ST-6",
                   column="L-01", holes=1),
    ])
    spec = build_spec_list(
        parts,
        annotations={
            "surface_finish": "台面表面：抛光",
            "edge_finish": "",
            "grain_direction": "",
            "evidence": {"surface_finish": ["台面表面：抛光"]},
        },
        source_name="t.dxf",
    )
    json_path, csv_path = write_spec_list_files(spec, tmp_path, "t")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["totals"]["parts"] == 2
    assert payload["annotations"]["surface_finish"] == "台面表面：抛光"
    assert payload["annotations"]["edge_finish"] == ""
    assert payload["groups"][0]["holes_total"] == 2

    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # utf-8-sig，Excel 直接打开
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0][:5] == ["材料", "区域", "长(mm)", "宽(mm)", "数量"]
    data_row = rows[1]
    assert data_row[0] == "ST-6"
    assert data_row[1] == "L-01"
    assert data_row[2:5] == ["1800", "900", "2"]
    assert data_row[8] == "1001-01、1001-02"
    assert data_row[9] == "台面表面：抛光"
    assert data_row[10] == ""  # 磨边未注明，留空
    assert rows[-1][0] == "合计"
    assert rows[-1][4] == "2"
