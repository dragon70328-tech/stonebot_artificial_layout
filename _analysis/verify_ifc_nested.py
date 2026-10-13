# -*- coding: utf-8 -*-
"""校验排板 DXF：图层、GROUP、逐板越界与重叠、板件归属完整性。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import ezdxf
from shapely.geometry import Polygon
from shapely.strtree import STRtree

dxf_path = Path(sys.argv[1])
doc = ezdxf.readfile(str(dxf_path))
msp = doc.modelspace()

print("=" * 64)
print("A) 整体结构")
print("=" * 64)
by_layer = Counter(e.dxf.layer for e in msp)
by_type = Counter(e.dxftype() for e in msp)
print(f"实体总数 {len(msp)}")
print(f"  按图层: {dict(by_layer)}")
print(f"  按类型: {dict(by_type)}")
print(f"DXF 版本 {doc.dxfversion}  INSUNITS {doc.header.get('$INSUNITS')}")

groups = list(doc.groups)
print(f"GROUP 数量: {len(groups)}")
sizes = Counter(len(g) for g in groups)
print(f"  每组实体数分布: {dict(sizes)}")

# 大板外框尺寸
borders = [e for e in msp if e.dxf.layer == "SHEET_BORDER"
           and e.dxftype() == "LWPOLYLINE"]
dims = Counter()
for b in borders:
    pts = [(p[0], p[1]) for p in b.get_points("xy")]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    dims[(round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1))] += 1
print(f"SHEET_BORDER 外框数: {len(borders)}")
print(f"  外框尺寸分布: {dict(dims)}")

# ── B) 逐板越界与重叠 ────────────────────────────────────────────
print()
print("=" * 64)
print("B) 逐板越界与重叠校验")
print("=" * 64)
outers = [e for e in msp if e.dxf.layer == "OUTER"]
polys = []
for e in outers:
    pts = [(p[0], p[1]) for p in e.get_points("xy")]
    polys.append(Polygon(pts) if len(pts) >= 3 else None)

# 用 GROUP 归属板号：SHEET_BORDER 文本形如板序号
sheet_texts = sorted(
    (e.dxf.text for e in msp
     if e.dxf.layer == "SHEET_BORDER" and e.dxftype() == "TEXT"),
    key=lambda t: int(t) if t.isdigit() else 0,
)

# 按大板 bbox 分区统计
sheet_boxes = []
for b in borders:
    pts = [(p[0], p[1]) for p in b.get_points("xy")]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    sheet_boxes.append((min(xs), min(ys), max(xs), max(ys)))

bad_bounds = 0
assigned = 0
for p in polys:
    if p is None:
        continue
    minx, miny, maxx, maxy = p.bounds
    for sb in sheet_boxes:
        if (minx >= sb[0] - 0.01 and miny >= sb[1] - 0.01
                and maxx <= sb[2] + 0.01 and maxy <= sb[3] + 0.01):
            assigned += 1
            break
    else:
        bad_bounds += 1
print(f"OUTER 轮廓数: {len(polys)}")
print(f"  落在某张大板范围内: {assigned}")
print(f"  越界(不属于任何大板): {bad_bounds}")

# 全局重叠（STRtree 加速）
valid = [(i, p) for i, p in enumerate(polys)
         if p is not None and p.is_valid and p.area > 0]
tree = STRtree([p for _, p in valid])
overlaps = []
for i, p in valid:
    for j in tree.query(p):
        idx = valid[j][0]
        if idx <= i:
            continue
        q = valid[j][1]
        inter = p.intersection(q)
        if inter.area > 1.0:
            overlaps.append((i, idx, round(inter.area, 1)))
print(f"  面积重叠 >1mm^2 的对数: {len(overlaps)}")
for a, b, ar in overlaps[:8]:
    print(f"    #{a} vs #{b}: {ar} mm^2")

# ── C) 板件归属 ──────────────────────────────────────────────────
print()
print("=" * 64)
print("C) 板件归属完整性")
print("=" * 64)
numbers = [e for e in msp if e.dxf.layer == "NUMBERS"]
print(f"NUMBERS 编号文本数: {len(numbers)}  (应为 766)")
holes = [e for e in msp if e.dxf.layer == "HOLES"]
print(f"HOLES 实体数: {len(holes)}  (本图无孔洞，应为 0)")
print(f"大板数: {len(borders)}  (报告声称 67)")
