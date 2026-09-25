import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _analysis.lns_lab import SHEET_W, SHEET_H
from src.beam_nesting import _beam_pack_multi, _build_tiers
from src.nesting import _sheet_used_area

with open("_analysis/cgr45_layout_beam3.pkl", "rb") as f:
    sheets = pickle.load(f)
sa = SHEET_W * SHEET_H
fills = sorted(range(len(sheets)), key=lambda i: _sheet_used_area(sheets[i]))
cache = {}
for k in (4, 5, 6, 7, 8):
    sel = fills[:k]
    sub = [pl.part for i in sel for pl in sheets[i]]
    bbox = sum((p.outer_polygon.bounds[2] - p.outer_polygon.bounds[0])
               * (p.outer_polygon.bounds[3] - p.outer_polygon.bounds[1])
               for p in sub)
    tiers = _build_tiers(sub, cache, (0, 90, 180, 270))
    counts = tuple(len(dq) for _k, _v, dq, _h in tiers)
    t0 = time.time()
    got = _beam_pack_multi(counts, tiers, k - 1, SHEET_W, SHEET_H, 300,
                           0.0, deadline=time.time() + 60)
    ok = "FEASIBLE" if got else "infeasible"
    print(f"worst{k}->{k - 1}: {len(sub)} parts {bbox / sa:.0%} "
          f"{ok} ({time.time() - t0:.1f}s)", flush=True)
