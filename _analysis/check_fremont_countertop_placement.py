from __future__ import annotations

import json
from pathlib import Path

import ezdxf
from ezdxf import bbox
from shapely.geometry import Polygon, box


def entity_box(entity):
    extents = bbox.extents([entity])
    if not extents.has_data:
        return None
    return (
        extents.extmin.x,
        extents.extmin.y,
        extents.extmax.x,
        extents.extmax.y,
    )


def overlaps(first, second, clearance=0.5):
    return not (
        first[2] + clearance < second[0]
        or first[0] - clearance > second[2]
        or first[3] + clearance < second[1]
        or first[1] - clearance > second[3]
    )


def main() -> int:
    source = Path(
        r"C:\Users\drago\Desktop\临时文件\AI识图文件\Fremont building\户型20261102.dxf"
    )
    output = Path(
        "_analysis/fremont_countertop_validation/户型20261102_台面板原位_编号.dxf"
    )
    manifest = json.loads(
        Path(
            "_analysis/fremont_countertop_validation/户型20261102_台面板编号清单.json"
        ).read_text("utf-8")
    )
    source_doc = ezdxf.readfile(source)
    output_doc = ezdxf.readfile(output)
    source_msp = source_doc.modelspace()
    output_msp = output_doc.modelspace()
    output_entities = list(output_msp)
    output_boxes = {
        entity.dxf.handle: entity_box(entity) for entity in output_entities
    }

    records_by_number = {item["number"]: item for item in manifest["countertops"]}
    labels = [
        entity
        for entity in output_entities
        if entity.dxftype() == "TEXT"
        and entity.dxf.get("layer") == "COUNTERTOP_NUMBER"
    ]
    outside = []
    collisions = []
    for label in labels:
        number = str(label.dxf.text)
        record = records_by_number[number]
        source_entity = source_msp.entitydb[record["handle"]]
        polygon = Polygon(
            [(float(x), float(y)) for x, y in source_entity.get_points("xy")]
        )
        label_box = output_boxes[label.dxf.handle]
        if not polygon.buffer(0.01).covers(box(*label_box)):
            outside.append(number)
        hits = []
        for entity in output_entities:
            if entity is label or entity.dxf.handle == record["handle"]:
                continue
            other = output_boxes[entity.dxf.handle]
            if other is None:
                continue
            if overlaps(label_box, other):
                hits.append((entity.dxftype(), entity.dxf.handle))
        if hits:
            collisions.append((number, hits))

    red_frames = [
        entity
        for entity in output_entities
        if entity.dxftype() == "LWPOLYLINE"
        and entity.closed
        and int(entity.dxf.get("color", 256)) == 1
    ]
    print("labels", len(labels), "unique", len({str(e.dxf.text) for e in labels}))
    print("outside_panel", len(outside), outside[:20])
    print("collisions", len(collisions), collisions[:20])
    print("red_frames", len(red_frames))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
