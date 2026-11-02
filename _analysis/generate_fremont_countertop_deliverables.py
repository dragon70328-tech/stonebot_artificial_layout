"""Number Fremont countertop polygons and produce a material quantity workbook.

Red closed polylines are only group boundaries. They are removed from the
delivered DXF after their enclosed countertop polygons have been identified.
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
from shapely.geometry import Point, Polygon, box

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.xlsx_writer import write_xlsx


@dataclass(frozen=True)
class FrameInfo:
    handle: str
    entity: Any
    bbox: tuple[float, float, float, float]
    unit_type: str
    material: str
    material_text: str


@dataclass(frozen=True)
class Countertop:
    handle: str
    frame_handle: str
    bbox: tuple[float, float, float, float]
    points: tuple[tuple[float, float], ...]
    unit_type: str
    material: str
    material_text: str
    number: str = ""
    number_position: tuple[float, float] | None = None
    number_height: float | None = None


def effective_color(entity: Any, doc: ezdxf.document.Drawing) -> int:
    color = int(entity.dxf.get("color", 256))
    if color == 256:
        try:
            return int(doc.layers.get(entity.dxf.layer).dxf.color)
        except Exception:
            return 256
    return color


def entity_bbox(entity: Any) -> tuple[float, float, float, float] | None:
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


def text_center(box_value: tuple[float, float, float, float]) -> tuple[float, float]:
    return (
        (box_value[0] + box_value[2]) / 2.0,
        (box_value[1] + box_value[3]) / 2.0,
    )


def point_inside(point: tuple[float, float], area: tuple[float, float, float, float]) -> bool:
    return area[0] <= point[0] <= area[2] and area[1] <= point[1] <= area[3]


def boxes_overlap(
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


def boxes_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    clearance: float = 0.0,
) -> bool:
    return boxes_overlap(first, second, clearance)


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


def collect_frames(doc: ezdxf.document.Drawing) -> tuple[list[FrameInfo], dict[str, Any]]:
    msp = doc.modelspace()
    all_entities = list(msp)
    boxes = {entity.dxf.handle: entity_bbox(entity) for entity in all_entities}
    texts = [
        entity
        for entity in all_entities
        if entity.dxftype() == "TEXT"
    ]
    frames: list[FrameInfo] = []
    for entity in all_entities:
        if not (
            entity.dxftype() == "LWPOLYLINE"
            and bool(entity.closed)
            and effective_color(entity, doc) == 1
        ):
            continue
        frame_box = boxes[entity.dxf.handle]
        if frame_box is None:
            continue
        unit_candidates = []
        material_candidates = []
        for text_entity in texts:
            text = str(text_entity.dxf.text)
            text_box = boxes[text_entity.dxf.handle]
            if text_box is None or not point_inside(text_center(text_box), frame_box):
                continue
            if text.upper().startswith("UNIT TYPE"):
                unit_candidates.append((text_entity, text_box))
            elif "材料" in text:
                material_candidates.append((text_entity, text_box))
        if len(unit_candidates) != 1:
            raise ValueError(
                f"red frame {entity.dxf.handle} has {len(unit_candidates)} unit labels"
            )
        if len(material_candidates) != 1:
            raise ValueError(
                f"red frame {entity.dxf.handle} has "
                f"{len(material_candidates)} material labels"
            )
        unit_entity, unit_box = unit_candidates[0]
        material_entity, material_box = material_candidates[0]
        unit_type = normalize_unit_type(str(unit_entity.dxf.text))
        material, material_text = normalize_material(str(material_entity.dxf.text))
        frames.append(
            FrameInfo(
                handle=str(entity.dxf.handle),
                entity=entity,
                bbox=frame_box,
                unit_type=unit_type,
                material=material,
                material_text=material_text,
            )
        )
    if len(frames) != 600:
        raise ValueError(f"expected 600 red frames, found {len(frames)}")
    return frames, boxes


def collect_countertops(
    doc: ezdxf.document.Drawing,
    frames: list[FrameInfo],
    boxes: dict[str, Any],
) -> list[Countertop]:
    msp = doc.modelspace()
    all_entities = list(msp)
    countertops: list[Countertop] = []
    used_handles: set[str] = set()
    for frame in frames:
        frame_parts = []
        for entity in all_entities:
            if entity.dxftype() != "LWPOLYLINE" or not bool(entity.closed):
                continue
            if effective_color(entity, doc) == 1:
                continue
            part_box = boxes[entity.dxf.handle]
            if part_box is None or not point_inside(text_center(part_box), frame.bbox):
                continue
            points = tuple(
                (float(point[0]), float(point[1]))
                for point in entity.get_points("xy")
            )
            if len(points) < 3:
                continue
            shape = Polygon(points)
            if shape.is_empty or not shape.is_valid or shape.area <= 0:
                raise ValueError(
                    f"countertop {entity.dxf.handle} in frame {frame.handle} "
                    "is not a valid closed polygon"
                )
            frame_parts.append(
                Countertop(
                    handle=str(entity.dxf.handle),
                    frame_handle=frame.handle,
                    bbox=part_box,
                    points=points,
                    unit_type=frame.unit_type,
                    material=frame.material,
                    material_text=frame.material_text,
                )
            )
            used_handles.add(str(entity.dxf.handle))
        if not frame_parts:
            raise ValueError(f"red frame {frame.handle} contains no countertop")
        countertops.extend(frame_parts)

    if len(countertops) != 908:
        raise ValueError(f"expected 908 countertops, found {len(countertops)}")
    if len(used_handles) != len(countertops):
        raise ValueError("countertop polylines are assigned to multiple frames")
    return countertops


def assign_numbers(
    frames: list[FrameInfo],
    countertops: list[Countertop],
) -> list[Countertop]:
    frame_order = {
        frame.handle: index
        for index, frame in enumerate(
            sorted(
                frames,
                key=lambda frame: (-frame.bbox[3], frame.bbox[0], frame.handle),
            )
        )
    }
    ordered = sorted(
        countertops,
        key=lambda part: (
            frame_order[part.frame_handle],
            -part.bbox[3],
            part.bbox[0],
            part.handle,
        ),
    )
    grouped: dict[tuple[str, str], list[Countertop]] = defaultdict(list)
    for part in ordered:
        grouped[(part.unit_type, part.material)].append(part)

    numbered: list[Countertop] = []
    for (_, _), group in grouped.items():
        for index, part in enumerate(group, start=1):
            numbered.append(
                Countertop(
                    **{
                        **part.__dict__,
                        "number": f"{part.unit_type}-{part.material}-{index:03d}",
                    }
                )
            )
    numbered.sort(key=lambda part: (frame_order[part.frame_handle], part.handle))
    return numbered


def candidate_centers(
    area: Polygon,
    text_width: float,
    text_height: float,
    step: float = 30.0,
):
    min_x, min_y, max_x, max_y = area.bounds
    half_width = text_width / 2.0
    half_height = text_height / 2.0
    if max_x - min_x < text_width or max_y - min_y < text_height:
        return
    x = min_x + half_width + 8.0
    while x <= max_x - half_width - 8.0:
        y = min_y + half_height + 8.0
        while y <= max_y - half_height - 8.0:
            yield x, y
            y += step
        x += step


def free_position(
    shape: Polygon,
    anchor: Point,
    obstacles: list[tuple[float, float, float, float]],
    text_width: float,
    text_height: float,
) -> tuple[float, float] | None:
    fitted_shape = shape.buffer(0.01)
    candidates = []
    for cx, cy in candidate_centers(shape, text_width, text_height):
        candidate_box = (
            cx - text_width / 2.0,
            cy - text_height / 2.0,
            cx + text_width / 2.0,
            cy + text_height / 2.0,
        )
        if not fitted_shape.covers(box(*candidate_box)):
            continue
        if any(boxes_overlap(candidate_box, obstacle, 8.0) for obstacle in obstacles):
            continue
        score = (cx - anchor.x) ** 2 + (cy - anchor.y) ** 2
        candidates.append((score, (cx, cy)))
    if candidates:
        return min(candidates, key=lambda item: item[0])[1]
    return None


def add_countertop_numbers(
    doc: ezdxf.document.Drawing,
    records: list[Countertop],
    boxes: dict[str, Any],
) -> list[Countertop]:
    msp = doc.modelspace()
    if "COUNTERTOPS" not in doc.layers:
        doc.layers.add("COUNTERTOPS", color=7)
    if "COUNTERTOP_NUMBER" not in doc.layers:
        doc.layers.add("COUNTERTOP_NUMBER", color=3)
    for record in records:
        entity = msp.entitydb[record.handle]
        entity.dxf.layer = "COUNTERTOPS"

    label_style = "Standard"
    unit_entities = [
        entity
        for entity in msp
        if entity.dxftype() == "TEXT"
        and str(entity.dxf.text).upper().startswith("UNIT TYPE")
    ]
    if unit_entities:
        label_style = str(unit_entities[0].dxf.get("style", "Standard"))

    generated: list[Countertop] = []
    all_labels: list[Any] = []
    label_boxes: list[tuple[float, float, float, float]] = []
    obstacle_items = [
        (handle, item_box)
        for handle, item_box in boxes.items()
        if item_box is not None
    ]
    for record in records:
        shape = Polygon(record.points)
        anchor = shape.representative_point()
        obstacles = [
            item_box
            for handle, item_box in obstacle_items
            if handle not in {record.handle, record.frame_handle}
            and boxes_intersect(item_box, record.bbox, clearance=150.0)
        ] + [
            item_box
            for item_box in label_boxes
            if boxes_intersect(item_box, record.bbox, clearance=50.0)
        ]
        accepted = None
        for label_height in (100.0, 90.0, 80.0, 70.0, 60.0, 50.0, 42.0, 36.0, 30.0):
            label = msp.add_text(
                record.number,
                height=label_height,
                rotation=0,
                dxfattribs={
                    "layer": "COUNTERTOP_NUMBER",
                    "color": 3,
                    "style": label_style,
                },
            )
            label.set_placement((0.0, 0.0), align=TextEntityAlignment.MIDDLE_CENTER)
            measured = entity_bbox(label)
            if measured is None:
                raise ValueError(f"cannot measure countertop label {record.number}")
            width = measured[2] - measured[0]
            height = measured[3] - measured[1]
            offset_x = (measured[0] + measured[2]) / 2.0
            offset_y = (measured[1] + measured[3]) / 2.0
            target = free_position(shape, anchor, obstacles, width, height)
            if target is None:
                msp.delete_entity(label)
                continue
            label.set_placement(
                (target[0] - offset_x, target[1] - offset_y),
                align=TextEntityAlignment.MIDDLE_CENTER,
            )
            accepted = (label, target, label_height)
            break
        if accepted is None:
            raise ValueError(f"no free interior space for countertop {record.number}")
        label, position, label_height = accepted
        all_labels.append(label)
        label_box = entity_bbox(label)
        if label_box is not None:
            label_boxes.append(label_box)
        generated.append(
            Countertop(
                **{
                    **record.__dict__,
                    "number_position": position,
                    "number_height": label_height,
                }
            )
        )
    return generated


def natural_key(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value)
    ]


def fractional_inches(mm: float, denominator: int = 8) -> str:
    total_units = round(float(mm) / 25.4 * denominator)
    whole, fraction = divmod(total_units, denominator)
    if fraction == 0:
        return f'{whole}"'
    divisor = denominator
    numerator = fraction
    while numerator % 2 == 0 and divisor % 2 == 0:
        numerator //= 2
        divisor //= 2
    if whole:
        return f'{whole} {numerator}/{divisor}"'
    return f'{numerator}/{divisor}"'


def size_text(record: Countertop) -> str:
    long_side = max(record.bbox[2] - record.bbox[0], record.bbox[3] - record.bbox[1])
    short_side = min(record.bbox[2] - record.bbox[0], record.bbox[3] - record.bbox[1])
    return f"{fractional_inches(long_side)} × {fractional_inches(short_side)}"


def write_material_xlsx(path: Path, records: list[Countertop]) -> Path:
    grouped: dict[tuple[str, str, str], list[Countertop]] = defaultdict(list)
    for record in records:
        grouped[(record.material, record.unit_type, size_text(record))].append(record)

    rows: list[list[Any]] = [
        ["材料", "户型", "台面板规格（英制，长×宽）", "数量（块）", "编号起", "编号止"]
    ]
    material_totals = Counter()
    for material in sorted({record.material for record in records}, key=natural_key):
        unit_types = sorted(
            {
                record.unit_type
                for record in records
                if record.material == material
            },
            key=natural_key,
        )
        for unit_type in unit_types:
            sizes = sorted(
                {
                    size_text(record)
                    for record in records
                    if record.material == material and record.unit_type == unit_type
                },
                key=natural_key,
            )
            for size in sizes:
                group = sorted(
                    grouped[(material, unit_type, size)],
                    key=lambda item: item.number,
                )
                rows.append(
                    [
                        material,
                        unit_type,
                        size,
                        len(group),
                        group[0].number,
                        group[-1].number,
                    ]
                )
                material_totals[material] += len(group)
        rows.append([f"{material} 小计", "", "", material_totals[material], "", ""])
    rows.append(["合计", "", "", sum(material_totals.values()), "", ""])
    write_xlsx(path, rows, sheet_name="台面板材料数量", header=True)
    return path


def validate_output(
    output_path: Path,
    records: list[Countertop],
    baseline_counts: Counter[str],
    baseline_texts: Counter[str],
) -> dict[str, Any]:
    output_doc = ezdxf.readfile(output_path)
    output_msp = output_doc.modelspace()
    output_counts = Counter(entity.dxftype() for entity in output_msp)
    red_frames = [
        entity
        for entity in output_msp
        if entity.dxftype() == "LWPOLYLINE"
        and bool(entity.closed)
        and effective_color(entity, output_doc) == 1
    ]
    if red_frames:
        raise ValueError(f"red frames remain in output: {len(red_frames)}")
    if output_counts["LWPOLYLINE"] != baseline_counts["LWPOLYLINE"] - 600:
        raise ValueError("countertop polyline count changed unexpectedly")
    if output_counts["DIMENSION"] != baseline_counts["DIMENSION"]:
        raise ValueError("dimension count changed")
    if output_counts["TEXT"] != baseline_counts["TEXT"] + len(records):
        raise ValueError("text count is not source + countertop numbers")

    output_labels = [
        entity
        for entity in output_msp
        if entity.dxftype() == "TEXT"
        and entity.dxf.get("layer") == "COUNTERTOP_NUMBER"
    ]
    labels = sorted(str(entity.dxf.text) for entity in output_labels)
    expected = sorted(record.number for record in records)
    if labels != expected or len(set(labels)) != len(labels):
        raise ValueError("countertop numbers do not match the manifest")

    original_texts = Counter(
        str(entity.dxf.text)
        for entity in output_msp
        if entity.dxftype() == "TEXT"
        and entity.dxf.get("layer") != "COUNTERTOP_NUMBER"
    )
    if original_texts != baseline_texts:
        raise ValueError("source annotation text multiset changed")
    return {
        "dxf_version": output_doc.dxfversion,
        "dwgcodepage": output_doc.header.get("$DWGCODEPAGE"),
        "lwpolyline_count": output_counts["LWPOLYLINE"],
        "dimension_count": output_counts["DIMENSION"],
        "source_text_count": sum(original_texts.values()),
        "number_text_count": len(output_labels),
        "red_frame_count": len(red_frames),
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
    baseline_counts = Counter(entity.dxftype() for entity in doc.modelspace())
    baseline_texts = Counter(
        str(entity.dxf.text)
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT"
    )
    frames, boxes = collect_frames(doc)
    countertops = collect_countertops(doc, frames, boxes)
    records = assign_numbers(frames, countertops)
    numbered = add_countertop_numbers(doc, records, boxes)

    for frame in frames:
        frame.entity.destroy()

    stem = source.stem
    output_dxf = output_dir / f"{stem}_台面板原位_编号.dxf"
    output_xlsx = output_dir / f"{stem}_台面板材料数量.xlsx"
    doc.header["$DWGCODEPAGE"] = "ANSI_936"
    doc.saveas(output_dxf, encoding="utf-8")
    write_material_xlsx(output_xlsx, numbered)
    validation = validate_output(
        output_dxf,
        numbered,
        baseline_counts,
        baseline_texts,
    )

    manifest = {
        "source": str(source),
        "source_sha256": source_hash,
        "output_dxf": str(output_dxf),
        "output_xlsx": str(output_xlsx),
        "red_frames_removed": len(frames),
        "countertop_count": len(numbered),
        "materials": dict(Counter(record.material for record in numbered)),
        "unit_types": dict(Counter(record.unit_type for record in numbered)),
        "number_height_counts": dict(Counter(record.number_height for record in numbered)),
        "validation": validation,
        "countertops": [
            {
                "handle": record.handle,
                "frame_handle": record.frame_handle,
                "number": record.number,
                "unit_type": record.unit_type,
                "material": record.material,
                "size_inches": size_text(record),
                "bbox": list(record.bbox),
                "number_position": list(record.number_position or []),
                "number_height": record.number_height,
            }
            for record in numbered
        ],
    }
    (output_dir / f"{stem}_台面板编号清单.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in manifest.items() if key != "countertops"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
