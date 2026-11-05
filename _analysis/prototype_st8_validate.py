"""Build and validate real DXF candidates for fixed-orientation ST-8 layouts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ezdxf
from shapely import affinity
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dxf_writer import write_nested_dxf
from src.models import Part
from src.nesting import _materialize, validate_nesting
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf
from src.postprocess import estimate_kerf_material, validate_kerf_material
from src.rect_nesting import RectPlacement


SOURCE = Path(
    r"C:\Users\drago\Desktop\临时文件\AI识图文件"
    r"\citizen_ST-08_全部2018_numbered_原位.dxf"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "st8_candidates"
SHEET_W = 3200.0
SHEET_H = 1600.0


def load_parts() -> list[Part]:
    doc = ezdxf.readfile(SOURCE)
    modelspace = doc.modelspace()
    polygons = [
        Polygon([
            (float(x), float(y))
            for x, y in entity.get_points("xy")
        ])
        for entity in modelspace
        if (
            entity.dxftype() == "LWPOLYLINE"
            and str(entity.dxf.layer) == "OUTER"
        )
    ]
    tree = STRtree(polygons)
    parts = []
    for entity in modelspace:
        if entity.dxftype() not in {"TEXT", "MTEXT"}:
            continue
        if str(entity.dxf.layer) != "NUMBERS":
            continue
        number = (
            str(entity.dxf.text).strip()
            if entity.dxftype() == "TEXT"
            else str(entity.text).strip()
        )
        point = Point(
            float(entity.dxf.insert.x),
            float(entity.dxf.insert.y),
        )
        matches = tree.query(point, predicate="covered_by")
        if not matches:
            nearest_index = int(tree.nearest(point))
            nearest_polygon = polygons[nearest_index]
            if nearest_polygon.distance(point) <= 1.0:
                matches = [nearest_index]
            else:
                raise ValueError(f"{number}: no containing outline")
        polygon = polygons[int(matches[0])]
        parts.append(
            Part(
                id=len(parts) + 1,
                number=number,
                polygon=polygon,
                outer_polygon=polygon,
                area=polygon.area,
                material_group="ST-08",
                packing_group="ST-08",
                label_position=tuple(polygon.representative_point().coords[0]),
                source_outer_polygon=polygon,
                placement_rot=0.0,
                allowed_rotations=(0.0,),
            )
        )
    if len(parts) != 2018:
        raise ValueError(f"expected 2018 parts, found {len(parts)}")
    return parts


def pack_layout(
    parts: list[Part],
    *,
    strategy: str,
    heuristic: str,
) -> list[list[RectPlacement]]:
    dimensions = [
        (
            round(part.outer_polygon.bounds[2] - part.outer_polygon.bounds[0], 1),
            round(part.outer_polygon.bounds[3] - part.outer_polygon.bounds[1], 1),
        )
        for part in parts
    ]
    if strategy == "area":
        ordered = sorted(
            range(len(parts)),
            key=lambda index: (
                -dimensions[index][0] * dimensions[index][1],
                -max(dimensions[index]),
            ),
        )
    elif strategy == "width":
        ordered = sorted(
            range(len(parts)),
            key=lambda index: (
                -dimensions[index][0],
                -dimensions[index][1],
            ),
        )
    else:
        raise ValueError(strategy)

    sheet_free: list[list[tuple[float, float, float, float]]] = []
    sheet_parts: list[list[RectPlacement]] = []
    for part_index in ordered:
        part = parts[part_index]
        min_x, min_y, max_x, max_y = part.outer_polygon.bounds
        width = max_x - min_x
        height = max_y - min_y
        best = None
        for sheet_index, free in enumerate(sheet_free):
            for rect_index, (x, y, free_w, free_h) in enumerate(free):
                if width > free_w + 1e-9 or height > free_h + 1e-9:
                    continue
                leftover_w = free_w - width
                leftover_h = free_h - height
                if heuristic == "short":
                    score = (
                        min(leftover_w, leftover_h),
                        max(leftover_w, leftover_h),
                    )
                elif heuristic == "area":
                    score = (
                        leftover_w * leftover_h,
                        min(leftover_w, leftover_h),
                    )
                else:
                    score = (
                        y + height,
                        x,
                        leftover_w * leftover_h,
                    )
                key = (*score, sheet_index, rect_index)
                if best is None or key < best[0]:
                    best = (key, sheet_index, rect_index, x, y)
        if best is None:
            sheet_free.append([(0.0, 0.0, SHEET_W, SHEET_H)])
            sheet_parts.append([])
            sheet_index = len(sheet_free) - 1
            free = sheet_free[sheet_index]
            x, y = 0.0, 0.0
        else:
            _, sheet_index, _, x, y = best
            free = sheet_free[sheet_index]

        placed_polygon = affinity.translate(
            part.outer_polygon,
            xoff=x - min_x,
            yoff=y - min_y,
        )
        sheet_parts[sheet_index].append(
            RectPlacement(
                part=part,
                rot=0.0,
                x=x,
                y=y,
                poly=placed_polygon,
            )
        )
        updated = []
        placed_right = x + width
        placed_top = y + height
        for fx, fy, free_w, free_h in free:
            if (
                placed_right <= fx + 1e-9
                or x >= fx + free_w - 1e-9
                or placed_top <= fy + 1e-9
                or y >= fy + free_h - 1e-9
            ):
                updated.append((fx, fy, free_w, free_h))
                continue
            if x > fx + 1e-9:
                updated.append((fx, fy, x - fx, free_h))
            if placed_right < fx + free_w - 1e-9:
                updated.append(
                    (placed_right, fy, fx + free_w - placed_right, free_h)
                )
            if y > fy + 1e-9:
                updated.append((fx, fy, free_w, y - fy))
            if placed_top < fy + free_h - 1e-9:
                updated.append(
                    (fx, placed_top, free_w, fy + free_h - placed_top)
                )
        kept = []
        for index, candidate in enumerate(updated):
            covered = any(
                other != index
                and candidate[0] >= updated[other][0] - 1e-9
                and candidate[1] >= updated[other][1] - 1e-9
                and candidate[0] + candidate[2]
                <= updated[other][0] + updated[other][2] + 1e-9
                and candidate[1] + candidate[3]
                <= updated[other][1] + updated[other][3] + 1e-9
                for other in range(len(updated))
            )
            if not covered:
                kept.append(candidate)
        sheet_free[sheet_index] = kept
    return sheet_parts


def is_guillotine(rectangles: list[tuple[float, float, float, float]]) -> bool:
    if len(rectangles) <= 1:
        return True
    min_x = min(item[0] for item in rectangles)
    min_y = min(item[1] for item in rectangles)
    max_x = max(item[0] + item[2] for item in rectangles)
    max_y = max(item[1] + item[3] for item in rectangles)
    for cut in sorted({item[0] for item in rectangles} | {item[0] + item[2] for item in rectangles}):
        if cut <= min_x + 1e-9 or cut >= max_x - 1e-9:
            continue
        if any(item[0] < cut < item[0] + item[2] for item in rectangles):
            continue
        left = [item for item in rectangles if item[0] < cut]
        right = [item for item in rectangles if item[0] > cut]
        if left and right and is_guillotine(left) and is_guillotine(right):
            return True
    for cut in sorted({item[1] for item in rectangles} | {item[1] + item[3] for item in rectangles}):
        if cut <= min_y + 1e-9 or cut >= max_y - 1e-9:
            continue
        if any(item[1] < cut < item[1] + item[3] for item in rectangles):
            continue
        bottom = [item for item in rectangles if item[1] < cut]
        top = [item for item in rectangles if item[1] > cut]
        if bottom and top and is_guillotine(bottom) and is_guillotine(top):
            return True
    return False


def validate_candidate(name: str, placements: list[list[RectPlacement]]) -> dict:
    result = _materialize(
        placements,
        SHEET_W,
        SHEET_H,
        20.0,
        "metric",
        sum(len(sheet) for sheet in placements),
    )
    geometry_errors = validate_nesting(result, SHEET_W, SHEET_H)
    kerf_errors = validate_kerf_material(
        result.sheets,
        5.0,
        allowance_x_mm=30.0,
        allowance_y_mm=30.0,
    )
    guillotine_failures = []
    for sheet in result.sheets:
        rectangles = [
            (
                part.outer_polygon.bounds[0],
                part.outer_polygon.bounds[1],
                part.outer_polygon.bounds[2] - part.outer_polygon.bounds[0],
                part.outer_polygon.bounds[3] - part.outer_polygon.bounds[1],
            )
            for part in sheet.parts
        ]
        if not is_guillotine(rectangles):
            guillotine_failures.append(sheet.index)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dxf_path = OUTPUT_DIR / f"citizen_ST-08_{name}.dxf"
    write_nested_dxf(
        result,
        str(dxf_path),
        unit_system="metric",
        board_size=(SHEET_W, SHEET_H),
    )
    readback_errors = verify_nested_dxf(
        dxf_path,
        sheets_payload_from_result(result),
        gap=200.0,
    )
    payload = {
        "name": name,
        "parts": result.total_parts,
        "sheets": result.total_sheets,
        "yield_rate": round(result.yield_rate, 3),
        "geometry_errors": geometry_errors,
        "kerf_errors": kerf_errors,
        "kerf_metrics": estimate_kerf_material(result.sheets, 5.0),
        "readback_errors": readback_errors,
        "guillotine_failure_count": len(guillotine_failures),
        "guillotine_failure_samples": guillotine_failures[:20],
        "dxf": str(dxf_path),
    }
    report = OUTPUT_DIR / f"citizen_ST-08_{name}.json"
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parts = load_parts()
    for name, strategy, heuristic in (
        ("maxrects_587", "area", "area"),
        ("maxrects_585", "width", "short"),
    ):
        placements = pack_layout(
            parts,
            strategy=strategy,
            heuristic=heuristic,
        )
        payload = validate_candidate(name, placements)
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
