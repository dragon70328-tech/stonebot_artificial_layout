"""Relocate IMAGEDEF references after moving a texture package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.slab_texture import relocate_texture_references  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把纹理 DXF 的 IMAGEDEF 重定位到当前目录的 textures/"
    )
    parser.add_argument("dxf", type=Path)
    parser.add_argument(
        "--texture-directory",
        type=Path,
        help="图片目录，默认使用 DXF 同级的 textures/",
    )
    args = parser.parse_args()
    dxf = args.dxf.expanduser().resolve()
    if not dxf.is_file():
        raise FileNotFoundError(f"DXF not found: {dxf}")
    updated = relocate_texture_references(
        dxf,
        args.texture_directory,
    )
    print(
        json.dumps(
            {
                "dxf": str(dxf),
                "updated_image_defs": updated,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
