import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import box

from src.models import Part
from src.deepnest_engine import (
    angle_candidates,
    nest_parts_deepnest,
)
from src.nesting import validate_nesting


def make_part(number: str, width: float, height: float) -> Part:
    poly = box(0.0, 0.0, width, height)
    return Part(
        id=abs(hash(number)) % 1_000_000,
        number=number,
        polygon=poly,
        outer_polygon=poly,
        holes=[],
        original_number=number,
        area=poly.area,
        label_position=(width / 2.0, height / 2.0),
    )


def test_angle_candidates_use_discrete_rotations_when_not_arbitrary():
    assert angle_candidates((0, 90), False) == [0, 90]


def test_angle_candidates_include_non_right_angles_when_arbitrary():
    angles = angle_candidates((0, 90), True)
    assert 0.0 in angles
    assert 45.0 in angles
    assert 90.0 in angles
    assert 355.0 in angles
    assert len(angles) == 72


def test_deepnest_nests_simple_rectangles_and_validates():
    parts = [make_part(f"A{i}", 400.0, 600.0) for i in range(2)]
    result = nest_parts_deepnest(
        parts,
        1000.0,
        1000.0,
        20.0,
        rotations=(0, 90, 180, 270),
        arbitrary_rotation=False,
    )

    assert result.total_parts == 2
    assert result.total_sheets == 1
    assert validate_nesting(result, 1000.0, 1000.0) == []


def test_deepnest_handles_arbitrary_rotation_single_part():
    parts = [make_part("A", 400.0, 600.0)]
    result = nest_parts_deepnest(
        parts,
        1000.0,
        1000.0,
        20.0,
        rotations=(0, 90, 180, 270),
        arbitrary_rotation=True,
    )

    assert result.total_sheets == 1
    assert validate_nesting(result, 1000.0, 1000.0) == []


def test_deepnest_first_part_left_edge_applies_to_every_sheet():
    parts = [make_part(str(i), 800.0, 400.0) for i in range(4)]
    result = nest_parts_deepnest(
        parts,
        1000.0,
        1000.0,
        20.0,
        rotations=(0, 90, 180, 270),
        arbitrary_rotation=True,
        first_part_left_edge=True,
        rotation_step=15.0,
    )
    assert result.total_sheets == 2
    for sheet in result.sheets:
        minx, miny, maxx, maxy = sheet.parts[0].outer_polygon.bounds
        assert minx == 0.0
        assert miny == 0.0
        assert round(maxx - minx, 1) == 400.0
        assert round(maxy - miny, 1) == 800.0
