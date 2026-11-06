"""Apply a manually adjusted nested-DXF sheet to similar board recipes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.layout_adjustment import propagate_adjusted_layout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "把用户在排板 DXF 中调整过的一张样板大板，按相同规格板库存"
            "传播到其他同类大板"
        )
    )
    parser.add_argument("adjusted_dxf", help="用户调整后另存的排板 DXF")
    parser.add_argument(
        "--reference-sheet",
        type=int,
        required=True,
        help="样板大板编号（DXF 中的 Sheet_n，从 1 开始）",
    )
    parser.add_argument(
        "--output-dxf",
        help="输出 DXF；默认在输入文件旁生成 *_布局调整传播.dxf",
    )
    parser.add_argument(
        "--manifest",
        help="可选 JSON 清单路径；默认与输出 DXF 同名 .json",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.adjusted_dxf).expanduser().resolve()
    output_path = (
        Path(args.output_dxf).expanduser().resolve()
        if args.output_dxf
        else input_path.with_name(
            f"{input_path.stem}_布局调整传播.dxf"
        )
    )
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else output_path.with_suffix(".json")
    )

    result = propagate_adjusted_layout(
        input_path,
        args.reference_sheet,
        output_path,
    )
    payload = result.as_dict()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"样板大板: Sheet_{result.reference_sheet_index}")
    print(
        "同类大板: "
        + ", ".join(
            f"Sheet_{index}" for index in result.matched_sheet_indices
        )
    )
    print(
        "已更新: "
        + (
            ", ".join(
                f"Sheet_{index}"
                for index in result.updated_sheet_indices
            )
            or "无"
        )
    )
    if result.skipped_sheets:
        print(f"未应用: {len(result.skipped_sheets)} 张，详见清单")
    print(f"输出 DXF: {result.output_path}")
    print(f"调整清单: {manifest_path}")
    return 0 if not result.skipped_sheets else 1


if __name__ == "__main__":
    raise SystemExit(main())
