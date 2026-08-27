# -*- coding: utf-8 -*-
"""ST-104 kerf 化排板几何校验：越界 / 重叠 / 最小间距 / 件数完整性。"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.list_nesting import (  # noqa: E402
    _deflate_results,
    _inflate_items,
    nest_list_items,
    parse_list_file,
)

FILE = r"C:\Users\drago\Desktop\临时文件\ST-104规格板清单.xlsx"
KERF = 5.0
OVERSIZE = 30.0
BILLING = [(2400.0, 1200.0), (2500.0, 1400.0)]
TOL = 1e-6


def rect_of(part):
    return part.outer_polygon.bounds  # (minx, miny, maxx, maxy)


def check(group_results, kerf_gap: float, label: str, expected: int) -> bool:
    oob = 0
    overlaps = 0
    min_gap = math.inf
    placed = 0
    for _, _, result in group_results:
        for sheet in result.sheets:
            rects = []
            for part in sheet.parts:
                b = rect_of(part)
                placed += 1
                if (
                    b[0] < -TOL
                    or b[1] < -TOL
                    or b[2] > sheet.width + TOL
                    or b[3] > sheet.height + TOL
                ):
                    oob += 1
                    print(
                        f"[{label}] 越界 sheet {sheet.width:.0f}x{sheet.height:.0f} "
                        f"part {part.number} bounds={tuple(round(v, 1) for v in b)}"
                    )
                rects.append((part.number, b))
            for i in range(len(rects)):
                n1, a = rects[i]
                for j in range(i + 1, len(rects)):
                    n2, c = rects[j]
                    hgap = max(c[0] - a[2], a[0] - c[2])
                    vgap = max(c[1] - a[3], a[1] - c[3])
                    if hgap < -TOL and vgap < -TOL:
                        overlaps += 1
                        print(
                            f"[{label}] 重叠 {n1} vs {n2} "
                            f"hgap={hgap:.2f} vgap={vgap:.2f}"
                        )
                    sep = math.hypot(max(hgap, 0.0), max(vgap, 0.0))
                    min_gap = min(min_gap, sep)
    print(
        f"[{label}] 已排 {placed}/{expected} 件, "
        f"越界 {oob}, 重叠 {overlaps}, 最小间距 {min_gap:.3f} mm "
        f"(要求 >= {kerf_gap})"
    )
    return (
        oob == 0
        and overlaps == 0
        and min_gap >= kerf_gap - TOL
        and placed == expected
    )


def main() -> int:
    items = parse_list_file(FILE)
    total_qty = sum(max(1, it.qty) for it in items)
    sizes = [(w + OVERSIZE + KERF, h + OVERSIZE + KERF) for w, h in BILLING]
    inflated = _inflate_items(items, KERF)
    group_results = nest_list_items(inflated, sheet_sizes=sizes)

    ok_inflated = check(group_results, kerf_gap=-TOL, label="放大空间", expected=total_qty)
    _deflate_results(group_results, KERF)
    ok_net = check(group_results, kerf_gap=KERF, label="净尺寸空间", expected=total_qty)

    sheets_used = sum(len(r.sheets) for _, _, r in group_results)
    print(f"用板 {sheets_used} 张, 清单总件数 {total_qty}")
    if ok_inflated and ok_net:
        print("校验通过")
        return 0
    print("校验失败")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
