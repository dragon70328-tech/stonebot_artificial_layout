"""矩形快通道（src/rect_nesting.py）回归测试。"""

from shapely.geometry import Polygon

from src.constraints import PROFILE_MIN_SHEETS
from src.models import Part
from src.nesting import _materialize, nest_parts
from src.rect_nesting import (
    is_rect_like,
    rect_fast_layout,
    rectangles_are_guillotine,
)
from src.workflow import postprocess_result


def _part(part_id: int, width: float, height: float, x: float = 0.0,
          y: float = 0.0) -> Part:
    outer = Polygon(
        [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    )
    return Part(
        id=part_id,
        number=f"P-{part_id:04d}",
        polygon=outer,
        outer_polygon=outer,
        holes=[],
        area=outer.area,
        label_position=(x + width / 2, y + height / 2),
    )


def test_is_rect_like():
    assert is_rect_like(_part(1, 400, 300))
    # L 形：外包络填充率明显不足 0.98
    l_shape = Polygon([(0, 0), (400, 0), (400, 150), (200, 150),
                       (200, 300), (0, 300)])
    part = Part(id=2, number="P-0002", polygon=l_shape,
                outer_polygon=l_shape, holes=[], area=l_shape.area,
                label_position=(100, 100))
    assert not is_rect_like(part)
    # 圆：填充率 0.785
    circle = Polygon(
        [(100 + 100 * __import__("math").cos(a), 100 + 100 * __import__("math").sin(a))
         for a in [i * 2 * __import__("math").pi / 64 for i in range(64)]]
    )
    round_part = Part(id=3, number="P-0003", polygon=circle,
                      outer_polygon=circle, holes=[], area=circle.area,
                      label_position=(100, 100))
    assert not is_rect_like(round_part)


def test_rect_layout_places_every_part_once_without_overlap():
    parts = (
        [_part(i, 400, 300) for i in range(1, 6)]
        + [_part(100 + i, 250, 300) for i in range(1, 7)]
    )
    sheets = rect_fast_layout(parts, 1000, 600, rotations=(0, 90, 180, 270))
    assert sheets is not None

    seen = []
    for placements in sheets:
        polys = []
        for pl in placements:
            min_x, min_y, max_x, max_y = pl.poly.bounds
            assert min_x >= -1e-6 and min_y >= -1e-6
            assert max_x <= 1000 + 1e-6 and max_y <= 600 + 1e-6
            seen.append(pl.part.id)
            polys.append(pl.poly)
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                assert polys[i].intersection(polys[j]).area <= 1e-6

    assert sorted(seen) == sorted(part.id for part in parts)


def test_rect_layout_rejects_non_zero_gap_and_round_parts():
    parts = [_part(i, 400, 300) for i in range(1, 4)]
    assert rect_fast_layout(parts, 1000, 600, min_gap=5.0) is None

    circle = Polygon(
        [(100 + 50 * __import__("math").cos(a), 100 + 50 * __import__("math").sin(a))
         for a in [i * 2 * __import__("math").pi / 32 for i in range(32)]]
    )
    round_part = Part(id=99, number="P-0099", polygon=circle,
                      outer_polygon=circle, holes=[], area=circle.area,
                      label_position=(100, 100))
    assert rect_fast_layout([parts[0], round_part], 1000, 600) is None


def test_guillotine_validator_accepts_grid_and_rejects_pinwheel():
    grid = [
        (0, 0, 50, 50),
        (50, 0, 50, 50),
        (0, 50, 50, 50),
        (50, 50, 50, 50),
    ]
    pinwheel = [
        (0, 0, 60, 40),
        (60, 0, 40, 60),
        (40, 60, 60, 40),
        (0, 40, 40, 60),
    ]

    assert rectangles_are_guillotine(grid) is True
    assert rectangles_are_guillotine(pinwheel) is False


def test_rect_fast_layout_is_guillotine_cuttable():
    parts = [
        _part(1, 400, 300),
        _part(2, 400, 300),
        _part(3, 300, 300),
        _part(4, 300, 300),
        _part(5, 200, 150),
    ]

    sheets = rect_fast_layout(
        parts,
        1000,
        600,
        rotations=(0, 90),
    )

    assert sheets is not None
    for sheet in sheets:
        rectangles = [
            (
                placement.poly.bounds[0],
                placement.poly.bounds[1],
                placement.poly.bounds[2] - placement.poly.bounds[0],
                placement.poly.bounds[3] - placement.poly.bounds[1],
            )
            for placement in sheet
        ]
        assert rectangles_are_guillotine(rectangles)


def test_fixed_orientation_rect_fast_path_preserves_source_dimensions():
    dimensions = [
        (700.0, 300.0),
        (500.0, 300.0),
        (400.0, 250.0),
        (300.0, 250.0),
        (200.0, 150.0),
        (200.0, 150.0),
    ]
    parts = [
        _part(index, width, height)
        for index, (width, height) in enumerate(dimensions, start=1)
    ]

    sheets = rect_fast_layout(
        parts,
        1000.0,
        600.0,
        rotations=(0,),
    )

    assert sheets is not None
    by_id = {part.id: part for part in parts}
    seen = []
    for sheet in sheets:
        for placement in sheet:
            assert placement.rot == 0.0
            source = by_id[placement.part.id]
            source_bounds = source.outer_polygon.bounds
            placed_bounds = placement.poly.bounds
            assert abs(
                (placed_bounds[2] - placed_bounds[0])
                - (source_bounds[2] - source_bounds[0])
            ) < 1e-6
            assert abs(
                (placed_bounds[3] - placed_bounds[1])
                - (source_bounds[3] - source_bounds[1])
            ) < 1e-6
            seen.append(placement.part.id)
    assert sorted(seen) == sorted(by_id)


def test_nest_stats_report_fixed_orientation_and_cut_sequence():
    parts = [
        _part(index, width, height)
        for index, (width, height) in enumerate(
            [
                (700.0, 300.0),
                (500.0, 300.0),
                (400.0, 250.0),
                (300.0, 250.0),
            ],
            start=1,
        )
    ]
    stats = {}

    result = nest_parts(
        parts,
        1000.0,
        600.0,
        20.0,
        rotations=(0,),
        rect_fast=True,
        stats=stats,
        improve_budget=0.0,
        beam=False,
        compress=False,
    )

    assert result.total_parts == len(parts)
    assert stats["rect_fast_path"] is True
    assert stats["rect_fast_fixed_orientation"] is True
    assert stats["guillotine_cuttable"] in {True, False}
    assert stats["non_guillotine_sheets"] >= 0


def test_rect_fast_competes_with_beam_when_enabled():
    parts = [
        _part(index, width, height)
        for index, (width, height) in enumerate(
            [
                (1500.0, 600.0),
                (1500.0, 600.0),
                (800.0, 500.0),
                (800.0, 500.0),
                (700.0, 500.0),
                (700.0, 500.0),
            ],
            start=1,
        )
    ]
    stats = {}

    result = nest_parts(
        parts,
        3200.0,
        1600.0,
        20.0,
        rotations=(0,),
        rect_fast=True,
        beam=True,
        beam_width=100,
        improve_budget=0.0,
        compress=False,
        stats=stats,
    )

    assert result.total_parts == len(parts)
    assert stats["rect_fast_path"] is True
    assert stats["beam_competed_with_rect_fast"] is True
    assert stats["beam_sheets"] >= 1
    assert stats["selected_layout_path"] in {
        "rect_fast",
        "beam",
        "pattern_integer",
    }


def test_postprocess_reports_final_cut_sequence_metrics():
    parts = [
        _part(1, 500, 300),
        _part(2, 500, 300),
        _part(3, 400, 250),
        _part(4, 400, 250),
    ]
    placements = rect_fast_layout(
        parts,
        1000.0,
        600.0,
        rotations=(0,),
    )
    assert placements is not None
    result = _materialize(
        placements,
        1000.0,
        600.0,
        20.0,
        "metric",
        len(parts),
    )

    metrics = postprocess_result(result, PROFILE_MIN_SHEETS)

    assert metrics["guillotine_cuttable"] is True
    assert metrics["non_guillotine_sheets"] == 0
