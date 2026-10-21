# -*- coding: utf-8 -*-
"""束搜索预算截断的完整性回归测试（2026-10-21 citizen ST-6 丢件缺陷）。

缺陷经过：nest_parts 给 beam_layout 传了 time_budget_s 却没传
greedy_fallback，_beam_pass 在预算处直接 break，未排零件被静默丢弃；
部分布局按板数评分反而"最优"，最终 1543 件只排出 176 件。
"""

from shapely.geometry import Polygon

import pytest

from src.beam_nesting import beam_layout
from src.models import Part
from src.nesting import _make_sort_key, _nest_single, nest_parts


def _part(part_id: int, width: float, height: float) -> Part:
    outer = Polygon(
        [(0, 0), (width, 0), (width, height), (0, height)]
    )
    return Part(
        id=part_id,
        number=f"P-{part_id:04d}",
        polygon=outer,
        outer_polygon=outer,
        holes=[],
        area=outer.area,
        label_position=(width / 2, height / 2),
    )


# 每张 1000x600 的板只能放 1 件 900x500：30 件必须 30 张，
# 束搜索在极小预算下第一张板之后就必然触发预算截断。
SHEET_W, SHEET_H = 1000.0, 600.0
PARTS = [_part(i, 900, 500) for i in range(1, 31)]


def _greedy_fallback(remaining):
    return _nest_single(
        remaining, SHEET_W, SHEET_H,
        _make_sort_key("area", 0), "skyline", {},
        rotations=(0, 90, 180, 270),
    )


def _placed_ids(sheets):
    return [pl.part.id for sheet in sheets for pl in sheet]


def test_beam_over_budget_with_fallback_places_every_part():
    sheets = beam_layout(
        PARTS, SHEET_W, SHEET_H, {},
        beam_width=50, rotations=(0, 90, 180, 270),
        time_budget_s=0.001, greedy_fallback=_greedy_fallback,
    )
    placed = _placed_ids(sheets)
    assert sorted(placed) == [part.id for part in PARTS]


def test_beam_over_budget_without_fallback_returns_valid_prefix():
    """契约记录：没有 fallback 时，束搜索可能只返回前缀（部分零件）。

    2026-10-21 加入板型复用后，这个极小的夹具已经能在预算内排完，
    所以这里只校验"返回的永远是合法前缀"，不强制必须截断；
    完整性由 nest_parts 的 fallback + 件数校验保证。
    """
    sheets = beam_layout(
        PARTS, SHEET_W, SHEET_H, {},
        beam_width=50, rotations=(0, 90, 180, 270),
        time_budget_s=0.001,
    )
    placed = _placed_ids(sheets)
    assert 0 < len(placed) <= len(PARTS)
    assert len(set(placed)) == len(placed)


def test_nest_parts_over_budget_is_complete():
    """端到端回归：nest_parts 在束搜索预算极小时也必须输出全部零件。"""
    result = nest_parts(
        PARTS, SHEET_W, SHEET_H, 20.0,
        configs=[("area", "skyline", 0)],
        improve_budget=0.001, trials=1, seed=0,
        beam=True, beam_width=50,
        rect_fast=False,
    )
    placed = [part.number for sheet in result.sheets for part in sheet.parts]
    assert sorted(placed) == sorted(part.number for part in PARTS)
