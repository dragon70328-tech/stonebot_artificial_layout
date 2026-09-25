import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _analysis.lns_lab import SHEET_W, SHEET_H
from src.constraints import PROFILES
from src.nesting import _materialize, validate_nesting, _sheet_used_area
from src.nesting_reviewer import find_reductions

with open("_analysis/cgr45_layout_beam4.pkl", "rb") as f:
    sheets = pickle.load(f)
res = _materialize(sheets, SHEET_W, SHEET_H, 20, "metric", 203)
sa = SHEET_W * SHEET_H
for s in sorted(res.sheets, key=lambda s: sum(p.area for p in s.parts))[:4]:
    nums = [(p.number, round(p.area / 1e6, 2)) for p in s.parts]
    print(f"sheet {s.index}: fill={sum(p.area for p in s.parts) / sa:.0%} {nums}")

profile = PROFILES["min_sheets"]
t0 = time.time()
cands, reduced = find_reductions(
    res, profile=profile, max_reductions=2, max_donor_attempts=10,
    min_gap=0.0, time_budget_s=180.0, per_donor_time_s=90.0)
print(f"回看: {len(cands)} 个减板候选, {res.total_sheets} -> "
      f"{reduced.total_sheets} 张 ({time.time() - t0:.1f}s)")
if cands:
    errs = validate_nesting(reduced, SHEET_W, SHEET_H)
    print("validate:", errs[:3] if errs else "OK")
