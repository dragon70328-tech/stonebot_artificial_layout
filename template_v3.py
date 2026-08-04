# -*- coding: utf-8 -*-
"""Template nesting: place CGR45final panels at v3 layout positions.

v3 parsing: sheet rects + containment-based hole filtering + point-in-polygon
text association (label is always inside its panel).
Placement: rotation+translation chosen by max IoU against the v3 polygon.
Repair: sliver overlaps fixed by chain-shift (move a panel together with the
panels blocking its path, toward available slack).
"""
import sys, json
import ezdxf
from collections import defaultdict
from pathlib import Path
from shapely.geometry import Polygon, Point
from shapely import affinity

sys.path.insert(0, r"C:\Users\drago\stonebot_artificial_layout")
from src.dxf_reader import read_dxf
from src.numbering import assign_numbers
from src.dxf_writer import write_nested_dxf
from src.models import Sheet, NestingResult, Part

V3 = r"C:\Users\drago\stonebot_artificial_layout\output\CGR45_nested_3200x1800_v3.dxf"
FIN = r"C:\Users\drago\Desktop\临时文件\CGR45final.dxf"
SHEET_W, SHEET_H = 3200.0, 1800.0

# ---------------- Parse v3 ----------------
doc = ezdxf.readfile(V3)
msp = doc.modelspace()

sheet_rects = []
raw_polys = []
for e in msp:
    if e.dxftype() != "LWPOLYLINE":
        continue
    pts = [(p[0], p[1]) for p in e.get_points()]
    if len(pts) < 3:
        continue
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    if abs(max(xs) - min(xs) - SHEET_W) < 1 and abs(max(ys) - min(ys) - SHEET_H) < 1 and len(pts) == 5:
        sheet_rects.append((min(xs), min(ys), max(xs), max(ys)))
        continue
    poly = Polygon(pts)
    if not poly.is_valid or poly.area < 1:
        poly = poly.buffer(0)
        if poly.is_empty or poly.area < 1:
            continue
    raw_polys.append(poly)

sheet_rects.sort(key=lambda r: (r[1], r[0]))
print(f"v3: {len(sheet_rects)} sheets, {len(raw_polys)} closed polys")

n = len(raw_polys)
is_hole = [False] * n
order = sorted(range(n), key=lambda i: raw_polys[i].area)
for rank, i in enumerate(order):
    pi = raw_polys[i]
    ci = pi.centroid
    for j in order[rank + 1:]:
        pj = raw_polys[j]
        b = pj.bounds
        if not (b[0] <= ci.x <= b[2] and b[1] <= ci.y <= b[3]):
            continue
        if pj.contains(ci) and pi.intersection(pj).area > 0.9 * pi.area:
            is_hole[i] = True
            break

outer_polys = [raw_polys[i] for i in range(n) if not is_hole[i]]
print(f"v3: {len(outer_polys)} outer polys, {sum(is_hole)} holes")

texts = [(e.dxf.insert.x, e.dxf.insert.y, e.dxf.text)
         for e in msp if e.dxftype() == "TEXT" and not e.dxf.text.startswith("Sheet_")]

text_used = [False] * len(texts)
v3_parts = {}
no_text = []
for poly in outer_polys:
    c = poly.centroid
    si = None
    for k, (sx0, sy0, sx1, sy1) in enumerate(sheet_rects):
        if sx0 - 1 <= c.x <= sx1 + 1 and sy0 - 1 <= c.y <= sy1 + 1:
            si = k
            break
    if si is None:
        no_text.append(("no-sheet", round(poly.area)))
        continue
    b = poly.bounds
    found = None
    for ti, (tx, ty, t) in enumerate(texts):
        if text_used[ti]:
            continue
        if not (b[0] - 1 <= tx <= b[2] + 1 and b[1] - 1 <= ty <= b[3] + 1):
            continue
        if poly.contains(Point(tx, ty)):
            found = (ti, t)
            break
    if found is None:
        no_text.append(("no-text", round(poly.area)))
        continue
    ti, t = found
    text_used[ti] = True
    sx0, sy0 = sheet_rects[si][0], sheet_rects[si][1]
    v3_parts[t] = (si, affinity.translate(poly, xoff=-sx0, yoff=-sy0))

print(f"v3: {len(v3_parts)} numbered parts matched, {len(no_text)} unmatched")
if no_text:
    print("  unmatched:", no_text[:10])

# ---------------- Read CGR45final ----------------
parts_data, _ = read_dxf(FIN, panel_layers=["0"], exclude_layers=["大样"], number_layer=None)
parts = assign_numbers(parts_data)
print(f"final: {len(parts)} parts")

