"""CLI: convert a multi-page vector PDF into one in-place DXF."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.pdf_intake import (  # noqa: E402
    PageMap,
    analyze_pdf,
    convert_model_to_dxf,
    inspect_pdf,
    validate_converted_dxf,
)


def _page_arg(value: str) -> PageMap:
    try:
        page_text, floor, prefix = value.split(":", 2)
        return PageMap(
            page_number=int(page_text),
            floor=floor.strip(),
            number_prefix=prefix.strip(),
        )
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "页面映射格式应为 PAGE:FLOOR:PREFIX，例如 1:D6F:D6F"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "把一个全矢量多页 PDF 转换为单一原位 DXF；"
            "结果包含楼层块、A4 布局、PDF 矢量层、板件 GROUP、材料与 R450 真弧。"
        )
    )
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("output_dxf", type=Path)
    parser.add_argument(
        "--page",
        dest="pages",
        action="append",
        type=_page_arg,
        required=True,
        help="页面映射，可重复，例如 --page 1:D6F:D6F",
    )
    parser.add_argument(
        "--audit-json",
        type=Path,
        help="可选：转换与回读审计 JSON 输出路径",
    )
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--expected-boards", type=int)
    parser.add_argument("--expected-r450", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    preflight = inspect_pdf(args.source_pdf)
    if preflight.encrypted:
        raise SystemExit("PDF 已加密，不能进入生产转换")
    if not preflight.is_vector:
        raise SystemExit("PDF 不是全矢量图纸，不能生成原位 DXF")

    model = analyze_pdf(args.source_pdf, args.pages)
    audit = convert_model_to_dxf(model, args.output_dxf)
    validation_issues = validate_converted_dxf(
        args.output_dxf,
        expected_pages=args.expected_pages,
        expected_board_labels=args.expected_boards,
        expected_r450=args.expected_r450,
    )
    report = {
        "preflight": {
            "sha256": preflight.sha256,
            "file_size": preflight.file_size,
            "page_count": preflight.page_count,
            "page_sizes": preflight.page_sizes,
            "producer": preflight.producer,
            "vector_pages": preflight.vector_pages,
            "text_pages": preflight.text_pages,
            "raster_images": preflight.raster_images,
        },
        "conversion": audit.as_dict(),
        "validation_issues": validation_issues,
    }
    if args.audit_json:
        args.audit_json.parent.mkdir(parents=True, exist_ok=True)
        args.audit_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if validation_issues or audit.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
