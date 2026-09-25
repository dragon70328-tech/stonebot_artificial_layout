"""渲染对照：算法52布局 vs 模板50布局 的最差/最好板。"""
import sys
from pathlib import Path

import ezdxf
from shapely.geometry import Point, Polygon
from shapely.affinity import translate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _analysis.lns_lab import (get_baseline_layout, SHEET_W, SHEET_H)
from _analysis.raster import dump_sheets_png
from src.nesting import _sheet_used_area

SHEET_AREA = SHEET_W * SHEET_H
OUT = Path(__file__).resolve().parent


def template_sheets_polys():
    doc = ezdxf.readfile(
        str(Path(__file__).resolve().parent.parent
            / "drawing_profiles" / "validated_templates"
            / "cgr45_3200x1800_v3.dxf"))
    polys, texts = [], []
    for e in doc.modelspace():
        if e.dxftype() == "LWPOLYLINE":
            pts = [(float(x), float(y)) for x, y in e.get_points("xy")]
            if len(pts) >= 3:
                polys.append(Polygon(pts))
        elif e.dxftype() == "TEXT":
            texts.append((e.dxf.text,
                          Point(float(e.dxf.insert.x), float(e.dxf.insert.y))))
    borders = [p for p in polys
               if abs(p.bounds[2] - p.bounds[0] - SHEET_W) < 1
               and abs(p.bounds[3] - p.bounds[1] - SHEET_H) < 1]
    outers_all = [p for p in polys if p not in borders]
    sheets = []
    for b in borders:
        inside_o = [p for p in outers_all if b.contains(p.centroid)]
        inside_t = [t for t in texts
                    if not t[0].startswith("Sheet_") and b.contains(t[1])]
        members, used = [], set()
        for text, pt in inside_t:
            cands = [p for p in inside_o
                     if p.buffer(1e-6).contains(pt) and id(p) not in used]
            if not cands:
                continue
            outer = max(cands, key=lambda p: p.area)
            used.add(id(outer))
            members.append((text, outer))
        sheets.append((b, members))
    return sheets


def main():
    import pickle
    parts_pkl = OUT / "cgr45_parts.pkl"
    with parts_pkl.open("rb") as fh:
        parts = pickle.load(fh)
    area_by_number = {p.number: p.area for p in parts}

    # 算法 52：最差 4 + 最好 4
    best_pl, _ = get_baseline_layout()
    fills = [(_sheet_used_area(s) / SHEET_AREA, i, s)
             for i, s in enumerate(best_pl)]
    fills.sort()
    pick = fills[:4] + fills[-4:]
    sheets_polys = [[pl.poly for pl in s] for _f, _i, s in pick]
    dump_sheets_png(sheets_polys, str(OUT / "algo52_edges.png"),
                    SHEET_W, SHEET_H, cols=4)
    print("algo52_edges.png:",
          [f"sheet{i + 1}:{f:.0%}" for f, i, _s in pick])

    # 模板 50：最差 4 + 最好 4
    tsheets = template_sheets_polys()
    rows = []
    for b, members in tsheets:
        ox, oy = b.bounds[0], b.bounds[1]
        area = sum(area_by_number.get(num, 0.0) for num, _p in members)
        rows.append((area / SHEET_AREA, [translate(p, xoff=-ox, yoff=-oy)
                                         for _n, p in members],
                     [num for num, _p in members]))
    rows.sort(key=lambda r: r[0])
    pick = rows[:4] + rows[-4:]
    dump_sheets_png([r[1] for r in pick], str(OUT / "tpl50_edges.png"),
                    SHEET_W, SHEET_H, cols=4)
    print("tpl50_edges.png:", [f"{r[0]:.0%}" for r in pick])
    for r in pick:
        print(f"  fill={r[0]:.1%} parts={len(r[1])} nums={r[2]}")


if __name__ == "__main__":
    main()
