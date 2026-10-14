"""P0（2026-10-14）：尾板择优键、LNS 确定性预算、--quick 不再降级束搜索宽度。

背景（实测）：CGR45 @3200x1800 的"50 张"依赖 束搜索(beam=1000) + 回看 两者都在；
`--quick` 会把 beam 静默降到 400 掉到 51，回看又是墙钟时间截断（不可复现）。
本文件锁定修好后的三条口径。
"""

import itertools

from shapely.geometry import box

import src.workflow as workflow
from src.constraints import NestingProfile
from src.models import NestingResult, Part
from src.nesting import (_Placement, _compress_sheets, _layout_key, _lns_improve)
from src.units import UnitSystem

_ids = itertools.count(1)


def _part(number, width, height):
    poly = box(0.0, 0.0, width, height)
    return Part(
        id=next(_ids),
        number=number,
        polygon=poly,
        outer_polygon=poly,
        holes=[],
        original_number=number,
        area=poly.area,
        label_position=(width / 2.0, height / 2.0),
    )


def _sheet(*areas):
    """一张板，按给定面积放若干个零件（面积 = 宽×高，取 1mm 宽便于凑面积）。"""
    return [
        _Placement(part=_part(f"P{i}", area, 1.0), rot=0.0, x=0.0, y=0.0,
                   poly=box(0.0, 0.0, area, 1.0))
        for i, area in enumerate(areas, start=1)
    ]


# --------------------------------------------------------------------------
# 1. 择优键：板数 → 最弱板可并性 → …
# --------------------------------------------------------------------------


def test_layout_key_prefers_fewer_sheets():
    one_sheet = [_sheet(500.0)]
    two_sheets = [_sheet(500.0), _sheet(500.0)]
    k1 = _layout_key(one_sheet, manufacture_score=0.0, compact=0.0)
    k2 = _layout_key(two_sheets, manufacture_score=0.0, compact=0.0)
    assert k1 < k2


def test_layout_key_prefers_lighter_weakest_sheet_when_sheet_count_ties():
    """板数相同 → 最弱板更轻的布局优先（更容易被回看并掉）。"""
    uneven = [_sheet(100.0), _sheet(900.0)]      # 最弱板 100
    balanced = [_sheet(500.0), _sheet(500.0)]    # 最弱板 500
    k_uneven = _layout_key(uneven, manufacture_score=0.0, compact=0.0)
    k_balanced = _layout_key(balanced, manufacture_score=0.0, compact=0.0)
    assert k_uneven[0] == k_balanced[0] == 2
    assert k_uneven[1] == 100.0 and k_balanced[1] == 500.0
    assert k_uneven < k_balanced


def test_layout_key_falls_back_to_manufacture_score_and_compactness():
    """尾板相同时，制造评分高者优先，其次紧凑度小者优先。"""
    layout = [_sheet(500.0), _sheet(500.0)]
    better = _layout_key(layout, manufacture_score=2.0, compact=10.0)
    worse = _layout_key(layout, manufacture_score=1.0, compact=2.0)
    assert better < worse
    assert (_layout_key(layout, manufacture_score=1.0, compact=1.0)
            < _layout_key(layout, manufacture_score=1.0, compact=2.0))


# --------------------------------------------------------------------------
# 2. LNS 确定性预算
# --------------------------------------------------------------------------


def _one_part_layout():
    part = _part("A", 20.0, 10.0)
    return [[_Placement(part=part, rot=0.0, x=0.0, y=0.0,
                        poly=part.outer_polygon)]]


def test_lns_improve_stops_at_iteration_cap_and_reports_stats():
    stats: dict = {}
    _lns_improve(
        _one_part_layout(), 100.0, 100.0, {},
        budget_s=None, seed=0, max_iterations=3, patience=None, stats=stats,
    )
    assert stats["lns_iterations"] == 3
    assert stats["lns_budget_exhausted"] is False
    assert stats["lns_max_iterations"] == 3


def test_lns_improve_stops_after_patience_dry_rounds():
    """确定性早停：连续无改进达到 patience 就收手（单件布局必然无改进）。"""
    stats: dict = {}
    _lns_improve(
        _one_part_layout(), 100.0, 100.0, {},
        budget_s=None, seed=0, max_iterations=100, patience=2, stats=stats,
    )
    assert stats["lns_iterations"] == 2
    assert stats["lns_dry_rounds"] == 2
    assert stats["lns_budget_exhausted"] is False


def test_lns_improve_flags_wall_clock_guardrail(monkeypatch):
    """墙钟护栏触发时必须标记，报告才能说明"本次结果不可复现"。"""
    import src.nesting as nesting_mod

    class _Clock:
        """每次读表前进 10s，避免依赖平台时钟粒度。"""

        def __init__(self):
            self.now = 0.0

        def time(self):
            self.now += 10.0
            return self.now

    monkeypatch.setattr(nesting_mod, "time", _Clock())
    stats: dict = {}
    _lns_improve(
        _one_part_layout(), 100.0, 100.0, {},
        budget_s=1e-9, seed=0, max_iterations=None, patience=None, stats=stats,
    )
    assert stats["lns_iterations"] == 0
    assert stats["lns_budget_exhausted"] is True


