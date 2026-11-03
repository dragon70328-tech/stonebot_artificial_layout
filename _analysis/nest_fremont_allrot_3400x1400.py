"""Nest all Fremont ST-1/ST-2 countertops on 3400 x 1400 sheets."""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dxf_writer import write_nested_dxf
from src.deepnest_engine import nest_parts_deepnest
from src.nesting import nest_parts, validate_nesting
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf
from src.postprocess import estimate_kerf_material, validate_kerf_material
from shapely import affinity


SHEET_WIDTH = 3400.0
SHEET_HEIGHT = 1400.0
ROTATIONS = (0.0, 90.0, 180.0, 270.0)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dxf-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--xlsx-output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--pre-rotate-seed", type=int)
    parser.add_argument(
        "--engine",
        choices=("bridge", "deepnest"),
        default="bridge",
    )
    parser.add_argument("--beam-width", type=int, default=1000)
    parser.add_argument("--beam-budget", type=float, default=120.0)
    parser.add_argument("--improve-budget", type=float, default=60.0)
    parser.add_argument("--pattern-reuse", type=int, default=4)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--no-beam", action="store_true")
    args = parser.parse_args()

    fixed = load_module(
        ROOT / "_analysis" / "nest_fremont_st12_fixed_orientation.py",
        "fixednest",
    )
    helper = load_module(
        ROOT / "_analysis" / "build_manual_informed_145.py",
        "manual_helpers",
    )
    parts, records, source_summary = fixed.parse_parts(
        args.source,
        3000.0,
        2000.0,
        args.manifest,
    )
    for part in parts:
        part.allowed_rotations = ROTATIONS
    if args.pre_rotate_seed is not None:
        rng = random.Random(args.pre_rotate_seed)
        for part in parts:
            angle = rng.choice(ROTATIONS)
            if not angle:
                continue
            origin = part.polygon.centroid
            part.polygon = affinity.rotate(
                part.polygon,
                angle,
                origin=origin,
            )
            part.outer_polygon = affinity.rotate(
                part.outer_polygon,
                angle,
                origin=origin,
            )
            part.source_outer_polygon = part.outer_polygon

    total_area = sum(float(part.area) for part in parts)
    theoretical_min = math.ceil(
        total_area / (SHEET_WIDTH * SHEET_HEIGHT)
    )
    started = time.time()
    stats: dict = {}
    if args.engine == "deepnest":
        result = nest_parts_deepnest(
            parts,
            SHEET_WIDTH,
            SHEET_HEIGHT,
            20.0,
            unit="metric",
            configs=[
                ("short", "bottom_left", args.seed),
                ("area", "bottom_left", args.seed + 1),
                ("area", "skyline", args.seed + 2),
                ("long", "column", args.seed + 3),
                ("short", "contact", args.seed + 4),
                ("area", "contact", args.seed + 5),
            ],
            improve_budget=args.improve_budget,
            trials=args.trials,
            seed=args.seed,
            rotations=ROTATIONS,
            arbitrary_rotation=False,
            candidate_mode="nfp_lite",
            low_fill_rework=True,
            low_fill_threshold=0.75,
            stats=stats,
        )
    else:
        result = nest_parts(
            parts,
            SHEET_WIDTH,
            SHEET_HEIGHT,
            20.0,
            unit="metric",
            configs=[
                ("short", "skyline", args.seed),
                ("area", "skyline", args.seed + 1),
                ("area", "column", args.seed + 2),
                ("long", "skyline", args.seed + 3),
                ("short", "contact", args.seed + 4),
                ("area", "contact", args.seed + 5),
            ],
            improve_budget=args.improve_budget,
            trials=args.trials,
            seed=args.seed,
            rotations=ROTATIONS,
            min_sheets=theoretical_min,
            beam=not args.no_beam,
            beam_width=args.beam_width,
            beam_budget=args.beam_budget,
            pattern_reuse=args.pattern_reuse,
            compress=True,
            rect_fast=False,
            stats=stats,
        )
    validation_errors = validate_nesting(
        result,
        SHEET_WIDTH,
        SHEET_HEIGHT,
    )
    if validation_errors:
        raise ValueError("; ".join(validation_errors[:10]))
    if result.total_parts != 708:
        raise ValueError(
            f"expected 708 parts, got {result.total_parts}"
        )

    write_nested_dxf(
        result,
        args.dxf_output,
        unit_system="metric",
        board_size=(SHEET_WIDTH, SHEET_HEIGHT),
    )
    readback_errors = verify_nested_dxf(
        args.dxf_output,
        sheets_payload_from_result(result),
        gap=200.0,
    )
    kerf_errors = validate_kerf_material(
        result.sheets,
        5.0,
        allowance_x_mm=30.0,
        allowance_y_mm=30.0,
    )
    helper.write_material_quantity_xlsx(args.xlsx_output, records)

    rotations = collections.Counter(
        f"{float(part.placement_rot or 0.0) % 360.0:g}"
        for sheet in result.sheets
        for part in sheet.parts
    )
    payload = {
        "source_summary": source_summary,
        "search_seed": args.seed,
        "engine": args.engine,
        "pre_rotate_seed": args.pre_rotate_seed,
        "beam_width": args.beam_width,
        "beam_budget_seconds": args.beam_budget,
        "improve_budget_seconds": args.improve_budget,
        "optimizer_stats": stats,
        "sheet_nominal_mm": [SHEET_WIDTH, SHEET_HEIGHT],
        "sheet_oversize_total_mm": [30.0, 30.0],
        "kerf_mm": 5.0,
        "rotation_policy": [0.0, 90.0, 180.0, 270.0],
        "rotation_counts": dict(sorted(rotations.items())),
        "theoretical_min_sheets": theoretical_min,
        "total_parts": result.total_parts,
        "total_sheets": result.total_sheets,
        "yield_rate_percent": round(result.yield_rate, 3),
        "elapsed_seconds": round(time.time() - started, 3),
        "dxf_validation_errors": readback_errors,
        "kerf_validation_errors": kerf_errors,
        "kerf_metrics": {
            key: value
            for key, value in estimate_kerf_material(result.sheets, 5.0).items()
            if key != "per_sheet"
        },
        "output_dxf": str(args.dxf_output),
        "output_xlsx": str(args.xlsx_output),
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
            for sheet in result.sheets
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
                    "search_seed",
                    "theoretical_min_sheets",
                    "total_parts",
                    "total_sheets",
                    "yield_rate_percent",
                    "elapsed_seconds",
                    "rotation_counts",
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
