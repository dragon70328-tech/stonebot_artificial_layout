"""Remove weak 3400 x 1400 sheets while preserving quarter-turn freedom."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dxf_writer import write_nested_dxf
from src.nesting import validate_nesting
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf
from src.nesting_reviewer import find_reductions, load_nested_dxf
from src.postprocess import estimate_kerf_material, validate_kerf_material


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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dxf-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--max-reductions", type=int, default=20)
    parser.add_argument("--time-budget", type=float, default=360.0)
    args = parser.parse_args()

    result = load_nested_dxf(args.input)
    for sheet in result.sheets:
        for part in sheet.parts:
            part.allowed_rotations = ROTATIONS
    profile = SimpleNamespace(
        rotation=list(ROTATIONS),
        arbitrary_rotation=False,
        group_mode=None,
    )
    started = time.time()
    moves, reduced = find_reductions(
        result,
        profile=profile,
        max_reductions=args.max_reductions,
        max_donor_parts=8,
        max_donor_fill_ratio=0.88,
        max_donor_attempts=400,
        time_budget_s=args.time_budget,
        per_donor_time_s=30.0,
        max_nodes_per_donor=200000,
        placement_limit=100,
    )
    errors = validate_nesting(reduced, SHEET_WIDTH, SHEET_HEIGHT)
    if errors:
        raise ValueError("; ".join(errors[:10]))
    if reduced.total_parts != 708:
        raise ValueError(f"expected 708 parts, got {reduced.total_parts}")
    write_nested_dxf(
        reduced,
        args.dxf_output,
        unit_system="metric",
        board_size=(SHEET_WIDTH, SHEET_HEIGHT),
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
    payload = {
        "input_dxf": str(args.input),
        "sheet_nominal_mm": [SHEET_WIDTH, SHEET_HEIGHT],
        "rotation_policy": list(ROTATIONS),
        "removed_sheet_count": len(moves),
        "removed_sheets": [
            {
                "removed_sheet": move.removed_sheet_index,
                "moved_parts": move.moved_parts,
                "target_sheets": move.target_sheets,
            }
            for move in moves
        ],
        "total_parts": reduced.total_parts,
        "total_sheets": reduced.total_sheets,
        "yield_rate_percent": round(reduced.yield_rate, 3),
        "elapsed_seconds": round(time.time() - started, 3),
        "dxf_validation_errors": readback_errors,
        "kerf_validation_errors": kerf_errors,
        "kerf_metrics": {
            key: value
            for key, value in estimate_kerf_material(reduced.sheets, 5.0).items()
            if key != "per_sheet"
        },
        "output_dxf": str(args.dxf_output),
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
                    "total_parts",
                    "total_sheets",
                    "removed_sheet_count",
                    "yield_rate_percent",
                    "elapsed_seconds",
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
