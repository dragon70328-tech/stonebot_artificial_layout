"""逐板最佳填充束搜索（src/beam_nesting.py）的单元测试。"""
import pytest
from shapely.geometry import Polygon

from src.beam_nesting import beam_layout, _beam_pack_multi, _build_tiers
from src.models import Part
from src.nesting import nest_parts, validate_nesting, _materialize


def _rect(num, w, h):
    poly = Polygon([(0, 0), (w, 0), (w, h), (0, h)])
    return Part(id=num, number=f"P{num}", polygon=poly, outer_polygon=poly,
                holes=[], area=poly.area,
                label_position=(w / 2.0, h / 2.0))


def _l_part(num, notch_corner="tr"):
    """1000x1000 的 L 形件，缺口 500x500 在指定角。"""
    if notch_corner == "tr":
        coords = [(0, 0), (1000, 0), (1000, 500), (500, 500),
                  (500, 1000), (0, 1000)]
    else:
        coords = [(0, 0), (1000, 0), (1000, 1000), (500, 1000),
                  (500, 500), (0, 500)]
    poly = Polygon(coords)
    return Part(id=num, number=f"L{num}", polygon=poly, outer_polygon=poly,
                holes=[], area=poly.area,
                label_position=poly.centroid.coords[0])


def test_beam_packs_exact_rectangles_on_one_sheet():
    parts = [_rect(i, 1600, 900) for i in range(4)]
    sheets = beam_layout(parts, 3200, 1800, {}, beam_width=100)
    assert len(sheets) == 1


def test_beam_rotates_part_that_only_fits_rotated():
    parts = [_rect(1, 1750, 3100)]  # 3100 > 1800，必须横放
    sheets = beam_layout(parts, 3200, 1800, {}, beam_width=50,
                         rotations=(0, 90, 180, 270))
    assert len(sheets) == 1
    assert sheets[0][0].rot == 90


def test_beam_raises_for_unplaceable_part():
    parts = [_rect(9, 3300, 100)]
    with pytest.raises(ValueError, match="无法放入"):
        beam_layout(parts, 3200, 1800, {}, beam_width=50)


def test_beam_respects_min_gap():
    parts = [_rect(i, 1610, 900) for i in range(3)]
    sheets = beam_layout(parts, 3200, 1800, {}, beam_width=100,
                         min_gap=10.0)
    assert len(sheets) == 2
    res = _materialize(sheets, 3200, 1800, 20, "metric", len(parts))
    assert validate_nesting(res, 3200, 1800, min_gap=10.0) == []


def test_beam_is_deterministic():
    parts = [_rect(i, 1600 + i * 37, 900 + i * 13) for i in range(6)]
    a = beam_layout(parts, 3200, 1800, {}, beam_width=100)
    b = beam_layout(parts, 3200, 1800, {}, beam_width=100)
    sig_a = [[(pl.rot, round(pl.x, 3), round(pl.y, 3)) for pl in s]
             for s in a]
    sig_b = [[(pl.rot, round(pl.x, 3), round(pl.y, 3)) for pl in s]
             for s in b]
    assert sig_a == sig_b


def test_mirrored_l_parts_get_separate_tiers():
    """同 bbox 但缺口方向不同的 L 件必须分档（缺口空区不能互套）。"""
    left = _l_part(1, "tr")
    right = _l_part(2, "tl")
    tiers = _build_tiers([left, right], {}, (0, 90, 180, 270))
    assert len(tiers) == 2


def test_beam_uses_notch_space_for_small_parts():
    """小矩形应能放进 L 形件的缺口，而不是另开一张板。"""
    left = _l_part(1, "tr")
    right = _l_part(2, "tl")
    small = _rect(3, 400, 400)
    sheets = beam_layout([left, right, small], 2000, 1000, {},
                         beam_width=200)
    assert len(sheets) == 1
    res = _materialize(sheets, 2000, 1000, 20, "metric", 3)
    assert validate_nesting(res, 2000, 1000) == []


def test_multi_sheet_beam_packs_known_feasible_case():
    """多板耦合束搜索：两块 1600x1700 + 两条 1600x100 应拼进 1 张板。"""
    parts = [_rect(1, 1600, 1700), _rect(2, 1600, 1700),
             _rect(3, 1600, 100), _rect(4, 1600, 100)]
    tiers = _build_tiers(parts, {}, (0, 90, 180, 270))
    counts = tuple(len(dq) for _k, _v, dq, _h in tiers)
    got = _beam_pack_multi(counts, tiers, 1, 3200, 1800, 200, 0.0)
    assert got is not None
    assert sum(len(sheet) for sheet in got) == 4


def test_nest_parts_beam_competes_with_greedy():
    """集成：矩形件集合上 beam 布局不劣于纯贪心，且校验通过。"""
    def build():
        return [_rect(i, 978, 1892) for i in range(6)] + \
               [_rect(10 + i, 648, 1819) for i in range(3)]

    with_beam = nest_parts(build(), 3200, 1800, 20,
                           configs=[("area", "skyline", 0)],
                           improve_budget=0.0, beam=True, beam_width=200)
    greedy_only = nest_parts(build(), 3200, 1800, 20,
                             configs=[("area", "skyline", 0)],
                             improve_budget=0.0, beam=False)
    assert validate_nesting(with_beam, 3200, 1800) == []
    assert with_beam.total_sheets <= greedy_only.total_sheets


def test_nest_parts_beam_disabled_keeps_greedy_path():
    parts = [_rect(i, 1600, 900) for i in range(4)]
    res = nest_parts(parts, 3200, 1800, 20,
                     configs=[("area", "skyline", 0)],
                     improve_budget=0.0, beam=False)
    assert res.total_sheets == 1


def test_nest_parts_skips_beam_when_first_part_left_edge():
    """first_part_left_edge 约束下 beam 自动跳过（约束不支持）。"""
    parts = [_rect(i, 800, 400) for i in range(2)]
    res = nest_parts(parts, 1000, 1000, 20,
                     configs=[("area", "skyline", 0)],
                     improve_budget=0.0,
                     rotations=(0, 90, 180, 270),
                     first_part_left_edge=True,
                     beam=True, beam_width=50)
    assert res.total_sheets == 1
    placed = res.sheets[0].parts[0]
    assert placed.outer_polygon.bounds[0] == 0.0
