"""Rebind the manual layout to source IDs, repair frame overruns, and reduce it."""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import ezdxf
from shapely import affinity
from shapely.geometry import Point, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.nesting as nesting
from src.dxf_writer import write_nested_dxf
from src.models import NestingResult, Part, Sheet
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf
from src.nesting_reviewer import find_reductions
from src.postprocess import estimate_kerf_material, validate_kerf_material
from src.xlsx_writer import write_xlsx


def family(polygon: Polygon) -> tuple[int, int, int, float]:
    min_x, min_y, max_x, max_y = polygon.bounds
    width = max_x - min_x
    height = max_y - min_y
    return (
        round(polygon.area / 100.0) * 100,
        round(min(width, height) / 5.0) * 5,
        round(max(width, height) / 5.0) * 5,
        round(polygon.area / (width * height), 1),
    )


def shape_signature(polygon: Polygon) -> tuple[tuple[tuple[int, int], ...], ...]:
    """Return a translation/axis-rotation invariant exterior signature."""
    candidates = []
    for angle in (0.0, 90.0, 180.0, 270.0):
        transformed = affinity.rotate(polygon, angle, origin="center")
        min_x, min_y, _max_x, _max_y = transformed.bounds
        points = [
            (
                round((float(x) - min_x) * 10.0),
                round((float(y) - min_y) * 10.0),
            )
            for x, y in transformed.exterior.coords
        ]
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if not points:
            continue
        start = min(range(len(points)), key=lambda index: points[index])
        ring = tuple(points[start:] + points[:start])
        candidates.append(ring)
    return tuple(sorted(set(candidates)))


def natural_key(value: str) -> list[object]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", str(value))
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


def orientation_angle(manual: Polygon, source: Polygon) -> float | None:
    for angle in (0.0, 90.0, 180.0, 270.0):
        candidate = affinity.rotate(source, angle, origin="centroid")
        candidate = affinity.translate(
            candidate,
            xoff=manual.centroid.x - candidate.centroid.x,
            yoff=manual.centroid.y - candidate.centroid.y,
        )
        if manual.hausdorff_distance(candidate) <= 2.0:
            return angle
    return None


def write_material_quantity_xlsx(
    path: Path,
    records: list[dict],
) -> None:
    grouped: dict[tuple[str, float, float], list[str]] = collections.defaultdict(list)
    for record in records:
        bounds = record["source_bbox"]
        width = float(bounds[2]) - float(bounds[0])
        height = float(bounds[3]) - float(bounds[1])
        grouped[
            (
                str(record["material"]),
                round(max(width, height), 1),
                round(min(width, height), 1),
            )
        ].append(str(record["number"]))

    rows: list[list[object]] = [
        ["材料", "数量（块）", "长（mm）", "宽（mm）", "长（英制）", "宽（英制）", "编号清单"]
    ]
    for key in sorted(
        grouped,
        key=lambda item: (natural_key(item[0]), -item[1], -item[2]),
    ):
        material, length, width = key
        numbers = sorted(grouped[key], key=natural_key)
        rows.append(
            [
                material,
                len(numbers),
                length,
                width,
                fractional_inches(length),
                fractional_inches(width),
                "、".join(numbers),
            ]
        )
    rows.append(
        [
            "合计",
            sum(len(numbers) for numbers in grouped.values()),
            "",
            "",
            "",
            "",
            "",
        ]
    )
    write_xlsx(path, rows, sheet_name="材料数量", decimal_columns=(2, 3))


