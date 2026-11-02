"""Nest the Fremont ST-1/ST-2 countertops as one material group."""

from __future__ import annotations

import argparse
import functools
import json
import math
import random
import sys
from collections import Counter
from itertools import permutations
from pathlib import Path

import ezdxf
from shapely.affinity import rotate
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models import Part
from src.dxf_writer import write_nested_dxf
import src.nesting as nesting_module
from src.nesting import nest_parts
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf
from src.postprocess import estimate_kerf_material, validate_kerf_material


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--quarter-turn",
        action="store_true",
        help="Allow 0/90 degree placement. Without this flag, strict 0-only is used.",
    )
    parser.add_argument(
        "--all-quarter-turns",
        action="store_true",
        help="Allow 0/90/180/270 degree placement.",
    )
    parser.add_argument(
        "--rotate-only-oversized",
        action="store_true",
        help=(
            "Pre-rotate only panels that cannot fit the unrotated 3000x2000 "
            "sheet, then keep every panel at final 0 degree."
        ),
    )
    parser.add_argument("--sheet-width", type=float, default=3000.0)
    parser.add_argument("--sheet-height", type=float, default=2000.0)
    parser.add_argument(
        "--rotation-order",
        default="0,90,180,270",
        help="Comma-separated allowed rotation order.",
    )
    parser.add_argument("--beam-width", type=int, default=1000)
    parser.add_argument("--beam-budget", type=float, default=60.0)
    parser.add_argument("--improve-budget", type=float, default=60.0)
    parser.add_argument("--pattern-reuse", type=int, default=4)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--orientation-seed", type=int)
    parser.add_argument(
        "--normalize-orientation",
        choices=("none", "long-horizontal", "long-vertical"),
        default="none",
    )
    parser.add_argument("--no-beam", action="store_true")
    parser.add_argument(
        "--greedy-configs",
        default="short:skyline:0",
        help="Comma-separated sort:mode:seed greedy fallback configs.",
    )
    parser.add_argument("--max-iterations", type=int, default=40)
    parser.add_argument("--patience", type=int, default=12)
    parser.add_argument("--compress-subsets", default="")
    parser.add_argument("--compress-beam-width", type=int, default=150)
    parser.add_argument("--compress-part-cap", type=int, default=60)
    parser.add_argument("--min-sheets", type=int)
    parser.add_argument("--label", default="")
    parser.add_argument(
        "--eliminate-weak-sheets",
        action="store_true",
        help="After nesting, try to eliminate the weakest sheets by reinserting their parts.",
    )
    parser.add_argument(
        "--bbox-rect-fast",
        action="store_true",
        help="Try the guillotine rect fast path using conservative bounding boxes.",
    )
    parser.add_argument("--bbox-rect-beam-width", type=int, default=120)
    parser.add_argument("--bbox-rect-max-patterns", type=int, default=400)
    parser.add_argument("--dxf-output", type=Path)
    args = parser.parse_args()
    if args.bbox_rect_fast:
        import src.rect_nesting as rect_nesting

        rect_nesting.RECT_FILL_RATIO = 0.60
        rect_nesting.rect_fast_layout = functools.partial(
            rect_nesting.rect_fast_layout,
            beam_width=args.bbox_rect_beam_width,
            max_patterns=args.bbox_rect_max_patterns,
        )
    if args.compress_subsets:
        subset_sizes = tuple(
            int(value.strip())
            for value in args.compress_subsets.split(",")
            if value.strip()
        )
        if not subset_sizes or any(value < 2 for value in subset_sizes):
            raise ValueError("compress subsets must be a comma-separated list >= 2")
        nesting_module._compress_sheets = functools.partial(
            nesting_module._compress_sheets,
            subset_sizes=subset_sizes,
            beam_width=args.compress_beam_width,
            beam_part_cap=args.compress_part_cap,
        )
    greedy_configs = []
    for raw in args.greedy_configs.split(","):
        if not raw.strip():
            continue
        fields = raw.strip().split(":")
        if len(fields) != 3:
            raise ValueError(f"invalid greedy config: {raw!r}")
        sort_name, mode, seed_text = fields
        greedy_configs.append((sort_name, mode, int(seed_text)))
    if sum(
        (
            bool(args.quarter_turn),
            bool(args.all_quarter_turns),
            bool(args.rotate_only_oversized),
        )
    ) > 1:
        raise ValueError("choose one rotation mode")
    rotation_order = tuple(
        int(value.strip())
        for value in args.rotation_order.split(",")
        if value.strip()
    )
    if not rotation_order or any(
        value % 90 != 0 for value in rotation_order
    ):
        raise ValueError("rotation order must contain multiples of 90")
    if args.all_quarter_turns:
        rotations = rotation_order
        mode_name = "all_quarter_turns"
    elif args.quarter_turn:
        rotations = (0, 90)
        mode_name = "quarter_turn"
    elif args.rotate_only_oversized:
        rotations = (0,)
        mode_name = "only_oversized_quarter_turn"
    else:
        rotations = (0,)
        mode_name = "strict_zero"
    sheet_width = float(args.sheet_width)
    sheet_height = float(args.sheet_height)
    mode_name = f"{int(sheet_width)}x{int(sheet_height)}_{mode_name}"
    if args.label:
        mode_name = f"{mode_name}_{args.label}"
    validation_dir = ROOT / "_analysis" / "fremont_countertop_validation"
    drawing_path = validation_dir / "户型20261102_台面板原位_编号.dxf"
    manifest_path = validation_dir / "户型20261102_台面板编号清单.json"
    output_path = (
        ROOT / "_analysis" / f"fremont_st12_nesting_result_{mode_name}.json"
    )

    manifest = json.loads(manifest_path.read_text("utf-8"))
    records = [
        item
        for item in manifest["countertops"]
        if item["material"] in {"ST/1", "ST/2"}
    ]
    if len(records) != 708:
        raise ValueError(f"expected 708 ST-1/ST-2 countertops, got {len(records)}")

    doc = ezdxf.readfile(drawing_path)
    msp = doc.modelspace()
    parts: list[Part] = []
    rotated_parts: list[str] = []
    orientation_rng = (
        random.Random(args.orientation_seed)
        if args.orientation_seed is not None
        else None
    )
    for item in sorted(records, key=lambda value: value["number"]):
        entity = msp.entitydb[item["handle"]]
        polygon = Polygon(
            [(float(x), float(y)) for x, y in entity.get_points("xy")]
        )
        if not polygon.is_valid or polygon.area <= 0:
            raise ValueError(f"invalid polygon for {item['number']}")
        placement_rot = 0.0
        if (
            args.rotate_only_oversized
            and item["bbox"][3] - item["bbox"][1] > 2000.0
            and item["bbox"][2] - item["bbox"][0] <= 2000.0
            and item["bbox"][3] - item["bbox"][1] <= 3000.0
        ):
            polygon = rotate(polygon, 90.0, origin="centroid")
            placement_rot = 90.0
            rotated_parts.append(item["number"])
        min_x, min_y, max_x, max_y = polygon.bounds
        width, height = max_x - min_x, max_y - min_y
        pre_rotate = (
            (args.normalize_orientation == "long-horizontal" and height > width)
            or (args.normalize_orientation == "long-vertical" and width > height)
        )
        if pre_rotate:
            polygon = rotate(polygon, 90.0, origin="centroid")
        if orientation_rng is not None:
            pre_angle = orientation_rng.choice((0.0, 90.0, 180.0, 270.0))
            if pre_angle:
                polygon = rotate(polygon, pre_angle, origin="centroid")
        parts.append(
            Part(
                id=len(parts) + 1,
                number=item["number"],
                polygon=polygon,
                outer_polygon=polygon,
                material_group="ST-1+ST-2",
                material_column="ST-1+ST-2",
                group_id=item["frame_handle"],
                area=polygon.area,
                label_position=tuple(item["number_position"]),
                outer_handle=item["handle"],
                source_outer_polygon=polygon,
                placement_rot=placement_rot,
            )
        )

    if not (args.quarter_turn or args.all_quarter_turns):
        invalid_unrotated = [
            part.number
            for part in parts
            if part.outer_polygon.bounds[2] - part.outer_polygon.bounds[0]
            > sheet_width + 0.01
            or part.outer_polygon.bounds[3] - part.outer_polygon.bounds[1]
            > sheet_height + 0.01
        ]
        if invalid_unrotated:
            raise ValueError(
                f"{len(invalid_unrotated)} parts still do not fit after required "
                f"rotation: {invalid_unrotated}"
            )

    sheet_area = sheet_width * sheet_height
    total_part_area = sum(part.area for part in parts)
    area_lower_bound = math.ceil(total_part_area / sheet_area)
    stats: dict = {}
    result = nest_parts(
        parts,
        sheet_width,
        sheet_height,
        20.0,
        unit="metric",
        configs=greedy_configs,
        improve_budget=args.improve_budget,
        beam_budget=args.beam_budget,
        beam_width=args.beam_width,
        rotations=rotations,
        min_gap=0.0,
        min_sheets=(
            args.min_sheets
            if args.min_sheets is not None
            else area_lower_bound
        ),
        beam=not args.no_beam,
        compress=True,
        rect_fast=True,
        pattern_reuse=args.pattern_reuse,
        seed=args.seed,
        improve_iterations=args.max_iterations,
        improve_patience=args.patience,
        stats=stats,
    )

    def eliminate_weak_sheets(current_result):
        raw_sheets = []
        part_cache: dict = {}
        for sheet in current_result.sheets:
            placements = []
            for placed in sheet.parts:
                source = placed.source_outer_polygon
                if source is None:
                    source = placed.outer_polygon
                unplaced = Part(
                    id=placed.id,
                    number=placed.number,
                    polygon=source,
                    outer_polygon=source,
                    material_group=placed.material_group,
                    material_column=placed.material_column,
                    group_id=placed.group_id,
                    area=placed.area,
                    label_position=placed.label_position,
                    outer_handle=placed.outer_handle,
                    source_outer_polygon=source,
                )
                min_x, min_y, _max_x, _max_y = placed.outer_polygon.bounds
                placements.append(
                    nesting_module._Placement(
                        part=unplaced,
                        rot=float(placed.placement_rot or 0.0),
                        x=float(min_x),
                        y=float(min_y),
                        poly=placed.outer_polygon,
                    )
                )
            raw_sheets.append(placements)

        def try_eliminate(raw):
            order = sorted(
                range(len(raw)),
                key=lambda index: sum(item.part.area for item in raw[index]),
            )
            for victim_index in order[:20]:
                victim = raw[victim_index]
                if not victim or len(victim) > 4:
                    continue
                targets = [list(items) for index, items in enumerate(raw) if index != victim_index]
                target_order = sorted(
                    range(len(targets)),
                    key=lambda index: sum(item.part.area for item in targets[index]),
                )
                ordered_variants = list(
                    permutations(sorted(victim, key=lambda item: -item.part.area))
                )[:6]
                for ordered_parts in ordered_variants:
                    for mode in ("contact", "skyline"):
                        trial = [list(items) for items in targets]
                        trial_cache = {}
                        success = True
                        for source_pl in ordered_parts:
                            placed_target = None
                            for target_index in target_order:
                                target = trial[target_index]
                                geoms = [item.poly for item in target]
                                boxes = [tuple(geom.bounds) for geom in geoms]
                                reflex = [
                                    point
                                    for geom in geoms
                                    for point in nesting_module._reflex_vertices(geom)
                                ]
                                placement = nesting_module._find_placement(
                                    source_pl.part,
                                    geoms,
                                    boxes,
                                    reflex,
                                    sheet_width,
                                    sheet_height,
                                    mode,
                                    trial_cache,
                                    rotations=rotations,
                                    min_gap=0.0,
                                )
                                if placement is not None:
                                    placed_target = (target_index, placement)
                                    break
                            if placed_target is None:
                                success = False
                                break
                            target_index, placement = placed_target
                            trial[target_index].append(placement)
                        if success:
                            candidate = trial
                            candidate_result = nesting_module._materialize(
                                candidate,
                                sheet_width,
                                sheet_height,
                                20.0,
                                "metric",
                                len(parts),
                            )
                            errors = nesting_module.validate_nesting(
                                candidate_result,
                                sheet_width,
                                sheet_height,
                                min_gap=0.0,
                            )
                            if not errors and candidate_result.total_parts == len(parts):
                                return candidate
            return None

        improved = raw_sheets
        while True:
            candidate = try_eliminate(improved)
            if candidate is None:
                break
            improved = candidate
        return nesting_module._materialize(
            improved,
            sheet_width,
            sheet_height,
            20.0,
            "metric",
            len(parts),
        )

    if args.eliminate_weak_sheets:
        result = eliminate_weak_sheets(result)

    kerf_metrics = estimate_kerf_material(result.sheets, 5.0)
    kerf_errors = validate_kerf_material(
        result.sheets,
        5.0,
        allowance_x_mm=30.0,
        allowance_y_mm=30.0,
    )
    if result.total_parts != len(parts):
        raise ValueError("nesting result lost parts")
    dxf_validation_errors: list[str] = []
    if args.dxf_output is not None:
        args.dxf_output.parent.mkdir(parents=True, exist_ok=True)
        write_nested_dxf(
            result,
            args.dxf_output,
            unit_system="metric",
            board_size=(sheet_width, sheet_height),
        )
        dxf_validation_errors = verify_nested_dxf(
            args.dxf_output,
            sheets_payload_from_result(result),
            gap=200.0,
        )
        if dxf_validation_errors:
            raise ValueError(
                "nested DXF readback failed: "
                + "; ".join(dxf_validation_errors)
            )

    payload = {
        "input_parts": len(parts),
        "materials_merged": ["ST/1", "ST/2"],
        "rotated_parts": rotated_parts,
        "rotated_part_count": len(rotated_parts),
        "source_material_counts": dict(Counter(item["material"] for item in records)),
        "sheet_nominal_mm": [sheet_width, sheet_height],
        "sheet_oversize_total_mm": [30.0, 30.0],
        "kerf_mm": 5.0,
        "rotation": rotations,
        "orientation_seed": args.orientation_seed,
        "process": "bridge",
        "total_part_area_mm2": round(total_part_area, 3),
        "area_lower_bound_sheets": area_lower_bound,
        "total_sheets": result.total_sheets,
        "yield_rate_percent": round(result.yield_rate, 3),
        "output_dxf": str(args.dxf_output) if args.dxf_output else None,
        "dxf_validation_errors": dxf_validation_errors,
        "total_parts": result.total_parts,
        "kerf_validation_errors": kerf_errors,
        "kerf_metrics": {
            key: value
            for key, value in kerf_metrics.items()
            if key != "per_sheet"
        },
        "stats": stats,
        "sheets": [
            {
                "index": sheet.index,
                "parts": len(sheet.parts),
                "used_area_mm2": round(sheet.used_area, 3),
                "remaining_area_mm2": round(
                    sheet.width * sheet.height - sheet.used_area, 3
                ),
                "fill_percent": round(
                    sheet.used_area / (sheet.width * sheet.height) * 100.0,
                    3,
                ),
                "part_numbers": [part.number for part in sheet.parts],
            }
            for sheet in result.sheets
        ],
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