# ---------------- Template placement ----------------
def best_fit(part, v3_local):
    cen = part.outer_polygon.centroid
    vb = v3_local.bounds
    vcx, vcy = (vb[0] + vb[2]) / 2, (vb[1] + vb[3]) / 2
    best = None
    for rot in (0, 90, 180, 270):
        rp = part.outer_polygon if rot == 0 else affinity.rotate(
            part.outer_polygon, rot, origin=(cen.x, cen.y))
        rb = rp.bounds
        rcx, rcy = (rb[0] + rb[2]) / 2, (rb[1] + rb[3]) / 2
        for dx, dy in ((vb[0] - rb[0], vb[1] - rb[1]),
                       (vb[2] - rb[2], vb[3] - rb[3]),
                       (vcx - rcx, vcy - rcy)):
            moved = affinity.translate(rp, xoff=dx, yoff=dy)
            inter = moved.intersection(v3_local).area
            union = moved.area + v3_local.area - inter
            iou = inter / union if union > 0 else 0.0
            if best is None or iou > best[0]:
                best = (iou, rot, dx, dy)
    return best

sheet_parts = defaultdict(list)
missing_in_v3, low_iou = [], []
rot_stats = {0: 0, 90: 0, 180: 0, 270: 0}

for part in parts:
    num = part.number
    if num not in v3_parts:
        missing_in_v3.append(num)
        continue
    si, v3_local = v3_parts[num]
    iou, rot, dx, dy = best_fit(part, v3_local)
    rot_stats[rot] += 1
    if iou < 0.90:
        low_iou.append((num, round(iou, 3), rot))
    cen = part.outer_polygon.centroid
    if rot:
        new_outer = affinity.rotate(part.outer_polygon, rot, origin=(cen.x, cen.y))
        new_poly = affinity.rotate(part.polygon, rot, origin=(cen.x, cen.y))
        new_holes = [affinity.rotate(h, rot, origin=(cen.x, cen.y)) for h in part.holes]
        lp = affinity.rotate(Point(part.label_position), rot, origin=(cen.x, cen.y))
        new_label = (lp.x + dx, lp.y + dy)
    else:
        new_outer, new_poly, new_holes = part.outer_polygon, part.polygon, part.holes
        new_label = (part.label_position[0] + dx, part.label_position[1] + dy)
    new_outer = affinity.translate(new_outer, xoff=dx, yoff=dy)
    new_poly = affinity.translate(new_poly, xoff=dx, yoff=dy)
    new_holes = [affinity.translate(h, xoff=dx, yoff=dy) for h in new_holes]
    sheet_parts[si].append(Part(
        id=part.id, number=num, polygon=new_poly, outer_polygon=new_outer,
        holes=new_holes, original_number=part.original_number, area=part.area,
        label_position=new_label,
        outer_handle=part.outer_handle, hole_handles=part.hole_handles))

print(f"\nPlaced: {sum(len(v) for v in sheet_parts.values())} parts on {len(sheet_parts)} sheets")
print(f"Rotation stats: {rot_stats}")
if missing_in_v3:
    print(f"Missing in v3 ({len(missing_in_v3)}): {missing_in_v3[:10]}")
if low_iou:
    print(f"Low IoU (<0.9) ({len(low_iou)}): {low_iou[:15]}")

# ---------------- Repair sliver overlaps ----------------
def move_part(part, dx, dy):
    part.polygon = affinity.translate(part.polygon, xoff=dx, yoff=dy)
    part.outer_polygon = affinity.translate(part.outer_polygon, xoff=dx, yoff=dy)
    part.holes = [affinity.translate(h, xoff=dx, yoff=dy) for h in part.holes]
    part.label_position = (part.label_position[0] + dx, part.label_position[1] + dy)

def in_bounds(g):
    b = g.bounds
    return b[0] >= -0.5 and b[1] >= -0.5 and b[2] <= SHEET_W + 0.5 and b[3] <= SHEET_H + 0.5

def pair_overlap(g1, g2):
    b1, b2 = g1.bounds, g2.bounds
    if b1[2] < b2[0] or b2[2] < b1[0] or b1[3] < b2[1] or b2[3] < b1[1]:
        return 0.0
    return g1.intersection(g2).area

