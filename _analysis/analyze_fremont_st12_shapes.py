from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import ezdxf
from shapely.geometry import Polygon


root = Path(__file__).resolve().parent
manifest = json.loads(
    (root / "fremont_countertop_validation" / "户型20261102_台面板编号清单.json").read_text(
        "utf-8"
    )
)
doc = ezdxf.readfile(
    root / "fremont_countertop_validation" / "户型20261102_台面板原位_编号.dxf"
)
msp = doc.modelspace()
groups = defaultdict(list)
for item in manifest["countertops"]:
    if item["material"] not in {"ST/1", "ST/2"}:
        continue
    entity = msp.entitydb[item["handle"]]
    polygon = Polygon([(x, y) for x, y in entity.get_points("xy")])
    min_x, min_y, max_x, max_y = polygon.bounds
    box_area = (max_x - min_x) * (max_y - min_y)
    groups[item["size_inches"]].append(polygon.area / box_area)

for size, fills in sorted(groups.items(), key=lambda item: -len(item[1])):
    unique = sorted({round(value, 6) for value in fills})
    print(
        f"{len(fills):3d} {size:<28} "
        f"fill={unique[0]:.4f}..{unique[-1]:.4f} variants={len(unique)}"
    )