def load_fixed_module():
    path = ROOT / "_analysis" / "nest_fremont_st12_fixed_orientation.py"
    spec = importlib.util.spec_from_file_location("fixednest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def repair_outside_parts(sheet: Sheet) -> int:
    moved = 0
    board = Polygon(
        [(0, 0), (sheet.width, 0), (sheet.width, sheet.height), (0, sheet.height)]
    )
    for part in sheet.parts:
        if part.outer_polygon.within(board):
            continue
        others = [item.outer_polygon for item in sheet.parts if item.id != part.id]
        candidate = None
        for distance in (0.25, 0.5, 1.0, 2.0, 3.2, 3.5, 4.0):
            for dx, dy in (
                (distance, 0),
                (-distance, 0),
                (0, distance),
                (0, -distance),
                (distance, distance),
                (distance, -distance),
                (-distance, distance),
                (-distance, -distance),
            ):
                shifted = affinity.translate(
                    part.outer_polygon,
                    xoff=dx,
                    yoff=dy,
                )
                if not board.covers(shifted):
                    continue
                if any(
                    shifted.intersection(other).area > 1e-6
                    for other in others
                ):
                    continue
                candidate = (dx, dy)
                break
            if candidate is not None:
                break
        if candidate is None:
            raise ValueError(
                f"cannot repair out-of-frame part {part.number} "
                f"on sheet {sheet.index}"
            )
        dx, dy = candidate
        part.outer_polygon = affinity.translate(
            part.outer_polygon,
            xoff=dx,
            yoff=dy,
        )
        part.polygon = affinity.translate(part.polygon, xoff=dx, yoff=dy)
        part.label_position = (
            part.label_position[0] + dx,
            part.label_position[1] + dy,
        )
        moved += 1
    return moved


def normalize_negative_board_origin(sheet: Sheet) -> None:
    """Shift an entire learned board so no component has a negative coordinate."""
    min_x = min(
        (part.outer_polygon.bounds[0] for part in sheet.parts),
        default=0.0,
    )
    min_y = min(
        (part.outer_polygon.bounds[1] for part in sheet.parts),
        default=0.0,
    )
    dx = -min_x if min_x < 0 else 0.0
    dy = -min_y if min_y < 0 else 0.0
    if not dx and not dy:
        return
    for part in sheet.parts:
        part.outer_polygon = affinity.translate(
            part.outer_polygon,
            xoff=dx,
            yoff=dy,
        )
        part.polygon = affinity.translate(part.polygon, xoff=dx, yoff=dy)
        part.label_position = (
            part.label_position[0] + dx,
            part.label_position[1] + dy,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manual", type=Path, required=True)
    parser.add_argument("--dxf-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--xlsx-output", type=Path, required=True)
    parser.add_argument("--allow-oversize-margin", action="store_true")
    args = parser.parse_args()

    fixed = load_fixed_module()
    source_parts, records, summary = fixed.parse_parts(
        args.source,
        3000.0,
        2000.0,
        args.manifest,
    )
    record_by_number = {str(record["number"]): record for record in records}
    by_unit_shape: dict[tuple[str, tuple], list[str]] = collections.defaultdict(list)
    by_unit_family = collections.defaultdict(list)
    by_family = collections.defaultdict(list)
    for part in source_parts:
        key = family(part.outer_polygon)
        unit_type = str(record_by_number[part.number]["unit_type"])
        by_unit_shape[(unit_type, shape_signature(part.outer_polygon))].append(
            part.number
        )
        by_unit_family[
            (unit_type, key)
        ].append(part.number)
        by_family[key].append(part.number)

    manual = ezdxf.readfile(str(args.manual))
    msp = manual.modelspace()
    all_polylines = [
        entity
        for entity in msp
        if entity.dxftype() == "LWPOLYLINE" and bool(entity.closed)
    ]
    board_polygons = []
    manual_parts = []
    unit_texts = [
        entity for entity in msp if entity.dxftype() == "TEXT"
    ]
    for entity in all_polylines:
        polygon = Polygon(
            [(float(x), float(y)) for x, y in entity.get_points("xy")]
        )
        min_x, min_y, max_x, max_y = polygon.bounds
        if (
            abs((max_x - min_x) - 3000.0) <= 1.0
            and abs((max_y - min_y) - 2000.0) <= 1.0
        ):
            board_polygons.append(polygon)
        else:
            manual_parts.append((entity, polygon))
    if len(board_polygons) != 146 or len(manual_parts) != 708:
        raise ValueError(
            f"manual layout mismatch: {len(board_polygons)} boards, "
            f"{len(manual_parts)} parts"
        )

    unit_types = {
        str(record["unit_type"]) for record in records
    }
    assigned: dict[str, str] = {}
    used_numbers: set[str] = set()
    for entity, polygon in manual_parts:
        labels = {
            str(text.dxf.text)
            for text in unit_texts
            if polygon.covers(
                Point(float(text.dxf.insert.x), float(text.dxf.insert.y))
            )
        }
        unit = next((label for label in labels if label in unit_types), None)
        if unit is None:
            continue
        candidates = by_unit_shape[(unit, shape_signature(polygon))]
        match = next(
            (number for number in candidates if number not in used_numbers),
            None,
        )
        if match is None:
            candidates = by_unit_family[(unit, family(polygon))]
            match = next(
                (number for number in candidates if number not in used_numbers),
                None,
            )
        if match is not None:
            assigned[str(entity.dxf.handle)] = match
            used_numbers.add(match)
    for entity, polygon in manual_parts:
        handle = str(entity.dxf.handle)
        if handle in assigned:
            continue
        match = next(
            (
                number
                for number in by_family[family(polygon)]
                if number not in used_numbers
            ),
            None,
        )
        if match is None:
            raise ValueError(f"cannot assign a source number to manual part {handle}")
        assigned[handle] = match
        used_numbers.add(match)
    if len(assigned) != 708 or len(used_numbers) != 708:
        raise ValueError("manual-to-source part assignment is incomplete")

    manual_orientation: dict[str, float] = {}
    manual_relative_angle: dict[str, float] = {}
    needs_source_repack: set[int] = set()
    orientation_errors: list[str] = []
    for entity, polygon in manual_parts:
        handle = str(entity.dxf.handle)
        number = assigned[handle]
        record = record_by_number[number]
        relative_angle = orientation_angle(polygon, record["polygon"])
        if relative_angle is None:
            orientation_errors.append(
                f"{number}: manual footprint differs from the source outline"
            )
            continue
        manual_relative_angle[handle] = relative_angle
        manual_orientation[number] = (
            float(record["rotation_deg"]) + relative_angle
        ) % 360.0

    direction_errors: list[str] = []
    rotated_frozen_l = sorted(
        (
            str(record["number"])
            for record in records
            if record["is_l"]
            and record["frozen_by_l_pair"]
            and manual_orientation.get(str(record["number"])) in {90.0, 270.0}
        ),
        key=natural_key,
    )
    if len(rotated_frozen_l) > 33:
        direction_errors.append(
            f"{len(rotated_frozen_l)} frozen L parts use 90/270; limit is 33"
        )
    for record in records:
        number = str(record["number"])
        angle = manual_orientation.get(number)
        if angle is None or record["is_rectangle"]:
            continue
        if not record["is_l"]:
            continue
        if record["material"] == "ST/1" and not record["yellow"]:
            if record["frozen_by_l_pair"]:
                continue
            else:
                allowed = {90.0, 270.0}
        else:
            allowed = {0.0, 180.0}
        if angle not in allowed:
            direction_errors.append(
                f"{number}: {angle:g}° outside allowed "
                f"{sorted(allowed)}"
            )
    if direction_errors:
        raise ValueError(
            "rotation-policy validation failed: "
            + "; ".join(direction_errors[:20])
        )

    source_by_number = {part.number: part for part in source_parts}
    sheets = []
    for index, board in enumerate(
        sorted(board_polygons, key=lambda polygon: (
            -round(polygon.bounds[3], 1),
            round(polygon.bounds[0], 1),
        )),
        start=1,
    ):
        board_min_x, board_min_y, _, _ = board.bounds
        sheet = Sheet(
            index=index,
            width=3000.0,
            height=2000.0,
            thickness=20.0,
        )
        for entity, polygon in manual_parts:
            if not board.covers(polygon.centroid):
                continue
            local_footprint = affinity.translate(
                polygon,
                xoff=-board_min_x,
                yoff=-board_min_y,
            )
            handle = str(entity.dxf.handle)
            number = assigned[handle]
            record = record_by_number[number]
            source_polygon = record["polygon"]
            relative_angle = manual_relative_angle.get(handle)
            if relative_angle is None:
                needs_source_repack.add(index)
                actual = affinity.translate(
                    source_polygon,
                    xoff=local_footprint.centroid.x - source_polygon.centroid.x,
                    yoff=local_footprint.centroid.y - source_polygon.centroid.y,
                )
            else:
                actual = affinity.rotate(
                    source_polygon,
                    relative_angle,
                    origin="centroid",
                )
                actual = affinity.translate(
                    actual,
                    xoff=local_footprint.centroid.x - actual.centroid.x,
                    yoff=local_footprint.centroid.y - actual.centroid.y,
                )
            sheet.parts.append(
                Part(
                    id=int(handle, 16),
                    number=number,
                    polygon=actual,
                    outer_polygon=actual,
                    material_group="ST-1+ST-2",
                    material_column="ST-1+ST-2",
                    area=actual.area,
                    label_position=(
                        actual.representative_point().x,
                        actual.representative_point().y,
                    ),
                    source_outer_polygon=actual,
                    placement_rot=0.0,
                    allowed_rotations=(
                        (0.0, 90.0, 180.0, 270.0)
                        if record["is_rectangle"]
                        else ((0.0, 180.0) if record["is_l"] else (0.0,))
                    ),
                )
            )
        sheets.append(sheet)

    if needs_source_repack:
        repack_sheets = [
            sheet for sheet in sheets if sheet.index in needs_source_repack
        ]
        group_parts = []
        for sheet in repack_sheets:
            for number in [part.number for part in sheet.parts]:
                source_part = source_by_number[number]
                record = record_by_number[number]
                source_part.allowed_rotations = (
                    (0.0, 90.0, 180.0, 270.0)
                    if record["is_rectangle"] or record["is_l"]
                    else (0.0,)
                )
                group_parts.append(source_part)
        repacked = nesting.nest_parts(
            group_parts,
            3000.0,
            2000.0,
            20.0,
            unit="metric",
            configs=[
                ("short", "skyline", 1),
                ("area", "skyline", 2),
                ("area", "column", 3),
                ("long", "skyline", 4),
                ("short", "contact", 5),
                ("area", "contact", 6),
            ],
            improve_budget=20.0,
            trials=3,
            seed=7,
            rotations=(0.0, 90.0, 180.0, 270.0),
            min_sheets=len(repack_sheets),
            beam=True,
            beam_width=3000,
            beam_budget=30.0,
            rect_fast=False,
        )
        if repacked.total_sheets > len(repack_sheets) + 1:
            raise ValueError(
                f"source-outline repack needs {repacked.total_sheets} sheets "
                f"instead of at most {len(repack_sheets) + 1}"
            )
        sheets = [
            sheet for sheet in sheets if sheet.index not in needs_source_repack
        ] + list(repacked.sheets)
        for index, sheet in enumerate(sheets, start=1):
            sheet.index = index

    if args.allow_oversize_margin:
        repaired = 0
        for sheet in sheets:
            normalize_negative_board_origin(sheet)
    else:
        repaired = sum(repair_outside_parts(sheet) for sheet in sheets)
    initial = NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(len(sheet.parts) for sheet in sheets),
        total_sheets=len(sheets),
        total_part_area=sum(sheet.used_area for sheet in sheets),
        total_sheet_area=sum(sheet.total_area for sheet in sheets),
    )
    validation_canvas_w = 3030.0 if args.allow_oversize_margin else 3000.0
    validation_canvas_h = 2030.0 if args.allow_oversize_margin else 2000.0
    validation = nesting.validate_nesting(
        initial,
        validation_canvas_w,
        validation_canvas_h,
    )
    if validation:
        for sheet in initial.sheets:
            outside = [
                (part.number, tuple(round(value, 3) for value in part.outer_polygon.bounds))
                for part in sheet.parts
                if (
                    part.outer_polygon.bounds[0] < -0.01
                    or part.outer_polygon.bounds[1] < -0.01
                    or part.outer_polygon.bounds[2] > validation_canvas_w + 0.01
                    or part.outer_polygon.bounds[3] > validation_canvas_h + 0.01
                )
            ]
            if outside:
                print(
                    f"DEBUG sheet {sheet.index} outside={outside}",
                    file=sys.stderr,
                )
        raise ValueError("manual layout invalid: " + "; ".join(validation[:10]))

    profile = SimpleNamespace(
        rotation=[0],
        arbitrary_rotation=False,
        group_mode=None,
    )
    started = time.time()
    moves, reduced = find_reductions(
        initial,
        profile=profile,
        max_reductions=10,
        max_donor_parts=8,
        max_donor_fill_ratio=0.9,
        max_donor_attempts=300,
        time_budget_s=480.0,
        per_donor_time_s=35.0,
        max_nodes_per_donor=200000,
        placement_limit=80,
    )
    final_errors = nesting.validate_nesting(
        reduced,
        validation_canvas_w,
        validation_canvas_h,
    )
    if final_errors:
        raise ValueError("reduced manual layout invalid: " + "; ".join(final_errors))

    final_orientation: dict[str, float] = {}
    final_geometry_errors: list[str] = []
    for sheet in reduced.sheets:
        for part in sheet.parts:
            record = record_by_number[part.number]
            relative_angle = orientation_angle(part.outer_polygon, record["polygon"])
            if relative_angle is None:
                final_geometry_errors.append(
                    f"sheet {sheet.index} {part.number}: source outline mismatch"
                )
                continue
            final_orientation[part.number] = (
                float(record["rotation_deg"]) + relative_angle
            ) % 360.0
    if final_geometry_errors:
        raise ValueError(
            "final source-outline validation failed: "
            + "; ".join(final_geometry_errors[:20])
        )
    final_rotated_frozen_l = sorted(
        (
            str(record["number"])
            for record in records
            if record["is_l"]
            and record["frozen_by_l_pair"]
            and final_orientation.get(str(record["number"])) in {90.0, 270.0}
        ),
        key=natural_key,
    )
    if len(final_rotated_frozen_l) > 33:
        raise ValueError(
            f"{len(final_rotated_frozen_l)} frozen L parts use 90/270; "
            "limit is 33"
        )

    oversize_parts = []
    for sheet in reduced.sheets:
        for part in sheet.parts:
            min_x, min_y, max_x, max_y = part.outer_polygon.bounds
            over_x = max(0.0, max_x - 3000.0)
            over_y = max(0.0, max_y - 2000.0)
            if over_x > 1e-6 or over_y > 1e-6:
                oversize_parts.append(
                    {
                        "sheet": sheet.index,
                        "number": part.number,
                        "over_x_mm": round(over_x, 3),
                        "over_y_mm": round(over_y, 3),
                    }
                )
    if any(
        item["over_x_mm"] > 30.0 + 1e-6 or item["over_y_mm"] > 30.0 + 1e-6
        for item in oversize_parts
    ):
        raise ValueError("a part exceeds the 30 mm physical oversize allowance")

    write_nested_dxf(
        reduced,
        args.dxf_output,
        unit_system="metric",
        board_size=(3000.0, 2000.0),
    )
    readback_errors = verify_nested_dxf(
        args.dxf_output,
        sheets_payload_from_result(reduced),
        gap=200.0,
    )
    kerf_errors = validate_kerf_material(
        reduced.sheets,
        5.0,
        allowance_x_mm=30.0,
        allowance_y_mm=30.0,
    )
    write_material_quantity_xlsx(args.xlsx_output, records)
    orientation_counts = collections.Counter(
        f"{angle:g}" for angle in final_orientation.values()
    )
    payload = {
        "source_summary": summary,
        "source_manual_sheets": 146,
        "repaired_outside_parts": repaired,
        "validation_canvas_mm": [validation_canvas_w, validation_canvas_h],
        "nominal_board_mm": [3000.0, 2000.0],
        "removed_sheets": [
            {
                "removed_sheet": item.removed_sheet_index,
                "moved_parts": item.moved_parts,
                "target_sheets": item.target_sheets,
            }
            for item in moves
        ],
        "total_parts": reduced.total_parts,
        "total_sheets": reduced.total_sheets,
        "yield_rate_percent": round(reduced.yield_rate, 3),
        "elapsed_seconds": round(time.time() - started, 3),
        "rotation_policy": "manual layout orientations approved for this run",
        "manual_orientation_counts": dict(sorted(orientation_counts.items())),
        "source_outline_repack_sheets": sorted(needs_source_repack),
        "source_outline_repack_notes": orientation_errors,
        "rotated_frozen_l_count": len(rotated_frozen_l),
        "rotated_frozen_l_numbers": rotated_frozen_l,
        "final_rotated_frozen_l_count": len(final_rotated_frozen_l),
        "final_rotated_frozen_l_numbers": final_rotated_frozen_l,
        "oversize_parts": oversize_parts,
        "max_oversize_x_mm": max(
            (item["over_x_mm"] for item in oversize_parts),
            default=0.0,
        ),
        "max_oversize_y_mm": max(
            (item["over_y_mm"] for item in oversize_parts),
            default=0.0,
        ),
        "output_dxf": str(args.dxf_output),
        "output_xlsx": str(args.xlsx_output),
        "dxf_validation_errors": readback_errors,
        "kerf_validation_errors": kerf_errors,
        "kerf_metrics": {
            key: value
            for key, value in estimate_kerf_material(reduced.sheets, 5.0).items()
            if key != "per_sheet"
        },
        "sheets": [
            {
                "index": sheet.index,
                "parts": len(sheet.parts),
                "fill_percent": round(
                    sheet.used_area / sheet.total_area * 100.0,
                    3,
                ),
                "part_numbers": [part.number for part in sheet.parts],
            }
            for sheet in reduced.sheets
        ],
    }
    args.json_output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                key: payload[key]
                for key in (
                    "source_manual_sheets",
                    "repaired_outside_parts",
                    "removed_sheets",
                    "total_parts",
                    "total_sheets",
                    "yield_rate_percent",
                    "manual_orientation_counts",
                    "max_oversize_x_mm",
                    "max_oversize_y_mm",
                    "dxf_validation_errors",
                    "kerf_validation_errors",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
