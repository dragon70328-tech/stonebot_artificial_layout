from __future__ import annotations

import json
from pathlib import Path


root = Path(__file__).resolve().parent
result = json.loads(
    (
        root
        / "fremont_st12_nesting_result_2800x1700_all_quarter_turns_detailed_201.json"
    ).read_text("utf-8")
)
manifest = json.loads(
    (
        root
        / "fremont_countertop_validation"
        / "户型20261102_台面板编号清单.json"
    ).read_text("utf-8")
)
sizes = {item["number"]: item["size_inches"] for item in manifest["countertops"]}

for sheet in sorted(result["sheets"], key=lambda item: item["fill_percent"])[:25]:
    print(
        f"Sheet {sheet['index']:3d} fill={sheet['fill_percent']:.3f}% "
        f"area={sheet['used_area_mm2'] / 1e6:.3f}m2"
    )
    for number in sheet["part_numbers"]:
        print(f"  {number:<16} {sizes[number]}")
