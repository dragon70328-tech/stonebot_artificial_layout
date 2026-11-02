from __future__ import annotations

import argparse
import json
from pathlib import Path

import ezdxf


parser = argparse.ArgumentParser()
parser.add_argument("size")
args = parser.parse_args()

root = Path(__file__).resolve().parent
manifest = json.loads(
    (root / "fremont_countertop_validation" / "户型20261102_台面板编号清单.json").read_text(
        "utf-8"
    )
)
record = next(
    item
    for item in manifest["countertops"]
    if item["material"] in {"ST/1", "ST/2"}
    and item["size_inches"].startswith(args.size)
)
doc = ezdxf.readfile(
    root / "fremont_countertop_validation" / "户型20261102_台面板原位_编号.dxf"
)
entity = doc.modelspace().entitydb[record["handle"]]
print(record["number"], record["handle"], record["bbox"])
print(list(entity.get_points("xy")))
