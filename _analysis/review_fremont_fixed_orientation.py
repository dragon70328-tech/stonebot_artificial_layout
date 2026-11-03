"""Review a fixed-orientation Fremont layout without violating part angles."""

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
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf
from src.nesting_reviewer import find_reductions, load_nested_dxf


def load_fixed_module():
    path = ROOT / "_analysis" / "nest_fremont_st12_fixed_orientation.py"
    spec = importlib.util.spec_from_file_location("fixednest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--relaxed-l-count", type=int, default=0)
    args = parser.parse_args()

    fixed = load_fixed_module()
    source_parts, _records, _summary = fixed.parse_parts(
        args.source,
        3000.0,
        2000.0,
        args.manifest,
    )
    allowed = {
        part.number: tuple(getattr(part, "allowed_rotations", (0.0,)))
        for part in source_parts
    }
    flexible_l = sorted(
        (
            part.number
            for part in source_parts
            if tuple(part.allowed_rotations or ()) == (0.0, 180.0)
            and abs(part.area - 1_677_416.0) < 2000.0
        ),
        key=str,
    )
    for number in flexible_l[:args.relaxed_l_count]:
        allowed[number] = (0.0, 90.0, 180.0, 270.0)
    result = load_nested_dxf(args.input)
    profile = SimpleNamespace(
        rotation=[0],
        arbitrary_rotation=False,
        group_mode=None,
    )
    for sheet in result.sheets:
        for part in sheet.parts:
            part.allowed_rotations = allowed.get(part.number, (0.0,))

    moves: list[dict] = []
    for round_index in range(1, 6):
        started = time.time()
        candidates, updated = find_reductions(
            result,
            profile=profile,
            max_reductions=30,
            max_donor_parts=6,
            max_donor_fill_ratio=0.88,
            max_donor_attempts=260,
            time_budget_s=480.0,
            per_donor_time_s=35.0,
            max_nodes_per_donor=180000,
            placement_limit=60,
        )
        for sheet in updated.sheets:
            for part in sheet.parts:
                part.allowed_rotations = allowed.get(part.number, (0.0,))
        print(
            f"round={round_index} removed={len(candidates)} "
            f"sheets={updated.total_sheets} "
            f"seconds={time.time() - started:.1f}",
            flush=True,
        )
        for candidate in candidates:
            moves.append(
                {
                    "round": round_index,
                    "removed_sheet": candidate.removed_sheet_index,
                    "moved_parts": candidate.moved_parts,
                    "target_sheets": candidate.target_sheets,
                }
            )
        if not candidates:
            break
        result = updated

    dxf = Path(f"{args.output_prefix}_{result.total_sheets}.dxf")
    data = dxf.with_suffix(".json")
    write_nested_dxf(
        result,
        dxf,
        unit_system="metric",
        board_size=(3000.0, 2000.0),
    )
    errors = verify_nested_dxf(
        dxf,
        sheets_payload_from_result(result),
        gap=200.0,
    )
    payload = {
        "total_parts": result.total_parts,
        "total_sheets": result.total_sheets,
        "yield_rate_percent": round(result.yield_rate, 3),
        "rotation_allowed": {"normal": [0], "l_pair": [0, 180]},
        "relaxed_l_count": args.relaxed_l_count,
        "moves": moves,
        "output_dxf": str(dxf),
        "dxf_validation_errors": errors,
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
    print(json.dumps({"total_sheets": result.total_sheets, "dxf": str(dxf), "errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
