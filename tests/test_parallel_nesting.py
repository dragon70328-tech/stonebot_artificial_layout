# -*- coding: utf-8 -*-
"""并行排板模块回归测试（不依赖真实多进程）。"""

from shapely.geometry import Polygon

from src.models import Part
from src.parallel_nesting import build_result, stratified_split


def _part(i, w, h):
    poly = Polygon([(0, 0), (w, 0), (w, h), (0, h)])
    return Part(id=i, number="P-%04d" % i, polygon=poly, outer_polygon=poly,
                holes=[], area=poly.area, label_position=(w / 2, h / 2))


def test_stratified_split_keeps_class_mix():
    parts = ([_part(i, 800, 600) for i in range(1, 31)]
             + [_part(100 + i, 1600, 622) for i in range(1, 11)])
    batches = stratified_split(parts, 3)
    assert len(batches) == 3
    assert sum(len(b) for b in batches) == len(parts)
    # 每批都同时拿到两种规格（按图纸顺序切会失衡）
    for batch in batches:
        sizes = {(round(max(p.outer_polygon.bounds[2] - p.outer_polygon.bounds[0],
                            p.outer_polygon.bounds[3] - p.outer_polygon.bounds[1]), 1))
                 for p in batch}
        assert len(sizes) >= 2


def test_stratified_split_single_batch():
    parts = [_part(i, 800, 600) for i in range(1, 6)]
    assert len(stratified_split(parts, 1)) == 1


def test_build_result_reindexes_sheets():
    from src.models import NestingResult, Sheet
    s1 = Sheet(index=7, width=100, height=100, thickness=20,
               parts=[_part(1, 10, 10)])
    s2 = Sheet(index=9, width=100, height=100, thickness=20,
               parts=[_part(2, 10, 10)])
    res = build_result([s1, s2], "metric")
    assert [s.index for s in res.sheets] == [1, 2]
    assert res.total_parts == 2
    assert res.total_sheets == 2