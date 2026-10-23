import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Polygon, box

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


def make_polygon_part(number: str, poly: Polygon) -> Part:
    return Part(
        id=abs(hash(number)) % 1_000_000,
        number=number,
        polygon=poly,
        outer_polygon=poly,
        holes=[],
        original_number=number,
        area=poly.area,
        label_position=(poly.centroid.x, poly.centroid.y),
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


def _rotated_rect_part(number: str, angle: float) -> Part:
    from shapely import affinity

    poly = affinity.rotate(box(0.0, 0.0, 400.0, 200.0), angle, origin=(0.0, 0.0))
    return Part(
        id=abs(hash(number)) % 1_000_000,
        number=number,
        polygon=poly,
        outer_polygon=poly,
        holes=[],
        original_number=number,
        area=poly.area,
        label_position=(200.0, 100.0),
    )


def test_edge_aligned_angles_for_axis_aligned_rectangle():
    from src.deepnest_engine import edge_aligned_angles

    angles = edge_aligned_angles(make_part("R", 400.0, 200.0))

    assert angles == [0.0, 90.0, 180.0, 270.0]


def test_edge_aligned_angles_follow_part_orientation():
    from src.deepnest_engine import edge_aligned_angles

    angles = edge_aligned_angles(_rotated_rect_part("R30", 30.0))

    # 30° 与 120° 两条边方向 -> 旋转 330°/60° 等可把长边摆平
    assert 60.0 in angles
    assert 330.0 in angles
    assert 240.0 in angles
    assert len(angles) < 10


def test_edge_aligned_angles_ignore_redundant_vertices():
    """CAD 冗余顶点（微段）不应被当成独立方向。"""
    from src.deepnest_engine import angle_candidates

    ring = [(0.0, 0.0), (400.0, 0.0), (400.0, 100.0), (400.0, 200.0),
            (0.0, 200.0), (0.0, 100.0)]
    from shapely.geometry import Polygon

    poly = Polygon(ring)
    part = Part(
        id=7, number="noisy", polygon=poly, outer_polygon=poly,
        holes=[], area=poly.area, label_position=(200.0, 100.0),
    )
    angles = angle_candidates((0, 90, 180, 270), True, 15.0, part=part)

    assert angles == [0.0, 90.0, 180.0, 270.0]


def test_angle_candidates_add_grid_fallback_for_curved_part():
    from shapely.geometry import Point

    from src.deepnest_engine import edge_aligned_angles

    circle = Point(0.0, 0.0).buffer(100.0, quad_segs=8)
    part = Part(
        id=9, number="circle", polygon=circle, outer_polygon=circle,
        holes=[], area=circle.area, label_position=(0.0, 0.0),
    )

    angles = angle_candidates((0, 90, 180, 270), True, 15.0, part=part)

    assert 45.0 in angles, "曲线件必须保留全角度兜底采样"
    assert len(angles) > len(edge_aligned_angles(part))


def test_angle_candidates_grid_mode_matches_legacy_step():
    angles = angle_candidates((0, 90), True, 15.0, part=make_part("G", 100.0, 50.0),
                              angle_mode="grid")

    assert len(angles) == 24
    assert 15.0 in angles


def test_low_fill_rework_never_increases_sheet_count():
    """低填充重排只在板数减少时接受，因此结果不可能变差。"""
    parts = [make_part(f"A{i}", 700.0, 300.0) for i in range(6)]
    kwargs = dict(
        rotations=(0, 90, 180, 270),
        arbitrary_rotation=False,
        trials=1,
        seed=0,
    )

    baseline = nest_parts_deepnest(parts, 1000.0, 1000.0, 20.0,
                                   low_fill_rework=False, **kwargs)
    reworked = nest_parts_deepnest(parts, 1000.0, 1000.0, 20.0,
                                   low_fill_rework=True, **kwargs)

    assert reworked.total_sheets <= baseline.total_sheets
    assert validate_nesting(reworked, 1000.0, 1000.0) == []


def test_placement_score_modes_are_available_and_distinct():
    from src.deepnest_engine import _placement_score

    bounds = (0.0, 0.0, 100.0, 50.0)
    boxes = [(0.0, 0.0, 100.0, 0.0)]

    assert _placement_score("bottom_left", bounds, boxes, boxes, 1000.0, 1000.0) == (0.0, 0.0)
    assert _placement_score("skyline", bounds, boxes, boxes, 1000.0, 1000.0) == (50.0, 0.0)
    assert _placement_score("column", bounds, boxes, boxes, 1000.0, 1000.0) == (100.0, 0.0)
    contact = _placement_score("contact", bounds, boxes, boxes, 1000.0, 1000.0)
    assert contact[0] <= 0  # 负的接触长度（越小越优）


def test_low_fill_rework_respects_explicit_disable():
    import inspect

    from src.deepnest_engine import nest_parts_deepnest

    signature = inspect.signature(nest_parts_deepnest)
    assert signature.parameters["low_fill_rework"].default is False
    assert signature.parameters["angle_mode"].default == "edge"


def test_deepnest_configs_drive_search_without_losing_parts():
    parts = [
        make_part("wide", 700.0, 300.0),
        make_part("tall", 300.0, 700.0),
        make_part("small", 250.0, 250.0),
    ]
    configs = [
        ("short", "skyline", 2, "sorted", False),
        ("long", "col", 3, "legacy", False),
    ]
    stats = {}

    result = nest_parts_deepnest(
        parts,
        1000.0,
        1000.0,
        20.0,
        configs=configs,
        improve_budget=0.0,
        stats=stats,
        rotations=(0, 90, 180, 270),
        arbitrary_rotation=False,
    )

    assert result.total_parts == len(parts)
    assert sum(len(sheet.parts) for sheet in result.sheets) == len(parts)
    assert validate_nesting(result, 1000.0, 1000.0) == []
    assert stats["deepnest_search_configs"] == 2
    assert stats["deepnest_candidate_mode"] == "bbox"


def test_low_fill_rework_repacks_bad_subset_and_keeps_all_parts():
    from src.deepnest_engine import _Placement, _improve_low_fill_sheets

    parts = [make_part(f"P{i}", 400.0, 400.0) for i in range(6)]
    sheets_pl = [
        [
            _Placement(parts[0], 0.0, box(0.0, 0.0, 400.0, 400.0)),
            _Placement(parts[1], 0.0, box(500.0, 0.0, 900.0, 400.0)),
        ],
        [
            _Placement(parts[2], 0.0, box(0.0, 0.0, 400.0, 400.0)),
            _Placement(parts[3], 0.0, box(500.0, 0.0, 900.0, 400.0)),
        ],
        [
            _Placement(parts[4], 0.0, box(0.0, 0.0, 400.0, 400.0)),
            _Placement(parts[5], 0.0, box(500.0, 0.0, 900.0, 400.0)),
        ],
    ]

    stats = {}
    improved = _improve_low_fill_sheets(
        sheets_pl,
        1000.0,
        1000.0,
        {},
        (0, 90, 180, 270),
        False,
        None,
        threshold=0.6,
        seed=0,
        configs=[
            ("area", "bottom_left", 0, "legacy", False),
            ("short", "skyline", 0, "sorted", False),
        ],
        improve_budget=0.0,
        stats=stats,
    )

    assert len(improved) == 2
    assert sum(len(sheet) for sheet in improved) == len(parts)
    assert stats["deepnest_low_fill_accepted_rounds"] >= 1


def test_nfp_lite_places_rectangle_inside_u_shaped_notch():
    u_shape = make_polygon_part(
        "U",
        Polygon([
            (0.0, 0.0),
            (800.0, 0.0),
            (800.0, 800.0),
            (500.0, 800.0),
            (500.0, 200.0),
            (300.0, 200.0),
            (300.0, 800.0),
            (0.0, 800.0),
        ]),
    )
    insert = make_part("insert", 200.0, 600.0)
    kwargs = dict(
        rotations=(0,),
        arbitrary_rotation=False,
        trials=1,
        seed=0,
    )

    baseline = nest_parts_deepnest(
        [u_shape, insert],
        800.0,
        800.0,
        20.0,
        candidate_mode="bbox",
        **kwargs,
    )
    nfp_lite = nest_parts_deepnest(
        [u_shape, insert],
        800.0,
        800.0,
        20.0,
        candidate_mode="nfp_lite",
        **kwargs,
    )

    assert baseline.total_sheets == 2
    assert nfp_lite.total_sheets == 1
    assert validate_nesting(nfp_lite, 800.0, 800.0) == []
