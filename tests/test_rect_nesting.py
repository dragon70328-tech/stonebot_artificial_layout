"""矩形快通道（src/rect_nesting.py）回归测试。"""

from shapely.geometry import Polygon

from src.models import Part
from src.rect_nesting import is_rect_like, rect_fast_layout


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
