from pathlib import Path

import numpy as np
from shapely.geometry import box

from src.drawing_profile import DrawingProfile
from src.models import NestingResult, Part, Sheet
from src.nesting import (
    _make_sort_key,
    _nest_single,
    _lns_improve,
    _overlap_indices,
    _Placement,
    nest_parts,
    validate_nesting,
)
import main as app


def test_overlap_indices_includes_candidates_touching_above():
    """候选在已放零件正上方（包围盒下边缘贴合）也必须进入精确检测。"""
    boxes_arr = np.asarray([(0.0, 0.0, 300.0, 400.0)])
    idxs = _overlap_indices((0.0, 400.0, 300.0, 800.0), boxes_arr, 0.0)
    assert idxs.tolist() == [0]
    idxs = _overlap_indices((0.0, 144.0, 300.0, 544.0), boxes_arr, 0.0)
    assert idxs.tolist() == [0]
    idxs = _overlap_indices((0.0, 400.5, 300.0, 800.5), boxes_arr, 0.0)
    assert idxs.tolist() == []


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


def test_validate_nesting_reports_min_gap_violation():
    left = _part("A", 20.0, 10.0)
    right = _part("B", 20.0, 10.0)
    left.outer_polygon = box(0.0, 0.0, 20.0, 10.0)
    right.outer_polygon = box(24.9, 0.0, 44.9, 10.0)
    sheet = Sheet(index=1, width=100.0, height=100.0, thickness=20.0,
                  parts=[left, right])
    result = NestingResult(
        sheets=[sheet], unit="metric", total_parts=2, total_sheets=1,
        total_part_area=200.0, total_sheet_area=10000.0,
    )

    errors = validate_nesting(result, 100.0, 100.0, min_gap=5.0)
    assert any("最小间距" in error for error in errors)


def test_validate_nesting_accepts_min_gap_when_separated():
    left = _part("A", 20.0, 10.0)
    right = _part("B", 20.0, 10.0)
    left.outer_polygon = box(0.0, 0.0, 20.0, 10.0)
    right.outer_polygon = box(25.0, 0.0, 45.0, 10.0)
    sheet = Sheet(index=1, width=100.0, height=100.0, thickness=20.0,
                  parts=[left, right])
    result = NestingResult(
        sheets=[sheet], unit="metric", total_parts=2, total_sheets=1,
        total_part_area=200.0, total_sheet_area=10000.0,
    )

    assert validate_nesting(result, 100.0, 100.0, min_gap=5.0) == []


def test_lns_improve_uses_warm_start_as_initial_solution():
    part = _part("A", 20.0, 10.0)
    current = [
        [_Placement(part=part, rot=0.0, x=0.0, y=0.0, poly=part.outer_polygon)]
    ]
    warm = [
        [_Placement(part=part, rot=90.0, x=0.0, y=0.0, poly=part.outer_polygon)]
    ]

    improved = _lns_improve(
        current,
        100.0,
        100.0,
        {},
        budget_s=0.0,
        seed=0,
        rotations=(0, 90, 180, 270),
        warm_start=warm,
    )

    assert improved is warm


def test_split_normal_parts_by_group_one_set_per_sheet():
    a = _part("A-1", 100.0, 50.0)
    b = _part("B-1", 100.0, 50.0)
    c = _part("A-2", 100.0, 50.0)
    a.group_id = "A"
    b.group_id = "B"
    c.group_id = "A"

    groups = app._split_normal_parts_by_group(
        [a, b, c], "one_set_per_sheet"
    )

    assert len(groups) == 2
    assert sorted(len(group) for group in groups) == [1, 2]


def test_split_normal_parts_returns_single_group_for_other_modes():
    parts = [_part(str(i), 100.0, 50.0) for i in range(3)]

    groups = app._split_normal_parts_by_group(parts, None)

    assert len(groups) == 1
    assert groups[0] == parts
