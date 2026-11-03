"""Search deterministic orientation priorities with learned bridge templates."""

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

from src.bridge_template_learning import nest_with_bridge_templates
from src.dxf_writer import write_nested_dxf
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
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--template-min-fill", type=float, default=0.0)
    parser.add_argument("--relaxed-l-count", type=int, default=0)
    args = parser.parse_args()

    fixed = load_fixed_module()
    parts, records, summary = fixed.parse_parts(
        args.source,
        3000.0,
        2000.0,
        args.manifest,
    )
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
    relaxed_l = set(flexible_l[:args.relaxed_l_count])
    for part in parts:
        if part.number in relaxed_l:
            part.allowed_rotations = (0.0, 90.0, 180.0, 270.0)
    rng = random.Random(args.seed)
    group_order: dict[str, tuple[float, float]] = {}
    for record in records:
        key = str(record["frame_handle"])
        if (
            record["frozen_by_l_pair"]
            and record["material"] == "ST/1"
            and key not in group_order
        ):
            group_order[key] = (
                (0.0, 180.0) if rng.random() < 0.5 else (180.0, 0.0)
            )
    frame_by_number = {
        str(record["number"]): str(record["frame_handle"])
        for record in records
    }
    for part in parts:
        if (
            part.number not in relaxed_l
            and frame_by_number[part.number] in group_order
        ):
            part.allowed_rotations = group_order[frame_by_number[part.number]]

    stats: dict = {}
    started = time.time()
    result = nest_with_bridge_templates(
        parts,
        args.template,
        3000.0,
        2000.0,
        20.0,
        unit="metric",
        template_min_fill_ratio=args.template_min_fill,
        template_shape_tolerance=0.2,
        fallback_rotations=(0.0, 180.0),
        stats=stats,
        nest_kwargs={
            "configs": [
                ("short", "skyline", args.seed),
                ("area", "skyline", args.seed),
                ("area", "column", args.seed),
                ("long", "skyline", args.seed),
                ("short", "contact", args.seed),
            ],
            "rotations": (0.0, 180.0),
            "min_gap": 0.0,
            "beam": True,
            "beam_width": 1000,
            "beam_budget": 300.0,
            "improve_budget": 180.0,
            "compress": True,
            "rect_fast": True,
            "pattern_reuse": 20,
            "seed": args.seed,
            "improve_iterations": 300,
            "improve_patience": 120,
            "min_sheets": 119,
        },
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
        "template_stats": stats,
        "relaxed_l_count": len(relaxed_l),
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
    data.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "seed": args.seed,
                "total_sheets": result.total_sheets,
                "total_parts": result.total_parts,
                "template_sheets": stats["template_sheets_reused"],
                "template_parts": stats["template_parts_reused"],
                "dxf": str(dxf),
                "errors": [*dxf_errors, *kerf_errors],
                "elapsed_seconds": payload["elapsed_seconds"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
