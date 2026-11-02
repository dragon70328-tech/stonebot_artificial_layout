"""Print focused summaries from ``fremont_source_inventory.json``."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def escaped(value: object) -> str:
    return str(value).encode("unicode_escape").decode("ascii")


root = Path(__file__).resolve().parent
data = json.loads((root / "fremont_source_inventory.json").read_text("utf-8"))

print("TEXT FREQUENCIES")
text_counts = Counter(
    " ".join(str(item.get("text", "")).split()) for item in data["texts"]
)
for text, count in text_counts.most_common():
    print(f"{count:4d} {escaped(text)}")

print("\nTEXT HEIGHTS")
height_counts = Counter(item.get("height") for item in data["texts"])
for height, count in height_counts.most_common():
    print(f"{count:4d} {height}")

print("\nDIMENSION OVERRIDES / TYPES")
override_counts = Counter(
    item.get("text_override") for item in data["dimensions"]
)
for value, count in override_counts.most_common(80):
    print(f"{count:4d} {escaped(value)}")
print("dimtypes", Counter(item.get("dimtype") for item in data["dimensions"]))

print("\nFRAME GEOMETRY")
frames = data["closed_frame_candidates"]
print("count", len(frames))
size_counts = Counter((round(item["width"], 3), round(item["height"], 3)) for item in frames)
for size, count in size_counts.most_common(80):
    print(f"{count:4d} {size}")

print("\nFRAMES BY AREA DESC")
for item in sorted(
    frames,
    key=lambda value: value["width"] * value["height"],
    reverse=True,
)[:160]:
    print(
        f"{item['handle']:>6} color={item['color']} layer={escaped(item['layer'])} "
        f"verts={item['vertex_count']:3d} "
        f"bbox={[round(v, 3) for v in item['bbox']]} "
        f"size=({item['width']:.3f},{item['height']:.3f})"
    )

print("\nTEXT ITEMS BY Y DESC")
for item in sorted(
    data["texts"],
    key=lambda value: (
        -(value.get("point") or [0.0, -1e30])[1],
        (value.get("point") or [0.0, 0.0])[0],
    ),
):
    print(
        f"{escaped(item.get('text', '')):<35} "
        f"p={item.get('point')} h={item.get('height')} "
        f"bbox={item.get('bbox')} handle={item.get('handle')}"
    )
