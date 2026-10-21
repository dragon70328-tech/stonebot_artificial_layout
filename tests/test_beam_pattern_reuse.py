# -*- coding: utf-8 -*-
"""板型复用（pattern reuse）回归测试。

背景：citizen ST-6 1543 件 / 148 张里只有 31 种配方、116 张与上一张完全
相同。逐张从零重算束搜索是主要耗时来源；复用必须做到：
1. 所有零件仍只放一次、位置合法；
2. 板数不劣于关闭复用时；
3. 低填充板型（贴边放不满的）不参与复用。
"""

from shapely.geometry import Polygon

from src.beam_nesting import beam_layout
from src.models import Part


def _part(part_id: int, width: float, height: float) -> Part:
    outer = Polygon([(0, 0), (width, 0), (width, height), (0, height)])
    return Part(
        id=part_id,
        number=f"P-{part_id:04d}",
        polygon=outer,
        outer_polygon=outer,
        holes=[],
        area=outer.area,
        label_position=(width / 2, height / 2),
    )


def _run(parts, sheet_w, sheet_h, **kwargs):
    stats: dict = {}
    sheets = beam_layout(
        parts, sheet_w, sheet_h, {},
        rotations=(0, 90, 180, 270), stats=stats, **kwargs)
    return sheets, stats


def test_reuse_keeps_every_part_once():
    parts = [_part(i, 800, 780) for i in range(1, 61)]
    sheets, stats = _run(parts, 3200, 1600, beam_width=60, pattern_reuse=4)
    placed = [pl.part.id for sheet in sheets for pl in sheet]
    assert sorted(placed) == [p.id for p in parts]
    assert stats["beam_reused_sheets"] > 0


def test_reuse_not_worse_than_no_reuse():
    parts = [_part(i, 800, 780) for i in range(1, 61)]
    with_reuse, stats = _run(parts, 3200, 1600, beam_width=60,
                             pattern_reuse=4)
    without, _ = _run(parts, 3200, 1600, beam_width=60, pattern_reuse=1)
    assert len(with_reuse) <= len(without)
    assert len(with_reuse) > 0


def test_low_fill_pattern_is_not_reused():
    # 2000x1500 单件填充率 586/1000 ≈ 58.6%，低于 80%，不复用
    parts = [_part(i, 2000, 1500) for i in range(1, 7)]
    sheets, stats = _run(parts, 3200, 1600, beam_width=30, pattern_reuse=4)
    assert len(sheets) == len(parts)
    assert stats.get("beam_reused_sheets", 0) == 0
