import sys, math
sys.path.insert(0, r"C:\Users\drago\stonebot_artificial_layout_architecture")
from src.nesting_reviewer import load_nested_dxf, free_polygons_for_sheet
path = r"C:\Users\drago\stonebot_artificial_layout_architecture\output\20260923_220414_CGR45final\CGR45final_nested_3200x1800.dxf"
r = load_nested_dxf(path)
for s in sorted(r.sheets, key=lambda x: len(x.parts)):
    used = sum(p.area for p in s.parts)
    free = sum(f.area for f in free_polygons_for_sheet(s))
    print(f"sheet {s.index:2d} parts={len(s.parts):2d} used={used:9.0f} free={free:9.0f} max_part={max((p.area for p in s.parts), default=0):9.0f}")
