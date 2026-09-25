import time, sys
from pathlib import Path
sys.path.insert(0, r"C:\Users\drago\stonebot_artificial_layout_architecture")
from src.constraints import PROFILE_MIN_SHEETS
import src.nesting_reviewer as nr

path = r"C:\Users\drago\stonebot_artificial_layout_architecture\output\20260923_220414_CGR45final\CGR45final_nested_3200x1800.dxf"
result = nr.load_nested_dxf(path)
print("loaded sheets:", result.total_sheets, "parts:", result.total_parts)

orig = nr._try_reduce_donor
def aggressive(donor, result, profile, clearance=0.0, deadline=None,
               max_nodes=500000, node_counter=None, placement_limit=200):
    return orig(donor, result, profile, clearance, deadline,
                max_nodes, node_counter, placement_limit)
nr._try_reduce_donor = aggressive

t0 = time.time()
candidates, reduced = nr.find_reductions(
    result,
    profile=PROFILE_MIN_SHEETS,
    max_reductions=5,
    max_donor_attempts=None,
    min_gap=0.0,
    time_budget_s=900.0,
    per_donor_time_s=120.0,
)
print("elapsed", round(time.time()-t0,1), "candidates", len(candidates))
for c in candidates:
    print("candidate removed", c.removed_sheet_index, "moved", c.moved_parts, "targets", c.target_sheets)
print("reduced sheets:", reduced.total_sheets)
