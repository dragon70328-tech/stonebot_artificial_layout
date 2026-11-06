"""Repair circular-saw neighbour collisions at fixed sheet count."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.saw_repair import repair_nested_saw_clearance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "在保持大板数量不变的前提下，局部移动规格板以消除圆盘锯"
            "切入相邻规格板的风险"
        )
    )
    parser.add_argument("nested_dxf", help="需要修复的排板 DXF")
    parser.add_argument("--diameter", type=float, required=True)
    parser.add_argument("--thickness", type=float, required=True)
    parser.add_argument("--plunge-extra", type=float, default=2.0)
    parser.add_argument("--safety-margin", type=float, default=0.0)
    parser.add_argument("--min-gap", type=float, default=0.0)
    parser.add_argument("--output-dxf")
    parser.add_argument("--manifest")
    parser.add_argument("--max-moves-per-sheet", type=int, default=12)
    parser.add_argument("--fail-on-risk", action="store_true")
    args = parser.parse_args(argv)

    input_path = Path(args.nested_dxf).expanduser().resolve()
    output_path = (
        Path(args.output_dxf).expanduser().resolve()
        if args.output_dxf
        else input_path.with_name(f"{input_path.stem}_让刀修复.dxf")
    )
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else output_path.with_suffix(".json")
    )
    result = repair_nested_saw_clearance(
        input_path,
        output_path,
        diameter_mm=args.diameter,
        thickness_mm=args.thickness,
        plunge_extra_mm=args.plunge_extra,
        safety_margin_mm=args.safety_margin,
        min_gap_mm=max(0.0, args.min_gap),
        max_moves_per_sheet=max(0, args.max_moves_per_sheet),
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(result.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    before = sum(
        item["before"]["points_insufficient"]
        for item in result.updated_sheets
    ) + sum(
        item["points_insufficient"]
        for item in result.unresolved_sheets
    )
    after = result.diagnosis["points_insufficient"]
    print(f"修复前不足切点: {before}")
    print(f"修复后不足切点: {after}")
    print(f"已调整大板: {len(result.updated_sheets)}")
    print(f"仍未解决大板: {len(result.unresolved_sheets)}")
    print(f"输出 DXF: {result.output_path}")
    print(f"修复清单: {manifest_path}")
    if args.fail_on_risk and after:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
