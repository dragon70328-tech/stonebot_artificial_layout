"""离线探测：对已存排板 DXF 直接跑"多板联合重排压缩"，不重跑整套排板。

用法：
    python -X utf8 _analysis/rea_youshi/probe_compress_offline.py <nested.dxf> [k ...]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from src.nesting import (_Placement, _compress_sheets, _materialize,
                         _sheet_outer_area, validate_nesting)
from src.nesting_reviewer import load_nested_dxf


def validate_placed(placed, sheet_w, sheet_h, thickness=20.0):
    """用仓库自带的 _materialize + validate_nesting 做几何校验。

    自己手写"平移+两两相交"很容易出错（第一版把 x/y 当 0 处理，等于把所有零件
    挪到原点，造出上千处假重叠）。这里直接复用生产代码的同一套变换与校验。
    """
    total = sum(len(s) for s in placed)
    result = _materialize(placed, sheet_w, sheet_h, thickness, "metric", total)
    return validate_nesting(result, sheet_w, sheet_h, min_gap=0.0)


def to_placed(result):
    """NestingResult → 内部 list[list[_Placement]]（几何已在落位状态）。"""
    return [
        # 约定与搜索内部一致：poly 保持原图坐标，(x, y) 是落位后的包围盒左下角
        [_Placement(part=p, rot=0.0,
                    x=p.outer_polygon.bounds[0], y=p.outer_polygon.bounds[1],
                    poly=p.outer_polygon)
         for p in sheet.parts]
        for sheet in result.sheets
    ]


def main():
    dxf = sys.argv[1]
    sizes = tuple(int(x) for x in sys.argv[2:]) or (4, 6, 8)
    result = load_nested_dxf(dxf)
    placed = to_placed(result)
    sheet_w = result.sheets[0].width
    sheet_h = result.sheets[0].height
    areas = sorted(_sheet_outer_area(s) for s in placed)
    cap = sheet_w * sheet_h
    print(f"输入 {dxf}")
    print(f"  {len(placed)} 张板 / 零件 {sum(len(s) for s in placed)} / "
          f"板面 {cap/1e6:.2f} m² / 外包络合计 {sum(areas)/1e6:.2f} m²")
    print(f"  单板外包络填充：最低 {areas[0]/cap*100:.1f}% / "
          f"中位 {areas[len(areas)//2]/cap*100:.1f}% / 最高 {areas[-1]/cap*100:.1f}%")
    base_problems = validate_nesting(result, sheet_w, sheet_h, min_gap=0.0)
    print(f"  压缩**前**几何自检："
          + ("通过" if not base_problems else f"{len(base_problems)} 处问题"))
    for p in base_problems[:3]:
        print(f"    {p}")

    t0 = time.time()
    stats: dict = {}
    compressed = _compress_sheets(placed, sheet_w, sheet_h, {},
                                  subset_sizes=sizes, stats=stats)
    dt = time.time() - t0
    print(f"  压缩重排 subset_sizes={sizes}：{len(placed)} -> {len(compressed)} 张"
          f"  接受 {stats['compress_accepted']} 次 / 尝试 {stats['compress_attempts']} 次"
          f"  面积跳过 {stats['compress_skipped_by_area']} 次"
          f"  大子集跳过束搜索 {stats.get('compress_beam_skipped_large', 0)} 次"
          f"  {dt:.1f}s")
    if len(compressed) != len(placed):
        problems = validate_placed(compressed, sheet_w, sheet_h)
        print(f"  几何自检：{'通过（越界 0、重叠 0）' if not problems else f'{len(problems)} 处问题'}")
        for p in problems[:5]:
            print(f"    {p}")


if __name__ == "__main__":
    main()
