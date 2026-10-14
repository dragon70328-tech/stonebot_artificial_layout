"""异形（水刀）路径的外轮廓鲁棒性。

2026-10-14 实图缺陷：东莞奥特莱斯2 有 65 件强制闭合轮廓，外轮廓是
Polygon 的复合体（MultiPolygon/GeometryCollection）时，
`deepnest_engine._edge_segments` 直接取 `poly.exterior` 会抛
`AttributeError: 'MultiPolygon' object has no attribute 'exterior'`，
`--process waterjet --pairing` 在这张图上必崩。
"""

import itertools

from shapely.geometry import MultiPolygon, box

from src.deepnest_engine import angle_candidates, edge_aligned_angles
from src.models import Part

_ids = itertools.count(1)


def _part(number, geometry):
    return Part(
        id=next(_ids),
        number=number,
        polygon=geometry,
        outer_polygon=geometry,
        holes=[],
        original_number=number,
        area=geometry.area,
        label_position=(30.0, 10.0),
    )


def test_edge_aligned_angles_accept_multipolygon_outer():
    """强制闭合/复合轮廓也要能算出方向候选，而不是抛异常。"""
    multi = MultiPolygon([box(0.0, 0.0, 60.0, 20.0), box(0.0, 30.0, 40.0, 50.0)])
    part = _part("MP-1", multi)

    edges = edge_aligned_angles(part)
    assert edges, "MultiPolygon 也应产出边对齐角度"

    angles = angle_candidates((0, 90, 180, 270), True, part=part)
    assert 0.0 in angles


def test_edge_aligned_angles_accept_geometry_collection_outer():
    """GeometryCollection（含点/线残留）不能把整条路径带崩。"""
    from shapely.geometry import GeometryCollection, Point

    geom = GeometryCollection([box(0.0, 0.0, 80.0, 30.0), Point(500.0, 500.0)])
    part = _part("GC-1", geom)

    angles = angle_candidates((0, 90, 180, 270), True, part=part)
    assert 0.0 in angles
