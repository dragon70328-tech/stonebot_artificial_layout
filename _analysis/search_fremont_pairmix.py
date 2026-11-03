"""Run one deterministic mixed 0/180-degree Fremont nesting search."""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.beam_nesting as beam_nesting
import src.nesting as nesting
from src.dxf_writer import write_nested_dxf
from src.nesting import nest_parts
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf
from src.postprocess import estimate_kerf_material, validate_kerf_material


def load_fixed_module():
    path = ROOT / "_analysis" / "nest_fremont_st12_fixed_orientation.py"
    spec = importlib.util.spec_from_file_location("fixednest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--relaxed-l-count", type=int, default=0)
    args = parser.parse_args()

    fixed = load_fixed_module()
    parts, records, summary = fixed.parse_parts(
        args.source,
        3000.0,
        2000.0,
        args.manifest,
    )
    if args.relaxed_l_count < 0:
        raise ValueError("relaxed-l-count must be non-negative")
    flexible_l = sorted(
        (
            record["number"]
            for record in records
            if record["frozen_by_l_pair"]
            and record["material"] == "ST/1"
            and record["is_l"]
        ),
        key=str,
    )
    if args.relaxed_l_count > len(flexible_l):
        raise ValueError("relaxed-l-count exceeds available L-pair parts")
    relaxed_l = set(flexible_l[:args.relaxed_l_count])
    for part in parts:
        if part.number in relaxed_l:
            part.allowed_rotations = (0.0, 90.0, 180.0, 270.0)

    rng = random.Random(args.seed)
    group_order: dict[str, tuple[float, float]] = {}
    for record in records:
        if record["frozen_by_l_pair"] and record["material"] == "ST/1":
            key = str(record["frame_handle"])
            if key not in group_order:
                group_order[key] = (
                    (0.0, 180.0) if rng.random() < 0.5 else (180.0, 0.0)
                )
    for part, record in zip(parts, records):
        if (
            part.number not in relaxed_l
            and str(record["frame_handle"]) in group_order
        ):
            part.allowed_rotations = group_order[str(record["frame_handle"])]

    original_forms = nesting._rotation_forms

    def rotation_forms(part, cache, rotations):
        return original_forms(
            part,
            cache,
            getattr(part, "allowed_rotations", rotations),
        )

    nesting._rotation_forms = rotation_forms
    beam_nesting._rotation_forms = rotation_forms

    stats: dict = {}
    started = time.time()
    result = nest_parts(
        parts,
        3000.0,
        2000.0,
        20.0,
        unit="metric",
        configs=[
            ("short", "skyline", args.seed),
            ("area", "skyline", args.seed),
            ("area", "column", args.seed),
            ("long", "skyline", args.seed),
            ("short", "contact", args.seed),
        ],
        improve_budget=180.0,
        beam_budget=300.0,
        beam_width=1000,
        rotations=(0.0, 180.0),
        min_gap=0.0,
        min_sheets=119,
        beam=True,
        compress=True,
        rect_fast=True,
        pattern_reuse=4,
        improve_iterations=300,
        improve_patience=120,
        seed=args.seed,
        stats=stats,
    )

    dxf = Path(f"{args.output_prefix}_{result.total_sheets}.dxf")
    data = dxf.with_suffix(".json")
    write_nested_dxf(
        result,
        dxf,
        unit_system="metric",
        board_size=(3000.0, 2000.0),
    )
    dxf_errors = verify_nested_dxf(
        dxf,
        sheets_payload_from_result(result),
        gap=200.0,
    )
    kerf_errors = validate_kerf_material(
        result.sheets,
        5.0,
        allowance_x_mm=30.0,
        allowance_y_mm=30.0,
    )
    payload = {
        "search_seed": args.seed,
        "source_summary": summary,
        "total_parts": result.total_parts,
        "total_sheets": result.total_sheets,
        "yield_rate_percent": round(result.yield_rate, 3),
        "rotation_allowed": {
            "normal": [0],
            "l_pair": [0, 180],
            "relaxed_l_pair_count": len(relaxed_l),
            "relaxed_l_pair_angles": [0, 90, 180, 270],
        },
        "stats": stats,
        "elapsed_seconds": round(time.time() - started, 3),
        "output_dxf": str(dxf),
        "dxf_validation_errors": dxf_errors,
        "kerf_validation_errors": kerf_errors,
        "kerf_metrics": {
            key: value
            for key, value in estimate_kerf_material(result.sheets, 5.0).items()
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
            for sheet in result.sheets
        ],
    }
    data.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "seed": args.seed,
                "total_sheets": result.total_sheets,
                "total_parts": result.total_parts,
                "dxf": str(dxf),
                "dxf_errors": dxf_errors,
                "kerf_errors": kerf_errors,
                "elapsed_seconds": payload["elapsed_seconds"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
