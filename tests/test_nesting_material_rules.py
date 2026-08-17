from pathlib import Path

from shapely.geometry import box

from src.drawing_profile import DrawingProfile
from src.models import Part
from src.nesting import _make_sort_key, _nest_single, nest_parts
import main as app


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


def test_first_part_left_edge_rotates_longest_edge_vertical():
    part = _part("A", 800.0, 400.0)
    result = nest_parts(
        [part],
        1000.0,
        1000.0,
        20.0,
        configs=[("area", "skyline", 0)],
        improve_budget=0.0,
        rotations=(0, 90, 180, 270),
        first_part_left_edge=True,
    )
    placed = result.sheets[0].parts[0]
    minx, miny, maxx, maxy = placed.outer_polygon.bounds
    assert minx == 0.0
    assert miny == 0.0
    assert round(maxx - minx, 1) == 400.0
    assert round(maxy - miny, 1) == 800.0


def test_material_grouping_skips_non_allowed_prefix():
    profile = DrawingProfile(
        name="test",
        version="1.0.0",
        material_group_enabled=True,
        allowed_material_prefixes=["01B", "02B"],
    )
    parts = [_part("01B-1", 100.0, 50.0), _part("P-1", 100.0, 50.0)]
    kept, skipped = app.apply_material_grouping(parts, profile)
    assert len(kept) == 1
    assert kept[0].material_group == "01B"
    assert skipped == ["P-1"]


def test_first_part_left_edge_applies_to_every_sheet():
    parts = [_part(str(i), 60.0, 40.0) for i in range(4)]
    cache = {}
    sheets = _nest_single(
        parts,
        100.0,
        100.0,
        _make_sort_key("area", 0),
        "skyline",
        cache,
        rotations=(0, 90, 180, 270),
        first_part_left_edge=True,
    )
    assert len(sheets) == 2
    for sheet in sheets:
        first = sheet[0]
        minx, miny, maxx, maxy = first.poly.bounds
        assert minx == 0.0
        assert miny == 0.0
        assert round(maxy - miny, 1) == 60.0
        assert round(maxx - minx, 1) == 40.0