def test_lns_improve_zero_budget_means_no_wall_clock_limit():
    """budget_s=0/None 表示"不设墙钟上限"，由迭代数与 patience 决定轮数。"""
    stats: dict = {}
    _lns_improve(
        _one_part_layout(), 100.0, 100.0, {},
        budget_s=0.0, seed=0, max_iterations=2, patience=None, stats=stats,
    )
    assert stats["lns_iterations"] == 2
    assert stats["lns_budget_exhausted"] is False


# --------------------------------------------------------------------------
# 3. --quick 不再改变束搜索宽度
# --------------------------------------------------------------------------


def test_quick_flag_keeps_full_beam_width(monkeypatch):
    """--quick 只该影响配置数与预算，不该把 beam 从 1000 静默降到 400。"""
    captured: dict = {}

    def spy(parts, width, height, thickness, **kwargs):
        captured.update(kwargs)
        sheet = workflow.Sheet(index=1, width=width, height=height, thickness=thickness)
        return NestingResult(
            sheets=[sheet], unit="metric", total_parts=0, total_sheets=1,
            total_part_area=0.0, total_sheet_area=width * height,
        )

    monkeypatch.setattr(workflow, "nest_parts", spy)
    profile = NestingProfile()
    workflow._nest_one_group(
        [_part("A", 100.0, 50.0)], 1000.0, 800.0, None, None,
        20.0, UnitSystem.METRIC, profile, None, 1, 0, 60.0, True,
        beam=True, beam_width=None,
    )
    assert captured["beam_width"] == 1000


def test_explicit_beam_width_still_wins(monkeypatch):
    captured: dict = {}

    def spy(parts, width, height, thickness, **kwargs):
        captured.update(kwargs)
        sheet = workflow.Sheet(index=1, width=width, height=height, thickness=thickness)
        return NestingResult(
            sheets=[sheet], unit="metric", total_parts=0, total_sheets=1,
            total_part_area=0.0, total_sheet_area=width * height,
        )

    monkeypatch.setattr(workflow, "nest_parts", spy)
    workflow._nest_one_group(
        [_part("A", 100.0, 50.0)], 1000.0, 800.0, None, None,
        20.0, UnitSystem.METRIC, NestingProfile(), None, 1, 0, 60.0, False,
        beam=True, beam_width=250,
    )
    assert captured["beam_width"] == 250


# --------------------------------------------------------------------------
# 4. P1：多板联合重排压缩（把 k 张板重排进 k-1 张）
# --------------------------------------------------------------------------


def test_compress_sheets_merges_two_sheets_into_one():
    """两张各放一件的板，应能硬性重排进一张（max_sheets=k-1）。"""
    layout = [
        [_Placement(part=_part("A", 400.0, 400.0), rot=0.0, x=0.0, y=0.0,
                    poly=box(0.0, 0.0, 400.0, 400.0))],
        [_Placement(part=_part("B", 400.0, 400.0), rot=0.0, x=0.0, y=0.0,
                    poly=box(0.0, 0.0, 400.0, 400.0))],
    ]
    stats: dict = {}
    merged = _compress_sheets(layout, 1000.0, 1000.0, {},
                             subset_sizes=(2,), stats=stats)
    assert len(merged) == 1
    assert sum(len(s) for s in merged) == 2
    assert stats["compress_accepted"] == 1


def test_compress_sheets_skips_subsets_that_cannot_fit_by_area():
    """面积算术上不可能的 k 直接跳过（CGR45 上 k=2 需 135% 填充）。"""
    layout = [
        [_Placement(part=_part("A", 900.0, 900.0), rot=0.0, x=0.0, y=0.0,
                    poly=box(0.0, 0.0, 900.0, 900.0))],
        [_Placement(part=_part("B", 900.0, 900.0), rot=0.0, x=0.0, y=0.0,
                    poly=box(0.0, 0.0, 900.0, 900.0))],
    ]
    stats: dict = {}
    merged = _compress_sheets(layout, 1000.0, 1000.0, {},
                             subset_sizes=(2,), stats=stats)
    assert len(merged) == 2
    assert stats["compress_attempts"] == 0
    assert stats["compress_skipped_by_area"] >= 1


def test_compress_sheets_is_skipped_under_first_part_left_edge():
    """first_part_left_edge 约束下必须跳过（重排会破坏首件贴左边缘）。"""
    layout = [
        [_Placement(part=_part("A", 400.0, 400.0), rot=0.0, x=0.0, y=0.0,
                    poly=box(0.0, 0.0, 400.0, 400.0))],
        [_Placement(part=_part("B", 400.0, 400.0), rot=0.0, x=0.0, y=0.0,
                    poly=box(0.0, 0.0, 400.0, 400.0))],
    ]
    stats: dict = {}
    merged = _compress_sheets(layout, 1000.0, 1000.0, {},
                             subset_sizes=(2,), first_part_left_edge=True,
                             stats=stats)
    assert len(merged) == 2
    assert stats["compress_left_edge_skipped"] is True
