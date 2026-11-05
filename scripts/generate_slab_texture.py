"""Generate a texture package from an existing nesting and source DXF."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.slab_texture import (  # noqa: E402
    generate_textured_package,
    load_texture_nesting_result,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从已有排板 DXF 和人造石图案清单生成纹理成果包"
    )
    parser.add_argument("--nested-dxf", type=Path, required=True)
    parser.add_argument("--source-dxf", type=Path, required=True)
    parser.add_argument("--patterns", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="输出目录，默认与排板 DXF 同级并使用 *_纹理原位 名称",
    )
    parser.add_argument(
        "--image-reference-mode",
        choices=["absolute", "relative"],
        default="absolute",
    )
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--preview-max-pixels", type=int, default=1800)
    parser.add_argument("--thickness", type=float, default=20.0)
    args = parser.parse_args()

    nested = args.nested_dxf.expanduser().resolve()
    source = args.source_dxf.expanduser().resolve()
    patterns = args.patterns.expanduser().resolve()
    for label, path in (
        ("nested DXF", nested),
        ("source DXF", source),
        ("pattern manifest", patterns),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
    output = (
        args.output.expanduser().resolve()
        if args.output
        else nested.parent / f"{nested.stem}_纹理原位"
    )
    result = load_texture_nesting_result(
        nested,
        source,
        thickness=args.thickness,
    )
    package = generate_textured_package(
        result,
        source,
        patterns,
        output,
        preview_max_pixels=args.preview_max_pixels,
        image_reference_mode=args.image_reference_mode,
        create_archive=args.archive,
    )
    print(
        json.dumps(
            {
                "output_directory": str(package.output_dir),
                "textured_dxf": str(package.textured_dxf),
                "portable_dxf": (
                    str(package.portable_dxf)
                    if package.portable_dxf is not None
                    else None
                ),
                "archive": (
                    str(package.archive_path)
                    if package.archive_path is not None
                    else None
                ),
                "manifest": str(package.manifest_path),
                "texture_count": package.texture_count,
                "preview_count": len(package.preview_paths),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
