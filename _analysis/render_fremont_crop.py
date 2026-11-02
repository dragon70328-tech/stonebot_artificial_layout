"""Render one rectangular region of the Fremont DXF for visual inspection."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import ezdxf
from ezdxf import bbox

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.visual_renderer import write_dxf_overview_svg


def intersects(entity, region: tuple[float, float, float, float]) -> bool:
    try:
        extents = bbox.extents([entity])
        if not extents.has_data:
            return False
        x0, y0, x1, y1 = region
        return not (
            extents.extmax.x < x0
            or extents.extmin.x > x1
            or extents.extmax.y < y0
            or extents.extmin.y > y1
        )
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("handle")
    parser.add_argument("output", type=Path)
    parser.add_argument("--margin", type=float, default=500.0)
    args = parser.parse_args()

    source_doc = ezdxf.readfile(args.source)
    source_msp = source_doc.modelspace()
    frame = source_msp.entitydb[args.handle]
    frame_box = list(frame.get_points("xy"))
    xs = [float(point[0]) for point in frame_box]
    ys = [float(point[1]) for point in frame_box]
    region = (
        min(xs) - args.margin,
        min(ys) - args.margin,
        max(xs) + args.margin,
        max(ys) + args.margin,
    )

    target_doc = ezdxf.new(source_doc.dxfversion)
    target_msp = target_doc.modelspace()
    for entity in source_msp:
        if entity.dxftype() == "DIMENSION":
            try:
                virtual = list(entity.virtual_entities())
            except Exception:
                virtual = []
            if virtual and any(intersects(item, region) for item in virtual):
                for item in virtual:
                    if intersects(item, region):
                        try:
                            target_msp.add_foreign_entity(item, copy=True)
                        except Exception:
                            pass
            continue
        if intersects(entity, region):
            try:
                target_msp.add_foreign_entity(entity, copy=True)
            except Exception:
                pass

    cropped_path = args.output.with_suffix(".crop.dxf")
    target_doc.saveas(cropped_path)
    write_dxf_overview_svg(
        cropped_path,
        args.output,
        svg_size=1600,
        include_text=True,
    )
    print(args.output)
    print("region", region)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
