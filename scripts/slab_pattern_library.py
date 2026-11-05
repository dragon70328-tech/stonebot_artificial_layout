"""Manage the project-level artificial-stone pattern library."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.slab_pattern_library import (  # noqa: E402
    list_pattern_assets,
    register_pattern_asset,
    validate_pattern_library,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="人造石标准图案资产库")
    parser.add_argument(
        "--library-dir",
        type=Path,
        default=ROOT / "slab_patterns",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register")
    register.add_argument("--pattern-id", required=True)
    register.add_argument("--image", type=Path, required=True)
    register.add_argument("--materials", required=True)
    register.add_argument("--usable-width-mm", type=float, required=True)
    register.add_argument("--usable-height-mm", type=float, required=True)
    register.add_argument("--max-pixels-per-mm", type=float)
    register.add_argument("--overwrite", action="store_true")

    commands.add_parser("list")
    commands.add_parser("validate")
    args = parser.parse_args()

    if args.command == "register":
        path = register_pattern_asset(
            args.library_dir,
            pattern_id=args.pattern_id,
            image_path=args.image,
            materials=[
                value.strip()
                for value in args.materials.split(",")
                if value.strip()
            ],
            usable_width_mm=args.usable_width_mm,
            usable_height_mm=args.usable_height_mm,
            max_pixels_per_mm=args.max_pixels_per_mm,
            overwrite=args.overwrite,
        )
        print(path)
        return 0
    if args.command == "list":
        print(
            json.dumps(
                list_pattern_assets(args.library_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    count = validate_pattern_library(args.library_dir)
    print(json.dumps({"valid": True, "patterns": count}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
