"""打印指定外包络的多段线顶点与"末点到各边"的距离，看清闭合是怎么兜出来的。

用法：python vertex_probe.py <源图.dxf> <bbox: x0,y0,x1,y1>
"""

from __future__ import annotations

import math
import sys

import ezdxf


def _points(entity) -> list[tuple[float, float]]:
    if entity.dxftype() == "LWPOLYLINE":
        return [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
    return [
        (float(v.dxf.location.x), float(v.dxf.location.y))
        for v in entity.vertices
    ]


def _point_segment_distance(point, start, end) -> float:
    px, py = point
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def main() -> int:
    doc = ezdxf.readfile(sys.argv[1])
    x0, y0, x1, y1 = (float(value) for value in sys.argv[2].split(","))
    for entity in doc.modelspace():
        if entity.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
            continue
        points = _points(entity)
        if len(points) < 3:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        if not all(
            abs(a - b) < 1.5 for a, b in zip(bbox, (x0, y0, x1, y1))
        ):
            continue
        print(f"实体 {entity.dxf.handle} {entity.dxftype()} 顶点数={len(points)}")
        for index, point in enumerate(points):
            print(f"   v{index}: ({point[0]:.4f}, {point[1]:.4f})")
        print(
            f"  首尾距离={math.hypot(points[0][0] - points[-1][0], points[0][1] - points[-1][1]):.4f}mm"
        )
        distances = [
            (
                index,
                _point_segment_distance(points[-1], points[index], points[index + 1]),
            )
            for index in range(len(points) - 1)
        ]
        for index, distance in sorted(distances, key=lambda item: item[1])[:4]:
            print(f"  末点 → 边 v{index}-v{index + 1} 距离 = {distance:.6f}mm")
        duplicates = [
            (i, j, math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1]))
            for i in range(len(points))
            for j in range(i + 1, len(points))
            if math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1]) < 1.0
        ]
        print(f"  相距 <1mm 的顶点对：{duplicates}")
        return 0
    print("未找到匹配实体")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
