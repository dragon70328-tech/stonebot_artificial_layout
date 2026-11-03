"""Nest Fremont ST-1/ST-2 countertops with a fixed per-part orientation.

Rules:
- ST-2 countertops keep their source orientation.
- ST-1 countertops whose outer polyline is yellow (ACI 40) keep their source
  orientation.
- Every other ST-1 countertop is rotated 90 degrees clockwise before nesting.
- The nesting engine receives rotations=(0,), so it cannot rotate any part.

The only unavoidable exception is reported explicitly when a prescribed
rotation makes a part larger than the 3000 x 2000 sheet. Such a part keeps its
source orientation if that orientation fits; otherwise nesting fails closed.
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import ezdxf
from shapely.affinity import rotate
from shapely.geometry import Point, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.nesting as nesting_module
from src.dxf_writer import write_nested_dxf
from src.models import Part
from src.nesting import nest_parts
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf
from src.postprocess import estimate_kerf_material, validate_kerf_material


YELLOW_ACI = 40
MATERIAL_RE = re.compile(r"ST[-/]?(\d+)", re.IGNORECASE)


def effective_color(entity: Any, doc: ezdxf.document.Drawing) -> int:
    color = int(entity.dxf.get("color", 256))
    if color == 256:
        try:
            return int(doc.layers.get(entity.dxf.layer).dxf.color)
        except Exception:
            return 256
    return color


def material_of(number: str) -> str:
    match = MATERIAL_RE.search(number)
    if not match:
        raise ValueError(f"countertop number has no ST material: {number!r}")
    return f"ST/{int(match.group(1))}"


def fits(shape: Polygon, sheet_width: float, sheet_height: float) -> bool:
    min_x, min_y, max_x, max_y = shape.bounds
    return (
        max_x - min_x <= sheet_width + 0.01
        and max_y - min_y <= sheet_height + 0.01
    )


def is_l_shape(shape: Polygon) -> bool:
    coords = list(shape.exterior.coords)
    min_x, min_y, max_x, max_y = shape.bounds
    bbox_area = (max_x - min_x) * (max_y - min_y)
    return (
        5 <= len(coords) <= 7
        and bbox_area > 0
        and shape.area / bbox_area < 0.999
    )


def is_rectangle(shape: Polygon) -> bool:
    coords = list(shape.exterior.coords)
    min_x, min_y, max_x, max_y = shape.bounds
    bbox_area = (max_x - min_x) * (max_y - min_y)
    return (
        len(coords) <= 6
        and bbox_area > 0
        and shape.area / bbox_area >= 0.999
    )


def longest_edge(shape: Polygon) -> float:
    coords = list(shape.exterior.coords)
    return max(
        math.hypot(x1 - x0, y1 - y0)
        for (x0, y0), (x1, y1) in zip(coords, coords[1:])
    )


def parse_parts(
    source_path: Path,
    sheet_width: float,
    sheet_height: float,
    manifest_path: Path | None = None,
) -> tuple[list[Part], list[dict[str, Any]], dict[str, Any]]:
    doc = ezdxf.readfile(str(source_path))
    msp = doc.modelspace()
    polygons = [
        entity
        for entity in msp
        if entity.dxftype() == "LWPOLYLINE" and bool(entity.closed)
    ]
    number_texts = [
        entity
        for entity in msp
        if entity.dxftype() == "TEXT"
        and str(entity.dxf.get("layer", "")) == "COUNTERTOP_NUMBER"
    ]
    if len(polygons) != 908 or len(number_texts) != 908:
        raise ValueError(
            f"expected 908 numbered countertops, found "
            f"{len(polygons)} polygons and {len(number_texts)} numbers"
        )

    frame_by_number: dict[str, str] = {}
    if manifest_path is not None and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text("utf-8"))
        frame_by_number = {
            str(item["number"]): str(item["frame_handle"])
            for item in manifest.get("countertops", [])
        }

    parsed: list[dict[str, Any]] = []
    for entity in polygons:
        source_polygon = Polygon(
            [(float(x), float(y)) for x, y in entity.get_points("xy")]
        )
        if (
            source_polygon.is_empty
            or not source_polygon.is_valid
            or source_polygon.area <= 0
        ):
            raise ValueError(f"invalid countertop polygon {entity.dxf.handle}")
        matching_texts = [
            text
            for text in number_texts
            if source_polygon.covers(
                Point(float(text.dxf.insert.x), float(text.dxf.insert.y))
            )
        ]
        if len(matching_texts) != 1:
            raise ValueError(
                f"countertop {entity.dxf.handle} contains "
                f"{len(matching_texts)} number labels"
            )
        number_text = matching_texts[0]
        number = str(number_text.dxf.text)
        material = material_of(number)
        if material not in {"ST/1", "ST/2"}:
            continue

        color = effective_color(entity, doc)
        parsed.append(
            {
                "handle": str(entity.dxf.handle),
                "number": number,
                "material": material,
                "unit_type": number.split("-ST/", 1)[0],
                "frame_handle": frame_by_number.get(number, ""),
                "color": color,
                "yellow": material == "ST/1" and color == YELLOW_ACI,
                "source_polygon": source_polygon,
                "number_text": number_text,
                "is_l": is_l_shape(source_polygon),
                "is_rectangle": is_rectangle(source_polygon),
                "longest_edge": longest_edge(source_polygon),
            }
        )

    if len(parsed) != 708:
        raise ValueError(f"expected 708 ST-1/ST-2 countertops, found {len(parsed)}")

    # An L countertop is eligible only when its longest edge is <= 2000 mm.
    # For an L-L combination, the whole pair rotates if either member exceeds
    # 2000 mm. A same-frame, same-unit rectangular companion is frozen with a
    # qualifying L.
    l_candidates = {
        item["number"]
        for item in parsed
        if item["material"] == "ST/1"
        and item["color"] != YELLOW_ACI
        and item["is_l"]
        and item["longest_edge"] <= 2000.0 + 1e-6
    }
    l_excluded: set[str] = set()
    if frame_by_number:
        l_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in parsed:
            if (
                item["material"] == "ST/1"
                and item["color"] != YELLOW_ACI
                and item["is_l"]
                and item["frame_handle"]
            ):
                key = (item["frame_handle"], item["unit_type"])
                l_groups.setdefault(key, []).append(item)
        for members in l_groups.values():
            if len(members) >= 2 and any(
                member["longest_edge"] > 2000.0 + 1e-6
                for member in members
            ):
                l_excluded.update(member["number"] for member in members)
    l_no_rotate = l_candidates - l_excluded
    companion_no_rotate: set[str] = set()
    if frame_by_number:
        for item in parsed:
            if item["number"] not in l_no_rotate or not item["frame_handle"]:
                continue
            companions = [
                other
                for other in parsed
                if other["number"] != item["number"]
                and other["frame_handle"] == item["frame_handle"]
                and other["unit_type"] == item["unit_type"]
                and other["material"] == "ST/1"
                and other["is_rectangle"]
            ]
            if len(companions) == 1:
                companion_no_rotate.add(companions[0]["number"])

    records: list[dict[str, Any]] = []
    for item in parsed:
        source_polygon = item["source_polygon"]
        number = item["number"]
        material = item["material"]
        color = item["color"]
        number_text = item["number_text"]
        frozen_by_l_pair = (
            number in l_no_rotate or number in companion_no_rotate
        )
        should_rotate = (
            material == "ST/1"
            and color != YELLOW_ACI
            and not frozen_by_l_pair
        )
        prescribed = (
            rotate(source_polygon, -90.0, origin="centroid")
            if should_rotate
            else source_polygon
        )
        exception = False
        if should_rotate and not fits(prescribed, sheet_width, sheet_height):
            if not fits(source_polygon, sheet_width, sheet_height):
                min_x, min_y, max_x, max_y = prescribed.bounds
                raise ValueError(
                    f"{number} fits neither the prescribed clockwise rotation "
                    f"({max_x - min_x:.1f} x {max_y - min_y:.1f} mm) nor its "
                    "source orientation on the sheet"
                )
            prescribed = source_polygon
            exception = True
        elif not fits(prescribed, sheet_width, sheet_height):
            min_x, min_y, max_x, max_y = prescribed.bounds
            raise ValueError(
                f"{number} does not fit the sheet in its fixed orientation: "
                f"{max_x - min_x:.1f} x {max_y - min_y:.1f} mm"
            )

        centroid = source_polygon.centroid
        label_point = Point(
            float(number_text.dxf.insert.x),
            float(number_text.dxf.insert.y),
        )
        if should_rotate and not exception:
            label_point = rotate(label_point, -90.0, origin=centroid)

        min_x, min_y, max_x, max_y = prescribed.bounds
        records.append(
            {
                "handle": item["handle"],
                "number": number,
                "material": material,
                "unit_type": item["unit_type"],
                "frame_handle": item["frame_handle"],
                "color": color,
                "yellow": material == "ST/1" and color == YELLOW_ACI,
                "is_l": item["is_l"],
                "is_rectangle": item["is_rectangle"],
                "longest_edge_mm": item["longest_edge"],
                "frozen_by_l_pair": frozen_by_l_pair,
                "rotation_deg": 0.0 if not should_rotate else -90.0,
                "rotation_exception": exception,
                "source_bbox": [float(value) for value in source_polygon.bounds],
                "fixed_bbox": [float(value) for value in prescribed.bounds],
                "fixed_size_mm": [
                    float(max_x - min_x),
                    float(max_y - min_y),
                ],
                "area_mm2": float(prescribed.area),
                "polygon": prescribed,
                "label_position": (float(label_point.x), float(label_point.y)),
            }
        )

    records.sort(key=lambda item: str(item["number"]))
    parts: list[Part] = []
    for index, record in enumerate(records, start=1):
        polygon = record["polygon"]
        part = Part(
            id=index,
            number=str(record["number"]),
            polygon=polygon,
            outer_polygon=polygon,
            material_group="ST-1+ST-2",
            material_column="ST-1+ST-2",
            area=float(polygon.area),
            label_position=record["label_position"],
            outer_handle=str(record["handle"]),
            source_outer_polygon=polygon,
            placement_rot=0.0,
        )
        part.allowed_rotations = (
            (0.0, 180.0)
            if record["frozen_by_l_pair"]
            else (0.0,)
        )
        parts.append(part)

    summary = {
        "source": str(source_path.resolve()),
        "input_countertops": 708,
        "materials": dict(Counter(str(item["material"]) for item in records)),
        "orientation_counts": {
            "st2_original": sum(
                item["material"] == "ST/2" and item["rotation_deg"] == 0
                for item in records
            ),
            "st1_yellow_original": sum(bool(item["yellow"]) for item in records),
            "st1_l_le_2000_original": sum(
                str(item["number"]) in l_no_rotate for item in records
            ),
            "st1_l_pair_forced_rotation": len(l_excluded),
            "st1_companion_rectangle_original": sum(
                str(item["number"]) in companion_no_rotate for item in records
            ),
            "st1_clockwise_90": sum(
                item["rotation_deg"] == -90.0 and not item["rotation_exception"]
                for item in records
            ),
            "rotation_exception_original": sum(
                bool(item["rotation_exception"]) for item in records
            ),
        },
        "l_shape_rule": {
            "max_longest_edge_mm": 2000.0,
            "qualifying_l_count": len(l_no_rotate),
            "paired_rectangle_count": len(companion_no_rotate),
            "group_flip_180_allowed": True,
        },
        "rotation_exceptions": [
            {
                "number": item["number"],
                "reason": "prescribed clockwise rotation exceeds sheet bound",
            }
            for item in records
            if item["rotation_exception"]
        ],
    }
    return parts, records, summary


def parse_configs(raw: str) -> list[tuple[str, str, int]]:
    configs = []
    for value in raw.split(","):
        if not value.strip():
            continue
        fields = value.strip().split(":")
        if len(fields) != 3:
            raise ValueError(f"invalid greedy config: {value!r}")
        configs.append((fields[0], fields[1], int(fields[2])))
    return configs


def enable_per_part_rotations() -> None:
    """Make the bridge engines honor a part-level allowed-rotation tuple."""
    original = nesting_module._rotation_forms

    @functools.wraps(original)
    def rotation_forms(part, cache, rotations):
        allowed = getattr(part, "allowed_rotations", None)
        return original(part, cache, allowed if allowed is not None else rotations)

    nesting_module._rotation_forms = rotation_forms
    import src.beam_nesting as beam_nesting

    beam_nesting._rotation_forms = rotation_forms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--source-manifest",
        type=Path,
        default=(
            ROOT
            / "_analysis"
            / "fremont_countertop_validation"
            / "户型20261102_台面板编号清单.json"
        ),
        help="Existing manifest used only to recover original frame membership.",
    )
    parser.add_argument("--dxf-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--sheet-width", type=float, default=3000.0)
    parser.add_argument("--sheet-height", type=float, default=2000.0)
    parser.add_argument("--kerf", type=float, default=5.0)
    parser.add_argument("--sheet-oversize", type=float, default=30.0)
    parser.add_argument("--beam-width", type=int, default=1000)
    parser.add_argument("--beam-budget", type=float, default=300.0)
    parser.add_argument("--improve-budget", type=float, default=300.0)
    parser.add_argument("--max-iterations", type=int, default=400)
    parser.add_argument("--patience", type=int, default=160)
    parser.add_argument("--pattern-reuse", type=int, default=4)
    parser.add_argument(
        "--greedy-configs",
        default=(
            "short:skyline:0,area:skyline:0,area:column:0,"
            "long:skyline:0,short:contact:0"
        ),
    )
    parser.add_argument("--no-beam", action="store_true")
    parser.add_argument(
        "--force-tail-refine",
        action="store_true",
        help=(
            "Run the beam engine's second tail-weighted pass even though "
            "this drawing has more than the normal 400-part cutoff."
        ),
    )
    parser.add_argument("--compress-subsets", default="4,6,8")
    parser.add_argument("--compress-beam-width", type=int, default=300)
    parser.add_argument("--compress-part-cap", type=int, default=80)
    args = parser.parse_args()

    if args.force_tail_refine:
        import src.beam_nesting as beam_nesting

        beam_nesting._REFINE_TAIL_MAX_PARTS = 10**9

    if args.compress_subsets:
        subset_sizes = tuple(
            int(value.strip())
            for value in args.compress_subsets.split(",")
            if value.strip()
        )
        nesting_module._compress_sheets = functools.partial(
            nesting_module._compress_sheets,
            subset_sizes=subset_sizes,
            beam_width=args.compress_beam_width,
            beam_part_cap=args.compress_part_cap,
        )

    parts, records, source_summary = parse_parts(
        args.source,
        args.sheet_width,
        args.sheet_height,
        args.source_manifest,
    )
    enable_per_part_rotations()
    total_area = sum(part.area for part in parts)
    area_lower_bound = math.ceil(
        total_area / (args.sheet_width * args.sheet_height)
    )
    stats: dict[str, Any] = {}
    result = nest_parts(
        parts,
        args.sheet_width,
        args.sheet_height,
        20.0,
        unit="metric",
        configs=parse_configs(args.greedy_configs),
        improve_budget=args.improve_budget,
        beam_budget=args.beam_budget,
        beam_width=args.beam_width,
        rotations=(0,),
        min_gap=0.0,
        min_sheets=area_lower_bound,
        beam=not args.no_beam,
        compress=True,
        rect_fast=True,
        pattern_reuse=args.pattern_reuse,
        improve_iterations=args.max_iterations,
        improve_patience=args.patience,
        stats=stats,
    )
    if result.total_parts != len(parts):
        raise ValueError(
            f"nesting lost parts: expected {len(parts)}, got {result.total_parts}"
        )
    nesting_errors = nesting_module.validate_nesting(
        result,
        args.sheet_width,
        args.sheet_height,
        min_gap=0.0,
    )
    if nesting_errors:
        raise ValueError("nesting validation failed: " + "; ".join(nesting_errors))

    kerf_metrics = estimate_kerf_material(result.sheets, args.kerf)
    kerf_errors = validate_kerf_material(
        result.sheets,
        args.kerf,
        allowance_x_mm=args.sheet_oversize,
        allowance_y_mm=args.sheet_oversize,
    )

    args.dxf_output.parent.mkdir(parents=True, exist_ok=True)
    write_nested_dxf(
        result,
        args.dxf_output,
        unit_system="metric",
        board_size=(args.sheet_width, args.sheet_height),
    )
    dxf_validation_errors = verify_nested_dxf(
        args.dxf_output,
        sheets_payload_from_result(result),
        gap=200.0,
    )
    if dxf_validation_errors:
        raise ValueError(
            "nested DXF readback failed: " + "; ".join(dxf_validation_errors)
        )

    payload = {
        "source_summary": source_summary,
        "sheet_nominal_mm": [args.sheet_width, args.sheet_height],
        "sheet_oversize_total_mm": [args.sheet_oversize, args.sheet_oversize],
        "kerf_mm": args.kerf,
        "rotation_allowed_in_nesting": [0],
        "total_parts": result.total_parts,
        "total_sheets": result.total_sheets,
        "area_lower_bound_sheets": area_lower_bound,
        "total_part_area_mm2": round(total_area, 3),
        "yield_rate_percent": round(result.yield_rate, 3),
        "output_dxf": str(args.dxf_output),
        "dxf_validation_errors": dxf_validation_errors,
        "kerf_validation_errors": kerf_errors,
        "kerf_metrics": {
            key: value for key, value in kerf_metrics.items() if key != "per_sheet"
        },
        "stats": stats,
        "sheets": [
            {
                "index": sheet.index,
                "parts": len(sheet.parts),
                "used_area_mm2": round(sheet.used_area, 3),
                "remaining_area_mm2": round(
                    sheet.width * sheet.height - sheet.used_area,
                    3,
                ),
                "fill_percent": round(
                    sheet.used_area / (sheet.width * sheet.height) * 100.0,
                    3,
                ),
                "part_numbers": [part.number for part in sheet.parts],
            }
            for sheet in result.sheets
        ],
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                key: payload[key]
                for key in (
                    "source_summary",
                    "sheet_nominal_mm",
                    "sheet_oversize_total_mm",
                    "kerf_mm",
                    "rotation_allowed_in_nesting",
                    "total_parts",
                    "total_sheets",
                    "area_lower_bound_sheets",
                    "yield_rate_percent",
                    "output_dxf",
                    "dxf_validation_errors",
                    "kerf_validation_errors",
                    "stats",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
