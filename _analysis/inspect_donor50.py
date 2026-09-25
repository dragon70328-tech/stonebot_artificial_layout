import sys
sys.path.insert(0, r"C:\Users\drago\stonebot_artificial_layout_architecture")
import src.nesting_reviewer as nr
path = r"C:\Users\drago\stonebot_artificial_layout_architecture\output\20260923_220414_CGR45final\CGR45final_nested_3200x1800.dxf"
r = nr.load_nested_dxf(path)
donor = next(s for s in r.sheets if s.index == 50)
print("donor parts:")
for p in donor.parts:
    print(p.number, "area", round(p.area), "bounds", tuple(round(x) for x in p.outer_polygon.bounds))
rotations = [0,90,180,270]
for tgt in sorted(r.sheets, key=lambda s: s.index):
    if tgt.index == donor.index: continue
    free = nr.free_polygons_for_sheet(tgt)
    for p in donor.parts:
        cand = nr._placements_for_part(p, free, rotations, limit=1000)
        if cand:
            print("placement found:", p.number, "-> sheet", tgt.index, "count", len(cand))
