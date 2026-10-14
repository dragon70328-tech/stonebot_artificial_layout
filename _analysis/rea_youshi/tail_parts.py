"""Print bounding-box sizes of the parts sitting on the last sheets of a run.

Usage:
    python -X utf8 _analysis/rea_youshi/tail_parts.py output/<run_dir> [...]
"""

import glob
import json
import os
import sys


def wkt_poly(wkt):
    if not wkt or "(" not in wkt:
        return []
    body = wkt[wkt.index("(") + 1:wkt.rindex(")")]
    ring = body.split(")")[0].lstrip("(")
    out = []
    for pair in ring.split(","):
        xy = pair.split()
        if len(xy) >= 2:
            out.append((float(xy[0]), float(xy[1])))
    return out


def main():
    for d in sys.argv[1:]:
        rep = json.load(open(glob.glob(os.path.join(d, "*report_3200x1800.json"))[0], encoding="utf-8"))
        rec = json.load(open(glob.glob(os.path.join(d, "*recognized.json"))[0], encoding="utf-8"))
        poly = {p["number"]: wkt_poly(p.get("outer_geometry_wkt")) for p in rec["panels"]}
        print(f"== {os.path.basename(d)}  sheets={len(rep['sheets'])}")
        for sheet in rep["sheets"][-3:]:
            for n in sheet["parts"]:
                pts = poly.get(n) or []
                if not pts:
                    continue
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                w = max(xs) - min(xs)
                h = max(ys) - min(ys)
                print(f"   sheet {sheet['index']:>3}  {n:<8} bbox {w:7.1f} x {h:7.1f} mm"
                      f"  ({max(w, h)/1000:.2f} m 长边)")


if __name__ == "__main__":
    main()
