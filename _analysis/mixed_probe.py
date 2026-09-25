import pickle
import random
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
rng = random.Random(7)
worst2 = fills[:2]
best = 0
for trial in range(12):
    donors = rng.sample(fills[10:45], 3)
    sel = worst2 + donors
    sub = [pl.part for i in sel for pl in sheets[i]]
    bbox = sum((p.outer_polygon.bounds[2] - p.outer_polygon.bounds[0])
               * (p.outer_polygon.bounds[3] - p.outer_polygon.bounds[1])
               for p in sub)
    tiers = _build_tiers(sub, cache, (0, 90, 180, 270))
    counts = tuple(len(dq) for _k, _v, dq, _h in tiers)
    t0 = time.time()
    got = _beam_pack_multi(counts, tiers, 4, SHEET_W, SHEET_H, 400, 0.0,
                           deadline=time.time() + 25)
    if got:
        best += 1
    tag = "OK" if got else "no"
    print(f"trial {trial}: {len(sub)} parts {bbox / sa:.0%} -> {tag} "
          f"({time.time() - t0:.1f}s)", flush=True)
print("success:", best, "/ 12")
