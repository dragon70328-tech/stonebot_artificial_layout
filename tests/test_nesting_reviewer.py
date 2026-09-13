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
