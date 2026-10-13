# -*- coding: utf-8 -*-
"""从生成的 DXF 直接核对编号文本与轮廓重叠（不依赖契约字段）。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import ezdxf
from shapely.geometry import Polygon

numbered = Path(sys.argv[1])
doc = ezdxf.readfile(str(numbered))
msp = doc.modelspace()

print("=" * 62)
print("A) NUMBERS 图层文本核对")
print("=" * 62)
numbers = [e for e in msp if e.dxf.layer == "NUMBERS"]
texts = [e.dxf.text for e in numbers]
print(f"NUMBERS 文本数: {len(texts)}")
print(f"文本去重后数量 : {len(set(texts))}")
print(f"重复的文本     : {[t for t, c in Counter(texts).items() if c > 1]}")
print(f"以 -2/-3 结尾的: {[t for t in texts if t.rsplit('-', 1)[-1].isdigit()]}")

print()
print("=" * 62)
print("B) OUTER 轮廓面积重叠检查（前 400 件两两比对）")
print("=" * 62)
outers = [e for e in msp if e.dxf.layer == "OUTER"]
polys = []
for e in outers:
    pts = [(p[0], p[1]) for p in e.get_points("xy")]
    polys.append(Polygon(pts) if len(pts) >= 3 else None)

n = len(polys)
overlaps = []
limit = min(n, 400)
for i in range(limit):
    pi = polys[i]
    if pi is None or not pi.is_valid or pi.area <= 0:
        continue
    for j in range(i + 1, limit):
        pj = polys[j]
        if pj is None or not pj.is_valid or pj.area <= 0:
            continue
        if not pi.bounds[2] < pj.bounds[0] or not pj.bounds[2] < pi.bounds[0]:
            pass
        # bbox 快速排除
        if (pi.bounds[2] <= pj.bounds[0] or pj.bounds[2] <= pi.bounds[0]
                or pi.bounds[3] <= pj.bounds[1] or pj.bounds[3] <= pi.bounds[1]):
            continue
        inter = pi.intersection(pj)
        if inter.area > 1.0:
            overlaps.append((i, j, round(inter.area, 1),
                             round(inter.area / min(pi.area, pj.area) * 100, 1)))

print(f"OUTER 轮廓数: {n}（本次比对前 {limit} 件，bbox 预筛）")
print(f"面积重叠 >1mm^2 的对数: {len(overlaps)}")
for i, j, a, pct in overlaps[:10]:
    print(f"  #{i} vs #{j}: 重叠 {a} mm^2（占较小件 {pct}%）")

print()
print("=" * 62)
print("C) CHECK_CLOSURE 标记")
print("=" * 62)
cc = [e for e in msp if e.dxf.layer == "CHECK_CLOSURE"]
cc_poly = [e for e in cc if e.dxftype() == "LWPOLYLINE"]
cc_text = [e for e in cc if e.dxftype() == "TEXT"]
print(f"CHECK_CLOSURE 实体: {len(cc)}（轮廓 {len(cc_poly)}，文本 {len(cc_text)}）")
for e in cc_text[:12]:
    print(f"  {e.dxf.text}")

areas = sorted(p.area for p in polys if p is not None)
print()
print(f"面积最小/中位/最大 (mm^2): {areas[0]:.0f} / "
      f"{areas[len(areas)//2]:.0f} / {areas[-1]:.0f}")
