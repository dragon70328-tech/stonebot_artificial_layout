"""解剖 50 张已验证模板布局：填充分布 + 零件分组，与算法布局对照。"""
import pickle
import sys
from collections import Counter
from pathlib import Path

import ezdxf
from shapely.geometry import Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _analysis.lns_lab import (REVIEWED_DXF, get_baseline_layout,
                               get_parts, SHEET_W, SHEET_H)

SHEET_AREA = SHEET_W * SHEET_H


def parse_template():
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
    outers = [p for p in polys if p not in borders]
    sheets = []
    for bi, b in enumerate(borders, 1):
        inside_o = [p for p in outers if b.contains(p.centroid)]
        inside_t = [t for t in texts
                    if not t[0].startswith("Sheet_") and b.contains(t[1])]
        members = []
        used = set()
        for text, pt in inside_t:
            # 编号文本可能落在孔内：包含该点的多边形里，外轮廓是面积最大的
            cands = [p for p in inside_o
                     if p.buffer(1e-6).contains(pt) and id(p) not in used]
            if not cands:
                continue
            outer = max(cands, key=lambda p: p.area)
            used.add(id(outer))
            members.append(text)
        sheets.append({"index": bi, "border": b, "numbers": members})
    return sheets


def main():
    parts = get_parts()
    area_by_number = {}
    for p in parts:
        area_by_number.setdefault(p.number, p.area)
    sheets = parse_template()
    n = len(sheets)
    assert n == 50, n
    total = sum(len(s["numbers"]) for s in sheets)
    print(f"模板: {n} 张, 编号数 {total}")

    fills, counts = [], []
    grouping = []
    for s in sheets:
        area = sum(area_by_number.get(num, 0.0) for num in s["numbers"])
        fills.append(area / SHEET_AREA)
        counts.append(len(s["numbers"]))
        grouping.append(sorted(s["numbers"]))
    fills_s = sorted(fills)
    print("模板填充: min={:.1%} med={:.1%} max={:.1%} mean={:.1%}".format(
        fills_s[0], fills_s[n // 2], fills_s[-1], sum(fills) / n))
    print("  最低 12:", [f"{f:.0%}" for f in fills_s[:12]])
    print("  最高 12:", [f"{f:.0%}" for f in fills_s[-12:]])
    print("  件/板: min", min(counts), "max", max(counts),
          "分布", sorted(Counter(counts).items()))
    missing = [num for s in sheets for num in s["numbers"]
               if num not in area_by_number]
    print("  未匹配编号:", len(missing), missing[:10])

    # 与算法 52 布局 / 回看 51 布局的分组相似度
    best_pl, _ = get_baseline_layout()
    algo_groups = [sorted(pl.part.number for pl in pls) for pls in best_pl]
    pkl = Path(__file__).resolve().parent / "cgr45_template_grouping.pkl"
    with pkl.open("wb") as fh:
        pickle.dump(grouping, fh)

    def overlap(g1, g2):
        s1, s2 = set(g1), set(g2)
        return len(s1 & s2) / max(len(s1 | s2), 1)

    same = sum(1 for g in grouping for ag in algo_groups
               if set(g) == set(ag))
    print("模板与算法52完全同组的板数:", same)


if __name__ == "__main__":
    main()
