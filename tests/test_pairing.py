from shapely.geometry import box

from src.models import Part
from src.pairing import build_pairing_units, nest_parts_deepnest_paired
from src.nesting import validate_nesting


def _part(number, width, height):
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


def test_build_pairing_units_pairs_identical_shapes():
    parts = [_part(str(i), 800.0, 400.0) for i in range(4)]
    units = build_pairing_units(parts)
    assert len(units) == 2
    assert all(len(unit.local_parts) == 2 for unit in units)


def test_paired_deepnest_validates_and_keeps_first_part_left():
    parts = [_part(str(i), 800.0, 400.0) for i in range(4)]
    result = nest_parts_deepnest_paired(
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
    assert validate_nesting(result, 1000.0, 1000.0) == []
    for sheet in result.sheets:
        minx, miny, maxx, maxy = sheet.parts[0].outer_polygon.bounds
        assert minx == 0.0
        assert miny == 0.0
        assert round(maxx - minx, 1) == 400.0
        assert round(maxy - miny, 1) == 800.0
