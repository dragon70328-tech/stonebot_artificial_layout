"""Review an existing nested DXF and try to reduce the sheet count locally.

Usage:

    python -X utf8 scripts/review_nesting.py <nested.dxf>

The script reads the DXF produced by ``write_nested_dxf``, looks for
low-occupancy donor sheets whose parts can all be moved into free space on
larger-or-equal sheets, and writes a new DXF when a reduction is found.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.constraints import PROFILE_MIN_SHEETS
from src.dxf_writer import write_nested_dxf
from src.nesting_reviewer import find_reductions, load_nested_dxf


def _parse_rotation(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dxf", help="existing nested DXF path")
    parser.add_argument("--max-reductions", type=int, default=3)
    parser.add_argument("--rotation", default="0,90,180,270")
    parser.add_argument("--min-gap", type=float, default=0.0)
    parser.add_argument("--no-write", action="store_true",
                        help="only print candidates, do not write a new DXF")
    args = parser.parse_args()

    dxf_path = Path(args.dxf)
    if not dxf_path.exists():
        print(f"文件不存在：{dxf_path}")
        return 1

    result = load_nested_dxf(dxf_path)
    print(f"读取排板 DXF：{dxf_path}")
    print(f"  {result.total_sheets} 张板，{result.total_parts} 件，"
          f"出材率 {result.yield_rate:.2f}%")

    profile = PROFILE_MIN_SHEETS.with_overrides(
        rotation=_parse_rotation(args.rotation),
        min_gap=args.min_gap,
    )
    candidates, reduced = find_reductions(
        result,
        profile=profile,
        max_reductions=args.max_reductions,
    )
    if not candidates:
        print("未找到可局部删除的大板。")
        return 0

    for index, candidate in enumerate(candidates, start=1):
        print(
            f"[{index}] 删除 Sheet_{candidate.removed_sheet_index}，"
            f"移入 {', '.join(candidate.moved_parts)} "
            f"到 Sheet {', '.join(f'_{i}' for i in candidate.target_sheets)}"
        )
    print(
        f"减少 {len(candidates)} 张："
        f"{result.total_sheets} -> {reduced.total_sheets} 张，"
        f"出材率 {reduced.yield_rate:.2f}%"
    )

    if args.no_write:
        return 0

    output_path = dxf_path.with_name(f"{dxf_path.stem}_reviewed.dxf")
    write_nested_dxf(reduced, str(output_path), unit_system=result.unit)
    print(f"DXF: {output_path}")

    report_path = output_path.with_suffix(".json")
    report = {
        "original_total_sheets": result.total_sheets,
        "reduced_total_sheets": reduced.total_sheets,
        "reductions": [
            {
                "removed_sheet_index": candidate.removed_sheet_index,
                "moved_parts": candidate.moved_parts,
                "target_sheets": candidate.target_sheets,
            }
            for candidate in candidates
        ],
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
