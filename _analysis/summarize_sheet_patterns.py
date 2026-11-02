from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


root = Path(__file__).resolve().parent
result = json.loads(
    (
        root / "fremont_st12_nesting_result_2800x1700_all_quarter_turns_detailed_201.json"
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

patterns = Counter(
    tuple(sorted(sizes[number] for number in sheet["part_numbers"]))
    for sheet in result["sheets"]
)
for pattern, count in patterns.most_common():
    print(f"{count:3d} x {', '.join(pattern)}")
