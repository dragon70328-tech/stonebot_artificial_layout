"""GROUP 契约回归测试。

历史问题：``doc.groups.new(name, entities)`` 的第二个位置参数其实是
description，所以写出的排板 DXF 里 GROUP 一直是空的；重复编号还会抛
``DXFValueError`` 并被静默吞掉，导致该面板完全没有 GROUP。
"""

import ezdxf
from shapely.geometry import Polygon

from src.dxf_writer import write_nested_dxf, write_numbered_parts_dxf
from src.models import NestingResult, Part, Sheet


def _part(part_id: int, number: str, x: float = 0.0) -> Part:
    outer = Polygon(
        [(x, 0), (x + 400, 0), (x + 400, 300), (x, 300)]
    )
    hole = Polygon(
        [(x + 100, 100), (x + 150, 100), (x + 150, 150), (x + 100, 150)]
    )
    return Part(
        id=part_id,
        number=number,
        polygon=outer.difference(hole),
        outer_polygon=outer,
        holes=[hole],
        label_position=(x + 200, 150),
    )


def _result(parts: list[Part]) -> NestingResult:
    sheet = Sheet(index=1, width=1000, height=800, thickness=20, parts=parts)
    return NestingResult(
        sheets=[sheet],
        unit="metric",
        total_parts=len(parts),
        total_sheets=1,
        total_part_area=sum(part.area for part in parts),
        total_sheet_area=sheet.total_area,
    )


def _group_sizes(doc) -> dict[str, int]:
    return {name: len(list(group)) for name, group in doc.groups}


def _border_size(doc):
    for entity in doc.modelspace():
        if entity.dxf.layer == "SHEET_BORDER" and entity.dxftype() == "LWPOLYLINE":
            points = [(p[0], p[1]) for p in entity.get_points("xy")]
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return (max(xs) - min(xs), max(ys) - min(ys))
    return None


def test_nested_dxf_groups_bind_outer_hole_and_number(tmp_path):
    parts = [_part(1, "P-0001"), _part(2, "P-0002", x=500)]
    output = tmp_path / "nested.dxf"

    write_nested_dxf(_result(parts), str(output))

    groups = _group_sizes(ezdxf.readfile(output))
    assert groups == {"PART_P-0001": 3, "PART_P-0002": 3}


def test_nested_dxf_keeps_group_for_duplicate_numbers(tmp_path):
    parts = [_part(1, "P-0001"), _part(2, "P-0001", x=500)]
    output = tmp_path / "nested_dup.dxf"

    write_nested_dxf(_result(parts), str(output))

    groups = _group_sizes(ezdxf.readfile(output))
    assert groups == {"PART_P-0001": 3, "PART_P-0001_2": 3}


def test_numbered_parts_dxf_groups_are_not_empty(tmp_path):
    parts = [_part(1, "P-0001"), _part(2, "P-0002", x=500)]
    output = tmp_path / "numbered.dxf"

    write_numbered_parts_dxf(parts, str(output))

    groups = _group_sizes(ezdxf.readfile(output))
    assert groups == {"PART_P-0001": 3, "PART_P-0002": 3}


def test_nested_dxf_border_defaults_to_sheet_size(tmp_path):
    """未指定 board_size 时，大板外框沿用 sheet 尺寸。"""
    output = tmp_path / "nested_default.dxf"

    write_nested_dxf(_result([_part(1, "P-0001")]), str(output))

    assert _border_size(ezdxf.readfile(output)) == (1000.0, 800.0)


def test_nested_dxf_border_uses_board_size_not_packing_area(tmp_path):
    """排板可用区因刀缝预留收缩时，外框仍按大板本体尺寸绘制。

    回归：曾把收缩后的排板空间当成大板尺寸画外框（3185x1585 而非
    3200x1600），导致按图下料量错尺寸。
    """
    parts = [_part(1, "P-0001"), _part(2, "P-0002", x=500)]
    # sheet 是收缩后的排板可用区，本体是 1000x800
    sheet = Sheet(index=1, width=985, height=785, thickness=20, parts=parts)
    result = NestingResult(
        sheets=[sheet],
        unit="metric",
        total_parts=len(parts),
        total_sheets=1,
        total_part_area=sum(part.area for part in parts),
        total_sheet_area=sheet.total_area,
    )
    output = tmp_path / "nested_board.dxf"

    write_nested_dxf(result, str(output), board_size=(1000.0, 800.0))

    assert _border_size(ezdxf.readfile(output)) == (1000.0, 800.0)
