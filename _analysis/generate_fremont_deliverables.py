"""Generate numbered in-place DXF and material quantity XLSX for Fremont.

The source drawing is opened read-only and copied into a new DXF. Existing
geometry, text, dimensions and dimension styles are retained unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.xlsx_writer import write_xlsx


@dataclass(frozen=True)
class FrameRecord:
    handle: str
    bbox: tuple[float, float, float, float]
    unit_type: str
    material: str
    material_text: str
    unit_text_handle: str
    material_text_handle: str
    part_count: int
    dimension_count: int
    number: str = ""
    number_position: tuple[float, float] | None = None
    number_height: float | None = None


def effective_color(entity, doc) -> int:
    color = int(entity.dxf.get("color", 256))
    if color == 256:
        try:
            return int(doc.layers.get(entity.dxf.layer).dxf.color)
        except Exception:
            return 256
    return color


def entity_bbox(entity) -> tuple[float, float, float, float] | None:
    try:
        extents = bbox.extents([entity])
        if not extents.has_data:
            return None
        return (
            float(extents.extmin.x),
            float(extents.extmin.y),
            float(extents.extmax.x),
            float(extents.extmax.y),
        )
    except Exception:
        return None


def center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def point_inside(point: tuple[float, float], box: tuple[float, float, float, float]) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def bbox_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    clearance: float = 0.0,
) -> bool:
    return not (
        first[2] + clearance < second[0]
        or first[0] - clearance > second[2]
        or first[3] + clearance < second[1]
        or first[1] - clearance > second[3]
    )


def normalize_unit_type(raw: str) -> str:
    value = raw.split(":", 1)[1] if ":" in raw else raw
    value = re.sub(r"[^0-9A-Za-z]+", "", value).upper()
    if not value:
        raise ValueError(f"cannot normalize unit type: {raw!r}")
    return value


def normalize_material(raw: str) -> tuple[str, str]:
    match = re.search(r"(ST)\s*-?\s*(\d+)", raw, re.IGNORECASE)
    if not match:
        raise ValueError(f"cannot normalize material: {raw!r}")
    number = int(match.group(2))
    return f"ST/{number}", f"ST-{number}"


def collect_records(doc: ezdxf.document.Drawing) -> tuple[list[FrameRecord], list[Any]]:
    msp = doc.modelspace()
    all_entities = list(msp)
    boxes = {entity.dxf.handle: entity_bbox(entity) for entity in all_entities}
    texts = [entity for entity in all_entities if entity.dxftype() == "TEXT"]
    frames = []
    for entity in all_entities:
        if (
            entity.dxftype() == "LWPOLYLINE"
            and bool(entity.closed)
            and effective_color(entity, doc) == 1
        ):
            box = boxes[entity.dxf.handle]
            if box is not None and box[2] > box[0] and box[3] > box[1]:
                frames.append((entity, box))

    unit_texts = [
        entity
        for entity in texts
        if str(entity.dxf.text).upper().startswith("UNIT TYPE")
    ]
    material_texts = [entity for entity in texts if "材料" in str(entity.dxf.text)]

    records: list[FrameRecord] = []
    used_units: set[str] = set()
    used_materials: set[str] = set()
    for frame_entity, frame_box in frames:
        unit_candidates = []
        material_candidates = []
        for text_entity in unit_texts:
            box = boxes[text_entity.dxf.handle]
            if box is not None and point_inside(center(box), frame_box):
                unit_candidates.append((text_entity, box))
        for text_entity in material_texts:
            box = boxes[text_entity.dxf.handle]
            if box is not None and point_inside(center(box), frame_box):
                material_candidates.append((text_entity, box))

        if len(unit_candidates) != 1:
            raise ValueError(
                f"frame {frame_entity.dxf.handle} has {len(unit_candidates)} unit labels"
            )
        if len(material_candidates) != 1:
            raise ValueError(
                f"frame {frame_entity.dxf.handle} has "
                f"{len(material_candidates)} material labels"
            )

        unit_text, _ = unit_candidates[0]
        material_text, _ = material_candidates[0]
        used_units.add(unit_text.dxf.handle)
        used_materials.add(material_text.dxf.handle)
        unit_type = normalize_unit_type(str(unit_text.dxf.text))
        material, material_display = normalize_material(str(material_text.dxf.text))

        part_count = 0
        dimension_count = 0
        for entity in all_entities:
            box = boxes[entity.dxf.handle]
            if box is None:
                continue
            if entity.dxftype() == "DIMENSION":
                if point_inside(center(box), frame_box):
                    dimension_count += 1
            elif (
                entity.dxftype() == "LWPOLYLINE"
                and entity.dxf.handle != frame_entity.dxf.handle
                and bool(entity.closed)
                and effective_color(entity, doc) != 1
            ):
                if point_inside(center(box), frame_box):
                    part_count += 1

        records.append(
            FrameRecord(
                handle=str(frame_entity.dxf.handle),
                bbox=frame_box,
                unit_type=unit_type,
                material=material,
                material_text=material_display,
                unit_text_handle=str(unit_text.dxf.handle),
                material_text_handle=str(material_text.dxf.handle),
                part_count=part_count,
                dimension_count=dimension_count,
            )
        )

    if len(records) != 600:
        raise ValueError(f"expected 600 red frames, found {len(records)}")
    if len(used_units) != len(records):
        raise ValueError(f"unit labels not one-to-one: {len(used_units)}")
    if len(used_materials) != len(records):
        raise ValueError(f"material labels not one-to-one: {len(used_materials)}")
    return records, [entity for entity, _ in frames]


def assign_numbers(records: list[FrameRecord]) -> list[FrameRecord]:
    grouped: dict[tuple[str, str], list[FrameRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.unit_type, record.material)].append(record)

    numbered: list[FrameRecord] = []
    for (unit_type, material), group in grouped.items():
        ordered = sorted(
            group,
            key=lambda item: (-item.bbox[3], item.bbox[0], item.handle),
        )
        for index, record in enumerate(ordered, start=1):
            numbered.append(
                FrameRecord(
                    **{
                        **record.__dict__,
                        "number": f"{unit_type}-{material}-{index:03d}",
                    }
                )
            )
    numbered.sort(key=lambda item: item.handle)
    return numbered


def find_number_position(
    record: FrameRecord,
    obstacles: list[tuple[float, float, float, float]],
    box_width: float,
    box_height: float,
) -> tuple[float, float]:
    x0, y0, x1, y1 = record.bbox
    margin = 55.0
    clearance = 12.0
    usable_x0 = x0 + margin
    usable_x1 = x1 - margin - box_width
    usable_y0 = y0 + margin
    usable_y1 = y1 - margin - box_height
    if usable_x1 < usable_x0 or usable_y1 < usable_y0:
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

    candidates: list[tuple[float, tuple[float, float]]] = []
    y = usable_y0
    while y <= usable_y1:
        x = usable_x0
        while x <= usable_x1:
            candidate_box = (x, y, x + box_width, y + box_height)
            if not any(bbox_overlap(candidate_box, obstacle, clearance) for obstacle in obstacles):
                cx = x + box_width / 2.0
                cy = y + box_height / 2.0
                frame_center_x = (x0 + x1) / 2.0
                score = (y - usable_y0) + abs(cx - frame_center_x) * 0.12
                candidates.append((score, (cx, cy)))
            x += 35.0
        y += 35.0

    if candidates:
        return min(candidates, key=lambda item: item[0])[1]

    # Fallback: use the lower-right interior, the least crowded area in this
    # source drawing. The small label may overlap a dimension line only if the
    # geometry itself leaves no free rectangle.
    return (x1 - margin - box_width / 2.0, y0 + margin + box_height / 2.0)


def add_numbers(
    doc: ezdxf.document.Drawing,
    records: list[FrameRecord],
) -> list[FrameRecord]:
    msp = doc.modelspace()
    if "PART_NUMBER" not in doc.layers:
        doc.layers.add("PART_NUMBER", color=3)
    source_style = "Standard"
    unit_entities = [e for e in msp if e.dxftype() == "TEXT" and str(e.dxf.text).startswith("UNIT TYPE")]
    if unit_entities:
        source_style = str(unit_entities[0].dxf.get("style", "Standard"))

    all_boxes = {}
    for entity in msp:
        box = entity_bbox(entity)
        if box is not None:
            all_boxes[entity.dxf.handle] = box

    updated: list[FrameRecord] = []
    for record in records:
        obstacles = [
            box
            for handle, box in all_boxes.items()
            if handle != record.handle
            and point_inside(center(box), record.bbox)
        ]
        accepted = None
        for label_height in (100.0, 92.0, 84.0, 76.0):
            label = msp.add_text(
                record.number,
                height=label_height,
                rotation=0,
                dxfattribs={
                    "layer": "PART_NUMBER",
                    "color": 3,
                    "style": source_style,
                },
            )
            label.set_placement((0.0, 0.0), align=TextEntityAlignment.MIDDLE_CENTER)
            measured = entity_bbox(label)
            if measured is None:
                raise ValueError(f"cannot measure generated label {record.number}")
            measured_width = measured[2] - measured[0]
            measured_height = measured[3] - measured[1]
            measured_center_x = (measured[0] + measured[2]) / 2.0
            measured_center_y = (measured[1] + measured[3]) / 2.0
            target_center = find_number_position(
                record,
                obstacles,
                measured_width,
                measured_height,
            )
            position = (
                target_center[0] - measured_center_x,
                target_center[1] - measured_center_y,
            )
            label.set_placement(position, align=TextEntityAlignment.MIDDLE_CENTER)
            actual_box = entity_bbox(label)
            collision = actual_box is not None and any(
                bbox_overlap(actual_box, obstacle, 1.0)
                for obstacle in obstacles
            )
            if not collision:
                accepted = (label, label_height, target_center)
                break
            msp.delete_entity(label)
        if accepted is None:
            raise ValueError(f"cannot place number label without overlap: {record.number}")
        label, label_height, target_center = accepted
        updated.append(
            FrameRecord(
                **{
                    **record.__dict__,
                    "number_position": target_center,
                    "number_height": label_height,
                }
            )
        )
    return updated


def natural_key(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value)
    ]


def write_material_xlsx(path: Path, records: list[FrameRecord]) -> Path:
    grouped: dict[tuple[str, str], list[FrameRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.material, record.unit_type)].append(record)

    rows: list[list[Any]] = [
        ["材料", "户型", "数量（图框/套）", "图内实体轮廓数", "编号起", "编号止"]
    ]
    material_frame_totals = Counter()
    material_part_totals = Counter()
    for material in sorted({record.material for record in records}, key=natural_key):
        material_records = [record for record in records if record.material == material]
        unit_types = sorted(
            {record.unit_type for record in material_records},
            key=natural_key,
        )
        for unit_type in unit_types:
            group = sorted(grouped[(material, unit_type)], key=lambda item: item.number)
            frame_count = len(group)
            part_count = sum(item.part_count for item in group)
            material_frame_totals[material] += frame_count
            material_part_totals[material] += part_count
            rows.append(
                [
                    material,
                    unit_type,
                    frame_count,
                    part_count,
                    group[0].number,
                    group[-1].number,
                ]
            )
        rows.append(
            [
                f"{material} 小计",
                "",
                material_frame_totals[material],
                material_part_totals[material],
                "",
                "",
            ]
        )

    rows.append(
        [
            "合计",
            "",
            sum(material_frame_totals.values()),
            sum(material_part_totals.values()),
            "",
            "",
        ]
    )
    write_xlsx(path, rows, sheet_name="材料数量", header=True)
    return path


def validate_output(
    baseline_entity_counts: Counter[str],
    baseline_text_counts: Counter[str],
    output_path: Path,
    records: list[FrameRecord],
) -> dict[str, Any]:
    output_doc = ezdxf.readfile(output_path)
    output_msp = output_doc.modelspace()
    output_texts = [str(item.dxf.text) for item in output_msp if item.dxftype() == "TEXT"]
    number_texts = [
        item
        for item in output_msp
        if item.dxftype() == "TEXT" and item.dxf.get("layer") == "PART_NUMBER"
    ]
    generated_numbers = sorted(str(item.dxf.text) for item in number_texts)
    expected_numbers = sorted(record.number for record in records)
    if generated_numbers != expected_numbers:
        raise ValueError("generated number labels do not match manifest")
    if len(set(generated_numbers)) != len(generated_numbers):
        raise ValueError("generated number labels are not unique")

    output_counts = Counter(entity.dxftype() for entity in output_msp)
    if output_counts["LWPOLYLINE"] != baseline_entity_counts["LWPOLYLINE"]:
        raise ValueError("LWPOLYLINE count changed")
    if output_counts["DIMENSION"] != baseline_entity_counts["DIMENSION"]:
        raise ValueError("DIMENSION count changed")
    if output_counts["TEXT"] != baseline_entity_counts["TEXT"] + len(records):
        raise ValueError("TEXT count is not source + generated labels")

    output_original_texts = Counter(
        text for text in output_texts if re.match(r"^(UNIT TYPE|材料|L[2-5]$|[ABC]户型)", text)
    )
    if baseline_text_counts != output_original_texts:
        raise ValueError("source annotation text multiset changed")

    return {
        "dxf_version": output_doc.dxfversion,
        "dwgcodepage": output_doc.header.get("$DWGCODEPAGE"),
        "text_count": output_counts["TEXT"],
        "added_number_count": len(number_texts),
        "dimension_count": output_counts["DIMENSION"],
        "lwpolyline_count": output_counts["LWPOLYLINE"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()

    doc = ezdxf.readfile(source)
    baseline_entity_counts = Counter(entity.dxftype() for entity in doc.modelspace())
    baseline_text_counts = Counter(
        str(entity.dxf.text)
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT"
    )
    frames, _ = collect_records(doc)
    records = assign_numbers(frames)
    numbered_records = add_numbers(doc, records)

    stem = source.stem
    output_dxf = output_dir / f"{stem}_原位_编号.dxf"
    output_xlsx = output_dir / f"{stem}_材料数量.xlsx"
    doc.header["$DWGCODEPAGE"] = "ANSI_936"
    # R2007+ DXF stores textual content as UTF-8 even when the legacy
    # DWGCODEPAGE tag remains ANSI_936 for compatibility with Chinese CAD.
    doc.saveas(output_dxf, encoding="utf-8")
    write_material_xlsx(output_xlsx, numbered_records)
    validation = validate_output(
        baseline_entity_counts,
        baseline_text_counts,
        output_dxf,
        numbered_records,
    )

    manifest = {
        "source": str(source),
        "source_sha256": source_hash,
        "output_dxf": str(output_dxf),
        "output_xlsx": str(output_xlsx),
        "frame_count": len(frames),
        "part_outline_count": sum(record.part_count for record in records),
        "dimension_count": sum(record.dimension_count for record in records),
        "materials": dict(Counter(record.material for record in records)),
        "unit_types": dict(Counter(record.unit_type for record in records)),
        "validation": validation,
        "frames": [
            {
                "handle": record.handle,
                "number": record.number,
                "unit_type": record.unit_type,
                "material": record.material,
                "part_count": record.part_count,
                "dimension_count": record.dimension_count,
                "bbox": list(record.bbox),
                "number_position": list(record.number_position or []),
                "number_height": record.number_height,
            }
            for record in numbered_records
        ],
    }
    manifest_path = output_dir / f"{stem}_编号清单.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
