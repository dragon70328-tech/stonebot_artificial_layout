from __future__ import annotations

import json
from pathlib import Path

import ezdxf
from ezdxf import bbox


def extents(entity):
    result = bbox.extents([entity])
    if not result.has_data:
        return None
    return (result.extmin.x, result.extmin.y, result.extmax.x, result.extmax.y)


def overlap(a, b, clearance=0.0):
    return not (
        a[2] + clearance < b[0]
        or a[0] - clearance > b[2]
        or a[3] + clearance < b[1]
        or a[1] - clearance > b[3]
    )


def main() -> int:
    output = Path("_analysis/fremont_validation/户型20261102_原位_编号.dxf")
    manifest = json.loads(
        Path("_analysis/fremont_validation/户型20261102_编号清单.json").read_text("utf-8")
    )
    frame_by_handle = {item["handle"]: item for item in manifest["frames"]}
    doc = ezdxf.readfile(output)
    entities = list(doc.modelspace())
    boxes = {entity.dxf.handle: extents(entity) for entity in entities}
    labels = [
        entity
        for entity in entities
        if entity.dxftype() == "TEXT" and entity.dxf.get("layer") == "PART_NUMBER"
    ]
    label_boxes = {entity.dxf.handle: boxes[entity.dxf.handle] for entity in labels}
    collisions = []
    outside = []
    label_overlaps = []
    for label in labels:
        box = label_boxes[label.dxf.handle]
        center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        candidates = [
            record
            for record in frame_by_handle.values()
            if record["bbox"][0] <= center[0] <= record["bbox"][2]
            and record["bbox"][1] <= center[1] <= record["bbox"][3]
        ]
        if len(candidates) != 1:
            outside.append((label.dxf.handle, label.dxf.text, len(candidates)))
            continue
        frame = candidates[0]
        if (
            box[0] < frame["bbox"][0] - 0.1
            or box[1] < frame["bbox"][1] - 0.1
            or box[2] > frame["bbox"][2] + 0.1
            or box[3] > frame["bbox"][3] + 0.1
        ):
            outside.append((label.dxf.handle, label.dxf.text, "crosses frame edge"))
        hits = []
        for entity in entities:
            if entity is label or entity.dxf.handle == frame["handle"]:
                continue
            other = boxes[entity.dxf.handle]
            if other is None:
                continue
            if not (
                frame["bbox"][0] <= (other[0] + other[2]) / 2.0 <= frame["bbox"][2]
                and frame["bbox"][1] <= (other[1] + other[3]) / 2.0 <= frame["bbox"][3]
            ):
                continue
            if overlap(box, other, clearance=1.0):
                hits.append((entity.dxftype(), entity.dxf.handle))
        if hits:
            collisions.append((label.dxf.handle, label.dxf.text, hits))
    for index, first in enumerate(labels):
        for second in labels[index + 1 :]:
            if overlap(label_boxes[first.dxf.handle], label_boxes[second.dxf.handle], 1.0):
                label_overlaps.append((first.dxf.text, second.dxf.text))

    print("labels", len(labels))
    print("outside frames", len(outside), outside[:20])
    print("label collisions", len(collisions), collisions[:30])
    print("label-label overlaps", len(label_overlaps), label_overlaps[:20])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
