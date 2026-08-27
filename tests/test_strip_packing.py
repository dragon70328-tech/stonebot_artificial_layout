"""精确窄条 packer 与 kerf 放大/缩小边界测试。"""

from src.list_nesting import (
    ListItem,
    _deflate_results,
    _inflate_items,
    nest_list_items,
)
from src.strip_packing import exact_bin_pack, pack_residual_strips


def _assert_layout_valid(sheets):
    for sheet in sheets:
        polys = [p.polygon for p in sheet.parts]
        for i, poly in enumerate(polys):
            minx, miny, maxx, maxy = poly.bounds
            assert minx >= -1e-6 and miny >= -1e-6
            assert maxx <= sheet.width + 1e-6
            assert maxy <= sheet.height + 1e-6
            for other in polys[i + 1 :]:
                assert poly.intersection(other).area < 1e-6


def test_exact_bin_pack_simple():
    bins, exact = exact_bin_pack({10: 3, 6: 2}, 12)
    assert exact
    assert len(bins) == 4  # 10 独占 3 列，6+6 一列
    for fill, _ in bins:
        assert fill <= 12


def test_exact_bin_pack_kerf_style():
    # 放大后尺寸：1197+5=1202 被迫独占一列；243+5=248 每列最多 4 根
    bins, exact = exact_bin_pack({1202: 3, 248: 4}, 1235)
    assert exact
    assert len(bins) == 4
    total = sum(count for _, cnt in bins for count in cnt.values())
    assert total == 7


def test_pack_residual_strips_two_sheets():
    strips = [ListItem("", 1202, 100, 1, "t") for _ in range(48)]
    sheets, next_id = pack_residual_strips(strips, [(2435.0, 1235.0)], 1, 20.0)
    # 24 列/板 -> 2 张板
    assert len(sheets) == 2
    assert all(len(sheet.parts) == 24 for sheet in sheets)
    _assert_layout_valid(sheets)


def test_pack_residual_strips_prefers_better_area():
    strips = [ListItem("", 1202, 100, 1, "t") for _ in range(50)]
    sizes = [(2435.0, 1235.0), (2535.0, 1435.0)]
    sheets, _ = pack_residual_strips(strips, sizes, 1, 20.0)
    # 小板 3 张 (24 列/板) vs 大板 2 张 (25 列/板)，大板总面积更小
    assert len(sheets) == 2
    assert all((sheet.width, sheet.height) == (2535.0, 1435.0) for sheet in sheets)
    _assert_layout_valid(sheets)


def test_kerf_inflate_deflate_roundtrip():
    items = [ListItem("", 600, 400, 2, "t")]
    kerf = 5.0
    effective_items = _inflate_items(items, kerf)
    group_results = nest_list_items(
        effective_items,
        sheet_sizes=[(2465.0, 1265.0)],
        rotations=(0,),
    )
    _deflate_results(group_results, kerf)
    _, _, result = group_results[0]
    assert result.total_sheets == 1
    sheet = result.sheets[0]
    assert (sheet.width, sheet.height) == (2460.0, 1260.0)
    for part in sheet.parts:
        minx, miny, maxx, maxy = part.polygon.bounds
        assert abs((maxx - minx) - 600) < 1e-6 or abs((maxx - minx) - 400) < 1e-6
        assert abs((maxy - miny) - 400) < 1e-6 or abs((maxy - miny) - 600) < 1e-6
    # 件间留缝不小于 kerf
    polys = sorted(sheet.parts, key=lambda p: p.polygon.bounds[0])
    for left, right in zip(polys, polys[1:]):
        gap = right.polygon.bounds[0] - left.polygon.bounds[2]
        assert gap >= kerf - 1e-6
    _assert_layout_valid([sheet])
