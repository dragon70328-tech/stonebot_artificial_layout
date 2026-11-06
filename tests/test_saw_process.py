import math

import pytest
from shapely.geometry import Polygon, box

from src.models import Part, Sheet
from src.nesting import nest_parts
from src.saw_process import (
    calculate_saw_clearance,
    diagnose_saw_access,
    saw_ranking_key,
)


def _part(number: str, polygon: Polygon) -> Part:
    return Part(
        id=abs(hash(number)) % 1_000_000,
        number=number,
        polygon=polygon,
        outer_polygon=polygon,
        holes=[],
        original_number=number,
        area=polygon.area,
    )


def _l_shape(x: float, y: float, scale: float = 1.0) -> Polygon:
    return Polygon([
        (x, y),
        (x + 400 * scale, y),
        (x + 400 * scale, y + 100 * scale),
        (x + 100 * scale, y + 100 * scale),
        (x + 100 * scale, y + 400 * scale),
        (x, y + 400 * scale),
    ])


def test_saw_clearance_uses_half_chord_not_blade_radius():
    result = calculate_saw_clearance(350, 20)

    assert result.plunge_depth_mm == 22
    assert result.theoretical_clearance_mm == pytest.approx(
        math.sqrt(350 * 22 - 22 * 22)
    )
    assert result.clearance_mm == pytest.approx(84.947042, rel=1e-6)
    assert result.clearance_mm < 175


def test_saw_clearance_adds_optional_safety_margin():
    result = calculate_saw_clearance(
        350,
        20,
        plunge_extra_mm=2,
        safety_margin_mm=5,
    )

    assert result.clearance_mm == pytest.approx(
        result.theoretical_clearance_mm + 5
    )


def test_saw_clearance_rejects_impossible_geometry():
    with pytest.raises(ValueError, match="diameter must exceed"):
        calculate_saw_clearance(20, 20)


def test_rectangles_have_no_reflex_access_points():
    sheet = Sheet(
        index=1,
        width=1000,
        height=1000,
        thickness=20,
        parts=[_part("A", box(0, 0, 400, 400))],
    )

    summary = diagnose_saw_access([sheet], 85)

    assert summary.points_checked == 0
    assert summary.points_insufficient == 0
    assert summary.all_compliant


def test_no_internal_access_points_outranks_an_unnecessary_access_point():
    rectangle = Sheet(
        index=1,
        width=1000,
        height=1000,
        thickness=20,
        parts=[_part("A", box(0, 0, 400, 400))],
    )
    l_shape = Sheet(
        index=1,
        width=1000,
        height=1000,
        thickness=20,
        parts=[_part("A", _l_shape(0, 0))],
    )

    assert saw_ranking_key([rectangle], 85) < saw_ranking_key([l_shape], 85)


def test_reflex_vertex_with_narrow_neighbor_is_reported():
    first = _l_shape(0, 0)
    blocker = box(120, 120, 150, 150)
    sheet = Sheet(
        index=1,
        width=1200,
        height=1000,
        thickness=20,
        parts=[_part("A", first), _part("B", blocker)],
    )

    summary = diagnose_saw_access([sheet], 85)

    assert summary.points_checked >= 1
    assert summary.points_insufficient >= 1
    assert any(
        warning["type"] == "saw_clearance_insufficient"
        for warning in summary.warnings
    )


def test_reflex_vertex_with_wide_channel_passes():
    first = _l_shape(0, 0)
    second = _l_shape(700, 0)
    sheet = Sheet(
        index=1,
        width=1600,
        height=1000,
        thickness=20,
        parts=[_part("A", first), _part("B", second)],
    )

    summary = diagnose_saw_access([sheet], 85)

    assert summary.points_checked >= 1
    assert summary.points_insufficient == 0


def test_saw_ranking_prefers_more_compliant_points():
    narrow = Sheet(
        index=1,
        width=1200,
        height=1000,
        thickness=20,
        parts=[
            _part("A", _l_shape(0, 0)),
            _part("B", box(120, 120, 150, 150)),
        ],
    )
    wide = Sheet(
        index=1,
        width=1600,
        height=1000,
        thickness=20,
        parts=[_part("A", _l_shape(0, 0)), _part("B", _l_shape(700, 0))],
    )

    assert saw_ranking_key([wide], 85) < saw_ranking_key([narrow], 85)


def test_nest_parts_accepts_saw_clearance_without_losing_parts():
    first = _l_shape(0, 0)
    second = _l_shape(500, 0)
    result = nest_parts(
        [_part("A", first), _part("B", second)],
        1200,
        1000,
        20,
        rotations=(0,),
        beam=False,
        improve_budget=0,
        rect_fast=False,
        compress=False,
        saw_clearance_mm=84.947,
    )

    assert result.total_sheets == 1
    assert result.total_parts == 2
