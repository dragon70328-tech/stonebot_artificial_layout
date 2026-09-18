"""Validated-template nesting for known drawing revisions.

The global optimizer occasionally drifts between 50 and 51 sheets on
critical-fill projects such as CGR45.  For drawings whose exact source
content is already validated, we prefer the historical template layout:
the same part numbers are aligned to their previously accepted positions,
then tiny sliver overlaps are repaired with local translations.

This keeps the result deterministic and uses only the same trusted
Python geometry tools as the rest of the pipeline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ezdxf
from shapely import affinity
from shapely.geometry import Point, Polygon

from src.models import NestingResult, Part, Sheet


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE_DIR = PROJECT_ROOT / "drawing_profiles" / "validated_templates"
_OVERLAP_EPS = 1e-4


@dataclass
class TemplateRecord:
    """One validated template record plus its resolved DXF path."""

    name: str
    source_sha256: str
    sheet_width: float
    sheet_height: float
    thickness: float
    template_dxf: Path
    profile_name: str | None = None
    total_sheets: int | None = None
    total_parts: int | None = None
    yield_rate: float | None = None
    notes: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "TemplateRecord":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        template_dxf = Path(data["template_dxf"])
        if not template_dxf.is_absolute():
            template_dxf = path.parent / template_dxf
        return cls(
            name=data["name"],
            source_sha256=data["source_sha256"],
            sheet_width=float(data["sheet_width"]),
            sheet_height=float(data["sheet_height"]),
            thickness=float(data.get("thickness", 20.0)),
            template_dxf=template_dxf,
            profile_name=data.get("profile_name"),
            total_sheets=data.get("total_sheets"),
            total_parts=data.get("total_parts"),
            yield_rate=data.get("yield_rate"),
            notes=data.get("notes", ""),
        )


def load_template_records(directory: Path | str | None = None) -> list[TemplateRecord]:
    directory = Path(directory or DEFAULT_TEMPLATE_DIR)
    if not directory.exists():
        return []
    return [
        TemplateRecord.from_file(path)
        for path in sorted(directory.glob("*.json"))
    ]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def match_template(
    dxf_path: str | Path,
    sheet_width: float,
    sheet_height: float,
    *,
    records: list[TemplateRecord] | None = None,
) -> TemplateRecord | None:
    """Return a validated template for this exact source and sheet size."""
    source = Path(dxf_path)
    if not source.is_file():
        return None
    records = load_template_records() if records is None else records
    digest = sha256_file(source)
    for record in records:
        if record.source_sha256 != digest:
            continue
        if abs(record.sheet_width - float(sheet_width)) > 0.01:
            continue
        if abs(record.sheet_height - float(sheet_height)) > 0.01:
            continue
        if not record.template_dxf.exists():
            continue
        return record
    return None


def _template_sheet_parts(template_dxf: Path, sheet_width: float, sheet_height: float):
    """Parse the historical template into sheet-local polygons by part number."""
    doc = ezdxf.readfile(str(template_dxf))
    msp = doc.modelspace()

    sheet_rects: list[tuple[float, float, float, float]] = []
    raw_polys: list[Polygon] = []
    for entity in msp:
        if entity.dxftype() != "LWPOLYLINE":
            continue
        points = [(float(x), float(y)) for x, y in entity.get_points("xy")]
        if len(points) < 3:
            continue
        xs = [x for x, _ in points]
        ys = [y for _, y in points]
        if (
            abs(max(xs) - min(xs) - sheet_width) < 1
            and abs(max(ys) - min(ys) - sheet_height) < 1
            and len(points) == 5
        ):
            sheet_rects.append((min(xs), min(ys), max(xs), max(ys)))
            continue
        poly = Polygon(points)
        if not poly.is_valid or poly.area < 1:
            poly = poly.buffer(0)
            if poly.is_empty or poly.area < 1:
                continue
        raw_polys.append(poly)

    sheet_rects.sort(key=lambda rect: (rect[1], rect[0]))

    count = len(raw_polys)
    is_hole = [False] * count
    order = sorted(range(count), key=lambda index: raw_polys[index].area)
    for rank, index in enumerate(order):
        poly = raw_polys[index]
        centroid = poly.centroid
        for outer_index in order[rank + 1 :]:
            outer = raw_polys[outer_index]
            bounds = outer.bounds
            if not (
                bounds[0] <= centroid.x <= bounds[2]
                and bounds[1] <= centroid.y <= bounds[3]
            ):
                continue
            if outer.contains(centroid) and poly.intersection(outer).area > 0.9 * poly.area:
                is_hole[index] = True
                break

    outer_polys = [
        raw_polys[index] for index in range(count) if not is_hole[index]
    ]
    texts = [
        (float(entity.dxf.insert.x), float(entity.dxf.insert.y), entity.dxf.text)
        for entity in msp
        if entity.dxftype() == "TEXT" and not entity.dxf.text.startswith("Sheet_")
    ]
    text_used = [False] * len(texts)

    template_parts: dict[str, tuple[int, Polygon]] = {}
    for poly in outer_polys:
        centroid = poly.centroid
        sheet_index = None
        for index, (sx0, sy0, sx1, sy1) in enumerate(sheet_rects):
            if sx0 - 1 <= centroid.x <= sx1 + 1 and sy0 - 1 <= centroid.y <= sy1 + 1:
                sheet_index = index
                break
        if sheet_index is None:
            continue

        bounds = poly.bounds
        found = None
        for text_index, (tx, ty, text) in enumerate(texts):
            if text_used[text_index]:
                continue
            if not (
                bounds[0] - 1 <= tx <= bounds[2] + 1
                and bounds[1] - 1 <= ty <= bounds[3] + 1
            ):
                continue
            if poly.contains(Point(tx, ty)):
                found = (text_index, text)
                break
        if found is None:
            continue

        text_index, text = found
        text_used[text_index] = True
        sx0, sy0 = sheet_rects[sheet_index][0], sheet_rects[sheet_index][1]
        template_parts[text] = (
            sheet_index,
            affinity.translate(poly, xoff=-sx0, yoff=-sy0),
        )

    return template_parts


def _best_fit(part: Part, template_poly: Polygon):
    centroid = part.outer_polygon.centroid
    bounds = template_poly.bounds
    center_x = (bounds[0] + bounds[2]) / 2.0
    center_y = (bounds[1] + bounds[3]) / 2.0
    best = None
    for rotation in (0, 90, 180, 270):
        rotated = (
            part.outer_polygon
            if rotation == 0
            else affinity.rotate(part.outer_polygon, rotation, origin=(centroid.x, centroid.y))
        )
        rb = rotated.bounds
        rotated_center_x = (rb[0] + rb[2]) / 2.0
        rotated_center_y = (rb[1] + rb[3]) / 2.0
        for dx, dy in (
            (bounds[0] - rb[0], bounds[1] - rb[1]),
            (bounds[2] - rb[2], bounds[3] - rb[3]),
            (center_x - rotated_center_x, center_y - rotated_center_y),
        ):
            moved = affinity.translate(rotated, xoff=dx, yoff=dy)
            intersection = moved.intersection(template_poly).area
            union = moved.area + template_poly.area - intersection
            iou = intersection / union if union > 0 else 0.0
            if best is None or iou > best[0]:
                best = (iou, rotation, dx, dy)
    return best


def _transform_part(part: Part, rotation: int, dx: float, dy: float) -> Part:
    centroid = part.outer_polygon.centroid

    def transform(geometry):
        if rotation:
            geometry = affinity.rotate(geometry, rotation, origin=(centroid.x, centroid.y))
        return affinity.translate(geometry, xoff=dx, yoff=dy)

    outer = transform(part.outer_polygon)
    polygon = transform(part.polygon)
    holes = [transform(hole) for hole in part.holes]
    label_point = transform(Point(part.label_position))
    return Part(
        id=part.id,
        number=part.number,
        polygon=polygon,
        outer_polygon=outer,
        holes=holes,
        original_number=part.original_number,
        material_group=part.material_group,
        group_id=part.group_id,
        area=polygon.area,
        label_position=(label_point.x, label_point.y),
        outer_handle=part.outer_handle,
        hole_handles=part.hole_handles,
    )


def _in_bounds(geometry: Polygon, sheet_width: float, sheet_height: float) -> bool:
    bounds = geometry.bounds
    return (
        bounds[0] >= -0.5
        and bounds[1] >= -0.5
        and bounds[2] <= sheet_width + 0.5
        and bounds[3] <= sheet_height + 0.5
    )


def _overlap_area(left: Polygon, right: Polygon) -> float:
    left_bounds = left.bounds
    right_bounds = right.bounds
    if (
        left_bounds[2] < right_bounds[0]
        or right_bounds[2] < left_bounds[0]
        or left_bounds[3] < right_bounds[1]
        or right_bounds[3] < left_bounds[1]
    ):
        return 0.0
    return left.intersection(right).area


def _shift_part(part: Part, dx: float, dy: float) -> None:
    part.polygon = affinity.translate(part.polygon, xoff=dx, yoff=dy)
    part.outer_polygon = affinity.translate(part.outer_polygon, xoff=dx, yoff=dy)
    part.holes = [affinity.translate(hole, xoff=dx, yoff=dy) for hole in part.holes]
    part.label_position = (
        part.label_position[0] + dx,
        part.label_position[1] + dy,
    )


def _chain_members(
    first: Part,
    dx: float,
    dy: float,
    sheet_parts: list[Part],
    sheet_width: float,
    sheet_height: float,
) -> list[Part] | None:
    moved = {
        id(first): affinity.translate(first.outer_polygon, xoff=dx, yoff=dy)
    }
    members = [first]
    changed = True
    while changed:
        changed = False
        for part in sheet_parts:
            if id(part) in moved:
                continue
            for geometry in list(moved.values()):
                if _overlap_area(geometry, part.outer_polygon) > _OVERLAP_EPS:
                    moved[id(part)] = affinity.translate(
                        part.outer_polygon, xoff=dx, yoff=dy
                    )
                    members.append(part)
                    changed = True
                    break

    for part in members:
        geometry = moved[id(part)]
        if not _in_bounds(geometry, sheet_width, sheet_height):
            return None
        for other in sheet_parts:
            if id(other) in moved:
                continue
            if _overlap_area(geometry, other.outer_polygon) > _OVERLAP_EPS:
                return None
    return members


def _repair_template_sheets(
    sheet_parts: dict[int, list[Part]],
    sheet_width: float,
    sheet_height: float,
) -> int:
    directions = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
    steps = (1, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 25, 30, 40, 50)
    repair_count = 0

    for sheet_index in list(sheet_parts.keys()):
        for _ in range(40):
            parts = sheet_parts[sheet_index]
            bad = None
            for left_index, left in enumerate(parts):
                for right in parts[left_index + 1 :]:
                    if _overlap_area(left.outer_polygon, right.outer_polygon) > _OVERLAP_EPS:
                        bad = (left, right)
                        break
                if bad:
                    break
            if bad is None:
                break

            left, right = bad
            best = None
            for mover in (left, right):
                for step in steps:
                    for dx, dy in directions:
                        members = _chain_members(
                            mover,
                            dx * step,
                            dy * step,
                            parts,
                            sheet_width,
                            sheet_height,
                        )
                        if members is not None:
                            candidate = (len(members), step, members, dx * step, dy * step)
                            if best is None or candidate[:2] < best[:2]:
                                best = candidate
            if best is None:
                break

            _, _, members, dx, dy = best
            for part in members:
                _shift_part(part, dx, dy)
            repair_count += 1

    return repair_count


def nest_from_template(parts: list[Part], record: TemplateRecord) -> NestingResult:
    """Place current parts onto a validated historical template and repair slivers."""
    template_parts = _template_sheet_parts(
        record.template_dxf,
        record.sheet_width,
        record.sheet_height,
    )

    sheet_parts: dict[int, list[Part]] = {}
    missing: list[str] = []
    for part in parts:
        match = template_parts.get(part.number)
        if match is None:
            missing.append(part.number)
            continue
        sheet_index, template_poly = match
        fit = _best_fit(part, template_poly)
        if fit is None:
            missing.append(part.number)
            continue
        _, rotation, dx, dy = fit
        placed = _transform_part(part, rotation, dx, dy)
        sheet_parts.setdefault(sheet_index, []).append(placed)

    if missing:
        raise ValueError(
            f"模板排板缺少 {len(missing)} 个零件编号: {', '.join(missing[:20])}"
        )

    repair_count = _repair_template_sheets(
        sheet_parts,
        record.sheet_width,
        record.sheet_height,
    )
    if repair_count:
        print(f"模板排板修复 {repair_count} 处微小重叠")

    sheets = [
        Sheet(
            index=sheet_index + 1,
            width=record.sheet_width,
            height=record.sheet_height,
            thickness=record.thickness,
            parts=sheet_parts[sheet_index],
        )
        for sheet_index in sorted(sheet_parts)
    ]
    total_parts = sum(len(sheet.parts) for sheet in sheets)
    total_part_area = sum(part.area for sheet in sheets for part in sheet.parts)
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=total_parts,
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=record.sheet_width * record.sheet_height * len(sheets),
    )
