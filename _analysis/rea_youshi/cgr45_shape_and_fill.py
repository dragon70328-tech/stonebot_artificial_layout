"""Measure where the CGR45 loss actually sits: per-sheet fill and part edge mix.

Reads a main.py report + its recognized-parts JSON and answers two questions:

1. Fill distribution across the used sheets - is the gap concentrated in a
   low-fill tail sheet, or spread evenly?
2. How much of the total part perimeter is axis-aligned? NFP-style interlocking
   mostly pays off on *diagonal / curved* edges, because axis-aligned free
   rectangles already handle rectilinear parts well.

Usage:
    python -X utf8 _analysis/rea_youshi/cgr45_shape_and_fill.py <report.json> <recognized.json>
"""

import glob
import json
import math
import os
import sys


def edges(poly):
    pts = poly
    for i in range(len(pts)):
        yield pts[i], pts[(i + 1) % len(pts)]


def wkt_poly(wkt):
    """Parse the first POLYGON ring of a WKT string into [(x, y), ...]."""
    if not wkt or "(" not in wkt:
        return []
    body = wkt[wkt.index("(") + 1:wkt.rindex(")")]
    ring = body.split(")")[0].lstrip("(")
    out = []
    for pair in ring.split(","):
        xy = pair.split()
        if len(xy) >= 2:
            out.append((float(xy[0]), float(xy[1])))
    return out


def shoelace(pts):
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def analyze(out_dir):
    report_path = glob.glob(os.path.join(out_dir, "*report_*.json"))[0]
    rec_path = glob.glob(os.path.join(out_dir, "*recognized.json"))[0]
    report = json.load(open(report_path, encoding="utf-8"))
    rec = json.load(open(rec_path, encoding="utf-8"))

    panels = rec.get("panels") or rec.get("parts") or []
    if not panels:
        print("recognized json keys:", list(rec.keys())[:20])
        return

    by_number = {}
    for p in panels:
        poly = wkt_poly(p.get("outer_geometry_wkt") or p.get("geometry_wkt"))
        by_number[p.get("number") or p.get("panel_id")] = (shoelace(poly), poly)

    total_part_area = report.get("total_part_area") or 0.0
    sheets = report.get("sheets") or []
    sheet_area = (report["sheet_dimensions"]["width"] * report["sheet_dimensions"]["height"])

    fills = []
    for s in sheets:
        used = sum((by_number.get(n, (0.0, None))[0] or 0.0) for n in s.get("parts", []))
        fills.append((s["index"], s.get("part_count"), used / sheet_area * 100.0, used))
    fills.sort(key=lambda x: x[2])

    print(f"\n===== {os.path.basename(out_dir)} =====")
    print(f"sheets={len(sheets)}  sheet_area={sheet_area/1e6:.3f} m2  "
          f"parts={report.get('total_parts')}  yield={report.get('yield_rate')}")
    print(f"total part area={total_part_area/1e6:.3f} m2")
    for target in (len(sheets) - 1, len(sheets) - 2):
        need = total_part_area / (target * sheet_area) * 100.0
        print(f"  用 {target} 张板需要平均填充 {need:.2f}%")

    print("\n最低填充的 8 张板：")
    for idx, cnt, pct, used in fills[:8]:
        print(f"  sheet {idx:>3}  parts={cnt:>2}  fill={pct:5.1f}%  used={used/1e6:.3f} m2")
    print("最高填充的 3 张板：")
    for idx, cnt, pct, used in fills[-3:]:
        print(f"  sheet {idx:>3}  parts={cnt:>2}  fill={pct:5.1f}%")
    print("尾板零件号：", [n for s in sheets[-2:] for n in s.get("parts", [])])
    mid = fills[len(fills) // 2]
    print(f"中位填充: {mid[2]:.1f}%   <60%: {sum(1 for f in fills if f[2] < 60)} 张"
          f"   <75%: {sum(1 for f in fills if f[2] < 75)} 张")

    # ---- edge mix ----
    ax_len = dia_len = 0.0
    parts_with_diag = 0
    parts_arc_like = 0
    seg_angles = []
    for num, (area, poly) in by_number.items():
        if not poly or len(poly) < 3:
            continue
        has_diag = False
        for (x1, y1), (x2, y2) in edges(poly):
            dx, dy = x2 - x1, y2 - y1
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            ang = abs(math.degrees(math.atan2(dy, dx))) % 180.0
            seg_angles.append((ang, L))
            if min(ang, abs(ang - 90), abs(ang - 180)) < 1.0:
                ax_len += L
            else:
                dia_len += L
                has_diag = True
        if has_diag:
            parts_with_diag += 1
        # arc-like: many edges whose direction drifts in small steps
        dirs = []
        for (x1, y1), (x2, y2) in edges(poly):
            dirs.append(math.degrees(math.atan2(y2 - y1, x2 - x1)) % 360.0)
        if len(dirs) >= 8:
            drifts = [abs(((dirs[(i + 1) % len(dirs)] - d) + 180) % 360 - 180) for i, d in enumerate(dirs)]
            small = sum(1 for d in drifts if d < 12)
            if small >= max(4, len(dirs) * 0.5):
                parts_arc_like += 1

    tot = ax_len + dia_len
    if not tot:
        print("!! 没能解析出轮廓，跳过边长统计")
        return
    print(f"\n轮廓边长合计 {tot/1000:.2f} m：轴对齐 {ax_len/tot*100:.1f}% / "
          f"斜边(含弧离散段) {dia_len/tot*100:.1f}%")
    print(f"含斜边的零件: {parts_with_diag} / {len(by_number)}"
          f"   疑似含弧(方向连续漂移)的零件: {parts_arc_like}")
    print(f"零件平均顶点数: "
          f"{sum(len(p[1]) for p in by_number.values() if p[1])/max(1,len(by_number)):.1f}")


if __name__ == "__main__":
    for d in sys.argv[1:]:
        analyze(d)
