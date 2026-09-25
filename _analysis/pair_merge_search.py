import sys, time, itertools
sys.path.insert(0, r"C:\Users\drago\stonebot_artificial_layout_architecture")
from src.nesting_reviewer import load_nested_dxf
from src.nesting import _nest_single, _make_sort_key

path = r"C:\Users\drago\stonebot_artificial_layout_architecture\output\20260923_220414_CGR45final\CGR45final_nested_3200x1800.dxf"
r = load_nested_dxf(path)
sheets = r.sheets
rot = (0, 90, 180, 270)
cache = {}
configs = [("area","skyline"),("short","skyline"),("long","skyline"),("area","col"),("short","col"),("long","col"),("area","contact"),("short","contact")]
found = []
t0 = time.time()
for a, b in itertools.combinations(sheets, 2):
    parts = a.parts + b.parts
    area = sum(p.area for p in parts)
    if area > 3200*1800 + 1e-6:
        continue
    for sort_name, mode in configs:
        try:
            res = _nest_single(parts, 3200, 1800, _make_sort_key(sort_name, 0), mode, cache,
                               max_sheets=1, rotations=rot, min_gap=0.0)
        except Exception:
            res = None
        if res is not None and len(res) == 1:
            found.append((a.index, b.index, sort_name, mode, [p.number for p in parts]))
            print("MERGE", a.index, b.index, sort_name, mode)
            break
print("elapsed", round(time.time()-t0,1), "found", len(found))
for f in found[:20]:
    print(f)
