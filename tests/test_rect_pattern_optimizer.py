from __future__ import annotations

import pytest
from shapely import affinity
from shapely.geometry import Polygon

from src.models import Part
from src.rect_nesting import RectPlacement

pytest.importorskip("highspy")

from src.rect_pattern_optimizer import optimize_pattern_counts


def _part(part_id: int) -> Part:
    polygon = Polygon([(0, 0), (300, 0), (300, 300), (0, 300)])
    return Part(
        id=part_id,
        number=f"P-{part_id}",
        polygon=polygon,
        outer_polygon=polygon,
        holes=[],
        area=polygon.area,
        label_position=(150, 150),
    )


def _placement(part: Part, x: float, y: float) -> RectPlacement:
    return RectPlacement(
        part=part,
        rot=0.0,
        x=x,
        y=y,
        poly=affinity.translate(part.outer_polygon, xoff=x, yoff=y),
    )


def test_pattern_integer_model_selects_fewer_repeated_sheets():
    parts = [_part(index) for index in range(1, 5)]
    two_piece_sheets = [
        [_placement(parts[0], 0, 0), _placement(parts[1], 300, 0)],
        [_placement(parts[2], 0, 0), _placement(parts[3], 300, 0)],
    ]
    four_piece_sheet = [
        [
            _placement(parts[0], 0, 0),
            _placement(parts[1], 300, 0),
            _placement(parts[2], 0, 300),
            _placement(parts[3], 300, 300),
        ]
    ]

    optimized = optimize_pattern_counts(
        parts,
        [two_piece_sheets, four_piece_sheet],
        time_limit_s=5.0,
    )

    assert optimized is not None
    assert optimized.objective_sheets == 1
    assert len(optimized.placements) == 1
    assert sorted(
        placement.part.id
        for placement in optimized.placements[0]
    ) == [1, 2, 3, 4]
