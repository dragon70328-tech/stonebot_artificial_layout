# -*- coding: utf-8 -*-
"""统计哪些台面板放不进 3200x1600，以及能否放进 3230x1630（让尺30）。

按最小旋转框（0/90 取向）判定，并给出超尺寸件的实际长宽。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.drawing_profile import load_profiles  # noqa: E402
from src.workflow import prepare_drawing  # noqa: E402

BASE = (3200.0, 1600.0)
OVER = (3230.0, 1630.0)


def min_box(poly):
    """返回 (最小宽度, 最小高度, 是否旋转90度)。"""
    minx, miny, maxx, maxy = poly.bounds
    w, h = maxx - minx, maxy - miny
    if h < w:          # 以短边为宽
        return h, w, True
    return w, h, False


def fits(sizes, sheet):
    w, h, _ = sizes
    sw, sh = sheet
    return (w <= sw + 1e-6 and h <= sh + 1e-6) or \
           (h <= sw + 1e-6 and w <= sh + 1e-6)


def main() -> int:
    dxf_path = sys.argv[1]
    profiles = load_profiles(PROJECT_ROOT / "drawing_profiles")
    target = next(p for p in profiles if p.name == "canada_ifc")

    prepared = prepare_drawing(str(dxf_path), drawing_profile=target,
                               skip_unnumbered=False, use_case_library=False)
    if prepared.error:
        print(prepared.error)
        return 1
    parts = prepared.parts

    normal, oversize, stilL_big = [], [], []
    for p in parts:
        s = min_box(p.outer_polygon)
        if fits(s, BASE):
            normal.append((p, s))
        elif fits(s, OVER):
            oversize.append((p, s))
        else:
            stilL_big.append((p, s))

    print("=" * 68)
    print("大板适用性判定（按最小旋转框，允许 0/90 旋转）")
    print("=" * 68)
    print(f"基准大板 {BASE[0]:.0f}x{BASE[1]:.0f} / 让尺大板 "
          f"{OVER[0]:.0f}x{OVER[1]:.0f}")
    print(f"台面板总数               : {len(parts)}")
    print(f"  放得进 3200x1600       : {len(normal)}")
    print(f"  需 3230x1630 才放得下   : {len(oversize)}")
    print(f"  连 3230x1630 都放不下   : {len(stilL_big)}")

    for label, group in (("需让尺的件", oversize), ("仍放不下的件", stilL_big)):
        if not group:
            continue
        print()
        print(f"── {label}（{len(group)} 件）──")
        rows = sorted(group, key=lambda t: -t[1][0] * t[1][1])
        print(f"{'编号':<12}{'短边':>9}{'长边':>9}{'旋转90':>8}   面积m2")
        for p, (w, h, rot) in rows[:40]:
            print(f"{str(p.number):<12}{w:>9.1f}{h:>9.1f}"
                  f"{'是' if rot else '否':>8}   {p.area/1e6:>6.3f}")
        if len(rows) > 40:
            print(f"  ... 另有 {len(rows)-40} 件")

    n_area = sum(p.area for p, _ in normal) / 1e6
    o_area = sum(p.area for p, _ in oversize) / 1e6
    print()
    print("=" * 68)
    print("面积与理论板数下界")
    print("=" * 68)
    print(f"常规件面积 {n_area:.3f} m2 -> 3200x1600 下界 "
          f"{int(-(-n_area*1e6//(3200*1600)))} 张")
    print(f"让尺件面积 {o_area:.3f} m2 -> 3230x1630 下界 "
          f"{int(-(-o_area*1e6//(3230*1630)))} 张")
    print(f"合计面积   {n_area+o_area:.3f} m2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
