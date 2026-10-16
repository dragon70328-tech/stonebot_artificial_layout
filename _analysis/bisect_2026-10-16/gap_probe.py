"""测量 DXF 里多段线的首尾缝隙，验证"闭合容差"假设。

用法：python gap_probe.py <源图.dxf> [零件清单.json]

清单里给出的外包络用于反查实体（dump_panels.py 的产物）。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import ezdxf


def _points(entity) -> list[tuple[float, float]]:
    if entity.dxftype() == "LWPOLYLINE":
        return [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
    return [
        (float(v.dxf.location.x), float(v.dxf.location.y))
        for v in entity.vertices
    ]


def main() -> int:
    doc = ezdxf.readfile(sys.argv[1])
    targets = []
    if len(sys.argv) > 2:
        rows = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        targets = [(row["number"], row["bbox"]) for row in rows]

    band = []
    matched = {}
    for entity in doc.modelspace():
        if entity.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
            continue
        points = _points(entity)
        if len(points) < 3:
            continue
        gap = math.hypot(
            points[0][0] - points[-1][0], points[0][1] - points[-1][1]
        )
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        if 0.01 < gap <= 0.1:
            band.append((round(gap, 4), bbox, len(points), entity.dxftype()))
        for number, target in targets:
            if all(abs(a - b) < 1.5 for a, b in zip(bbox, target)):
                matched[number] = (gap, len(points), entity.dxftype(), bbox)

    print(f"首尾缝隙落在 (0.01, 0.1] mm 的多段线：{len(band)} 条")
    for gap, bbox, count, kind in sorted(band)[:20]:
        print(f"  缝隙={gap}mm 顶点={count} {kind} bbox={[round(v, 1) for v in bbox]}")

    print(f"\n清单里的零件反查到实体：{len(matched)}/{len(targets)}")
    for number, (gap, count, kind, _bbox) in sorted(matched.items()):
        print(f"  {number!r:22s} 首尾缝隙={gap:.4f}mm 顶点={count} {kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