def chain_members(first, dx, dy, si):
    """Panels that must move together with `first` by (dx,dy); None if infeasible."""
    sp = sheet_parts[si]
    moved = {id(first): affinity.translate(first.outer_polygon, xoff=dx, yoff=dy)}
    members = [first]
    changed = True
    while changed:
        changed = False
        for p in sp:
            if id(p) in moved:
                continue
            for g in list(moved.values()):
                if pair_overlap(g, p.outer_polygon) > 0.5:
                    moved[id(p)] = affinity.translate(p.outer_polygon, xoff=dx, yoff=dy)
                    members.append(p)
                    changed = True
                    break
    for p in members:
        g = moved[id(p)]
        if not in_bounds(g):
            return None
        for q in sp:
            if id(q) in moved:
                continue
            if pair_overlap(g, q.outer_polygon) > 0.5:
                return None
    return members

repair_log = []
DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
STEPS = (1, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 25, 30, 40, 50)
for si in list(sheet_parts.keys()):
    for _ in range(20):
        sp = sheet_parts[si]
        bad = None
        for i, p1 in enumerate(sp):
            for j in range(i + 1, len(sp)):
                if pair_overlap(p1.outer_polygon, sp[j].outer_polygon) > 0.5:
                    bad = (p1, sp[j])
                    break
            if bad:
                break
        if not bad:
            break
        p1, p2 = bad
        best = None  # (chain_len, step, members, dx, dy)
        for mover in (p1, p2):
            for step in STEPS:
                for ux, uy in DIRS:
                    members = chain_members(mover, ux * step, uy * step, si)
                    if members is not None:
                        cand = (len(members), step, members, ux * step, uy * step)
                        if best is None or cand[:2] < best[:2]:
                            best = cand
        if best is None:
            repair_log.append((si + 1, f"UNFIXED {p1.number} x {p2.number}", 0, 0))
            break
        clen, step, members, dx, dy = best
        for p in members:
            move_part(p, dx, dy)
        repair_log.append((si + 1, "+".join(p.number for p in members), dx, dy))

print(f"\nRepair: {len(repair_log)} fixes")
for r in repair_log:
    print(f"  Sheet {r[0]}: {r[1]} moved ({r[2]}, {r[3]})")

# ---------------- Validation ----------------
oob, overlaps = [], []
for si, sp in sheet_parts.items():
    for i, p1 in enumerate(sp):
        if not in_bounds(p1.outer_polygon):
            oob.append((si + 1, p1.number, [round(v, 1) for v in p1.outer_polygon.bounds]))
        for j in range(i + 1, len(sp)):
            inter = pair_overlap(p1.outer_polygon, sp[j].outer_polygon)
            if inter > 0.5:
                overlaps.append((si + 1, p1.number, sp[j].number, round(inter)))

print(f"\n=== Validation ===")
print(f"Out of bounds: {len(oob)}")
for e in oob[:10]:
    print(f"  Sheet {e[0]}, {e[1]} bbox={e[2]}")
print(f"Overlaps: {len(overlaps)}")
for e in overlaps[:10]:
    print(f"  Sheet {e[0]}: {e[1]} x {e[2]} area={e[3]}")

# ---------------- Output ----------------
if not oob and not overlaps:
    sheets = [Sheet(index=si + 1, parts=sheet_parts[si], width=SHEET_W, height=SHEET_H, thickness=20)
              for si in sorted(sheet_parts.keys())]
    total_parts = sum(len(s.parts) for s in sheets)
    total_area = sum(p.area for s in sheets for p in s.parts)
    result = NestingResult(sheets=sheets, total_sheets=len(sheets),
                           total_parts=total_parts, total_part_area=total_area,
                           total_sheet_area=SHEET_W * SHEET_H * len(sheets), unit="metric")
    out_dir = Path(r"C:\Users\drago\stonebot_artificial_layout\output")
    out_dxf = str(out_dir / "CGR45final_nested_3200x1800.dxf")
    write_nested_dxf(result, out_dxf, unit_system="metric")
    print(f"\n** PASS **  sheets={len(sheets)} parts={total_parts} yield={result.yield_rate:.2f}%")
    print(f"DXF: {out_dxf}")
    out_json = str(out_dir / "CGR45final_report_3200x1800.json")
    report = {"sheet_dimensions": {"width": SHEET_W, "height": SHEET_H, "thickness": 20, "unit": "mm"},
              "total_sheets": result.total_sheets, "total_parts": total_parts,
              "total_part_area": round(total_area, 1), "total_sheet_area": result.total_sheet_area,
              "yield_rate": round(result.yield_rate, 2), "method": "template_from_v3"}
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"JSON: {out_json}")
else:
    print("\n** FAIL ** - not written")
