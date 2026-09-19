import ezdxf
from shapely.geometry import Polygon

from src.constraints import PROFILE_MIN_SHEETS
from src.dxf_writer import write_nested_dxf
from src.models import NestingResult, Part, Sheet
from src.nesting_reviewer import find_reductions, load_nested_dxf


def _rect(x, y, width, height, number):
    poly = Polygon([
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height),
    ])
    return Part(
        id=len(number),
        number=number,
        polygon=poly,
        outer_polygon=poly,
        area=poly.area,
        label_position=(x + width / 2, y + height / 2),
    )


def _l_part():
    return Polygon([
        (3230, 9.16),
        (3230, 1630),
        (2585.48, 1630),
        (2585.48, 653.69),
        (690.03, 653.69),
        (690.03, 9.16),
        (3230, 9.16),
    ])


def test_reviewer_moves_two_rectangles_into_larger_sheet():
    l_part = _l_part()
    target = Sheet(
        index=1,
        width=3230,
        height=1630,
        thickness=20,
        parts=[
            Part(
                id=1,
                number="F",
                polygon=l_part,
                outer_polygon=l_part,
                area=l_part.area,
            )
        ],
    )
    donor = Sheet(
        index=2,
        width=3200,
        height=1600,
        thickness=20,
        parts=[
            _rect(0, 0, 1606.55, 644.525, "B1"),
            _rect(0, 644.525, 1606.55, 644.525, "B2"),
        ],
    )
    result = NestingResult(
        sheets=[target, donor],
        unit="metric",
        total_parts=3,
        total_sheets=2,
        total_part_area=sum(
            part.area for sheet in [target, donor] for part in sheet.parts
        ),
        total_sheet_area=sum(sheet.total_area for sheet in [target, donor]),
    )

    candidates, reduced = find_reductions(
        result,
        profile=PROFILE_MIN_SHEETS,
        max_reductions=1,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.removed_sheet_index == 2
    assert set(candidate.moved_parts) == {"B1", "B2"}
    assert reduced.total_sheets == 1
    assert {part.number for part in reduced.sheets[0].parts} == {"F", "B1", "B2"}


def test_load_nested_dxf_roundtrip(tmp_path):
    parts = [
        _rect(0, 0, 800, 500, "A-1"),
        _rect(900, 0, 700, 400, "A-2"),
    ]
    sheet = Sheet(1, 2000, 1000, 20, parts)
    result = NestingResult(
        sheets=[sheet],
        unit="metric",
        total_parts=2,
        total_sheets=1,
        total_part_area=sum(part.area for part in parts),
        total_sheet_area=sheet.total_area,
    )
    path = tmp_path / "nested.dxf"
    write_nested_dxf(result, str(path))

    loaded = load_nested_dxf(path)

    assert loaded.total_sheets == 1
    assert loaded.total_parts == 2
    assert {part.number for part in loaded.sheets[0].parts} == {"A-1", "A-2"}

def test_load_nested_dxf_restores_material_group(tmp_path):
    parts = [
        _rect(0, 0, 800, 500, "04B-7-11"),
        _rect(900, 0, 700, 400, "04B-7-12"),
    ]
    sheet = Sheet(1, 2000, 1000, 20, parts)
    result = NestingResult(
        sheets=[sheet],
        unit="metric",
        total_parts=2,
        total_sheets=1,
        total_part_area=sum(part.area for part in parts),
        total_sheet_area=sheet.total_area,
    )
    path = tmp_path / "nested_group.dxf"
    write_nested_dxf(result, str(path))

    loaded = load_nested_dxf(path)

    groups = {part.material_group for part in loaded.sheets[0].parts}
    assert groups == {"04B"}


def _grouped(number, group, x, y, width, height):
    part = _rect(x, y, width, height, number)
    part.material_group = group
    return part


def test_reviewer_never_mixes_material_groups():
    # 目标板空位很多，但材料组不同；回看不能跨材料组并板。
    other_group_sheet = Sheet(
        index=1,
        width=3200,
        height=1600,
        thickness=20,
        parts=[_grouped("01B-1", "01B", 0, 0, 800, 800)],
    )
    donor = Sheet(
        index=2,
        width=3200,
        height=1600,
        thickness=20,
        parts=[_grouped("04B-1", "04B", 0, 0, 500, 500)],
    )
    sheets = [other_group_sheet, donor]
    result = NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=2,
        total_sheets=2,
        total_part_area=sum(part.area for sheet in sheets for part in sheet.parts),
        total_sheet_area=sum(sheet.total_area for sheet in sheets),
    )

    candidates, reduced = find_reductions(
        result,
        profile=PROFILE_MIN_SHEETS,
        max_reductions=1,
    )

    assert candidates == []
    assert reduced.total_sheets == 2


def test_reviewer_respects_min_gap_clearance():
    target = Sheet(
        index=1,
        width=1000,
        height=1000,
        thickness=20,
        parts=[_rect(0, 0, 500, 1000, "T")],
    )
    donor = Sheet(
        index=2,
        width=1000,
        height=1000,
        thickness=20,
        parts=[_rect(0, 0, 500, 1000, "D")],
    )
    result = NestingResult(
        sheets=[target, donor],
        unit="metric",
        total_parts=2,
        total_sheets=2,
        total_part_area=1000 * 1000,
        total_sheet_area=2 * 1000 * 1000,
    )

    no_gap, _ = find_reductions(
        result, profile=PROFILE_MIN_SHEETS, max_reductions=1, min_gap=0.0,
    )
    with_gap, _ = find_reductions(
        result, profile=PROFILE_MIN_SHEETS, max_reductions=1, min_gap=10.0,
    )

    assert len(no_gap) == 1
    assert with_gap == []


def test_numbered_dxf_marks_forced_closure_parts(tmp_path):
    from src.dxf_writer import write_numbered_parts_dxf

    part = _rect(0, 0, 800, 500, "02B-17")
    part.closure_gap = 704.25
    plain = _rect(900, 0, 700, 400, "02B-18")

    path = tmp_path / "numbered.dxf"
    write_numbered_parts_dxf([part, plain], str(path))

    doc = ezdxf.readfile(path)
    check = [e for e in doc.modelspace() if e.dxf.layer == "CHECK_CLOSURE"]
    outlines = [e for e in check if e.dxftype() == "LWPOLYLINE"]
    notes = [e for e in check if e.dxftype() == "TEXT"]

    assert len(outlines) == 1
    assert len(notes) == 1
    assert "缺口704mm" in notes[0].dxf.text
