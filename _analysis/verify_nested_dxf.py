# -*- coding: utf-8 -*-
"""独立校验排板 DXF：图层/GROUP、逐板越界与重叠、板件归属完整性。

对照同目录的 `*_report_*.json` 检查板数、件数与编号集合，不依赖排板过程的
内存状态（只读 DXF + 报告）。

用法：
  python -X utf8 _analysis/verify_nested_dxf.py <nested.dxf> [报告.json]
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import ezdxf
from shapely.geometry import Polygon
from shapely.strtree import STRtree


def _load_report(dxf_path: Path, given: str | None) -> dict | None:
    if given:
        return json.loads(Path(given).read_text(encoding="utf-8"))
    for candidate in sorted(dxf_path.parent.glob("*_report_*.json")):
        print(f"（未指定报告，自动使用 {candidate.name}）")
        return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dxf_path = Path(sys.argv[1])
    report = _load_report(dxf_path, sys.argv[2] if len(sys.argv) > 2 else None)

    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()

    print("=" * 64)
    print("A) 整体结构")
    print("=" * 64)
    print(f"实体总数 {len(msp)}")
    print(f"  按图层: {dict(Counter(e.dxf.layer for e in msp))}")
    print(f"  按类型: {dict(Counter(e.dxftype() for e in msp))}")
    print(f"DXF 版本 {doc.dxfversion}  INSUNITS {doc.header.get('$INSUNITS')}")
    groups = list(doc.groups)
    print(f"GROUP 数量: {len(groups)}，每组实体数分布: "
          f"{dict(Counter(len(g) for g in groups))}")

    borders = [
        entity for entity in msp
        if entity.dxf.layer == "SHEET_BORDER"
        and entity.dxftype() == "LWPOLYLINE"
    ]
    dims: Counter = Counter()
    sheet_boxes = []
    for border in borders:
        pts = [(p[0], p[1]) for p in border.get_points("xy")]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        dims[(round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1))] += 1
        sheet_boxes.append((min(xs), min(ys), max(xs), max(ys)))
    print(f"SHEET_BORDER 外框数: {len(borders)}，尺寸分布: {dict(dims)}")

    print()
    print("=" * 64)
    print("B) 逐板越界与重叠校验")
    print("=" * 64)
    polys = []
    for entity in (item for item in msp if item.dxf.layer == "OUTER"):
        pts = [(p[0], p[1]) for p in entity.get_points("xy")]
        polys.append(Polygon(pts) if len(pts) >= 3 else None)

    bad_bounds = 0
    for polygon in polys:
        if polygon is None:
            continue
        minx, miny, maxx, maxy = polygon.bounds
        for box in sheet_boxes:
            if (minx >= box[0] - 0.01 and miny >= box[1] - 0.01
                    and maxx <= box[2] + 0.01 and maxy <= box[3] + 0.01):
                break
        else:
            bad_bounds += 1
    print(f"OUTER 轮廓数: {len(polys)}；越界(不属于任何大板): {bad_bounds}")

    valid = [
        (index, polygon) for index, polygon in enumerate(polys)
        if polygon is not None and polygon.is_valid and polygon.area > 0
    ]
    tree = STRtree([polygon for _, polygon in valid])
    overlaps = []
    for index, polygon in valid:
        for hit in tree.query(polygon):
            other_index, other = valid[hit]
            if other_index <= index:
                continue
            area = polygon.intersection(other).area
            if area > 1.0:
                overlaps.append((index, other_index, round(area, 1)))
    print(f"重叠面积 >1mm^2 的对数: {len(overlaps)}")
    for a, b, area in overlaps[:8]:
        print(f"    #{a} vs #{b}: {area} mm^2")

    print()
    print("=" * 64)
    print("C) 板件归属完整性")
    print("=" * 64)
    numbers = [e.dxf.text for e in msp if e.dxf.layer == "NUMBERS"]
    holes = [e for e in msp if e.dxf.layer == "HOLES"]
    print(f"NUMBERS 文本数: {len(numbers)}（唯一 {len(set(numbers))}）")
    print(f"HOLES 实体数: {len(holes)}")
    print(f"大板数: {len(borders)}")

    if report:
        print()
        print("对照报告：")
        for label, actual, expected in (
            ("大板数", len(borders), report.get("total_sheets")),
            ("零件数", len(numbers), report.get("total_parts")),
        ):
            mark = "OK" if expected is None or actual == expected else "**不一致**"
            print(f"  {label:<8}: DXF {actual} / 报告 {expected} -> {mark}")
        sheet_parts = report.get("sheets") or []
        if sheet_parts:
            report_numbers = [
                number for sheet in sheet_parts for number in sheet["parts"]
            ]
            print(f"  报告零件清单: {len(report_numbers)} 件"
                  f"（唯一 {len(set(report_numbers))}）；"
                  f"与 DXF 编号集合一致: "
                  f"{set(report_numbers) == set(numbers)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
