"""Annotate ST-8 panels present in the source DXF but absent from the 2018 file.

The comparison is geometry based.  It first aligns the ordered panel geometry
signatures of the source with the OUTER entities of the numbered DXF.  The
aligned source gaps identify the extra panels without relying on either file's
numbering convention.
"""

from __future__ import annotations

import json
import os
import sys
from array import array
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Polygon

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.workflow import prepare_drawing


SOURCE = Path(
    r"C:\Users\drago\iCloudDrive\Desktop\石英石\citizen"
    r"\citizen_ST-08.dxf"
)
NUMBERED = Path(
    r"C:\Users\drago\iCloudDrive\Desktop\石英石\citizen"
    r"\citizen_ST-08_全部2018_numbered_原位.dxf"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "st8_extra14_annotation"

OUTLINE_LAYER = "ST8_EXTRA14_OUTLINE"
LABEL_LAYER = "ST8_EXTRA14_LABEL"
FRAME_LAYER = "ST8_EXTRA14_FRAME"


def geometry_signature(polygon: Polygon) -> tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = polygon.bounds
    return (
        round(polygon.area, 3),
        round(max_x - min_x, 3),
        round(max_y - min_y, 3),
        round(polygon.length, 3),
    )


def numbered_geometry(path: Path) -> list[tuple[float, float, float, float]]:
    doc = ezdxf.readfile(path)
    signatures = []
    for entity in doc.modelspace():
        if str(entity.dxf.layer).upper() != "OUTER":
            continue
        polygon = Polygon(
            [(float(x), float(y)) for x, y in entity.get_points("xy")]
        )
        signatures.append(geometry_signature(polygon))
    return signatures


def extra_source_indices(
    source_signatures: list[tuple[float, float, float, float]],
    numbered_signatures: list[tuple[float, float, float, float]],
) -> list[int]:
    """Return source indices that are insertions relative to the numbered file."""
    n = len(numbered_signatures)
    previous = array("H", [0]) * (n + 1)
    rows = [previous]
    for source_sig in source_signatures:
        current = array("H", [0]) * (n + 1)
        for index, numbered_sig in enumerate(numbered_signatures, start=1):
            if source_sig == numbered_sig:
                current[index] = previous[index - 1] + 1
            else:
                current[index] = max(previous[index], current[index - 1])
        rows.append(current)
        previous = current

    matched_source = set()
    source_index = len(source_signatures)
    numbered_index = n
    while source_index and numbered_index:
        if (
            source_signatures[source_index - 1]
            == numbered_signatures[numbered_index - 1]
        ):
            matched_source.add(source_index - 1)
            source_index -= 1
            numbered_index -= 1
        elif rows[source_index - 1][numbered_index] >= rows[source_index][
            numbered_index - 1
        ]:
            source_index -= 1
        else:
            numbered_index -= 1

    return sorted(set(range(len(source_signatures))) - matched_source)


def add_layers(doc) -> None:
    doc.layers.add(OUTLINE_LAYER, color=1, lineweight=60)
    doc.layers.add(LABEL_LAYER, color=1)
    doc.layers.add(FRAME_LAYER, color=1)


def polygon_points(part) -> list[tuple[float, float]]:
    return [
        (float(x), float(y))
        for x, y in list(part.outer_polygon.exterior.coords)[:-1]
    ]


def add_highlights(modelspace, parts, *, include_frame: bool) -> None:
    for index, part in enumerate(parts, start=1):
        points = polygon_points(part)
        modelspace.add_lwpolyline(
            points,
            close=True,
            dxfattribs={
                "layer": OUTLINE_LAYER,
                "color": 1,
                "lineweight": 60,
            },
        )
        centroid = part.outer_polygon.centroid
        text = modelspace.add_text(
            f"A{index:02d}  {part.number}",
            height=100.0,
            dxfattribs={
                "layer": LABEL_LAYER,
                "color": 1,
            },
        )
        text.set_placement(
            (float(centroid.x), float(centroid.y)),
            align=TextEntityAlignment.MIDDLE_CENTER,
        )

    if include_frame:
        min_x = min(part.outer_polygon.bounds[0] for part in parts)
        min_y = min(part.outer_polygon.bounds[1] for part in parts)
        max_x = max(part.outer_polygon.bounds[2] for part in parts)
        max_y = max(part.outer_polygon.bounds[3] for part in parts)
        margin = 250.0
        modelspace.add_lwpolyline(
            [
                (min_x - margin, min_y - margin),
                (max_x + margin, min_y - margin),
                (max_x + margin, max_y + margin),
                (min_x - margin, max_y + margin),
            ],
            close=True,
            dxfattribs={
                "layer": FRAME_LAYER,
                "color": 1,
                "lineweight": 35,
            },
        )
        title = modelspace.add_text(
            "ST-8: 14 panels absent from the 2018 numbered DXF (L-49)",
            height=220.0,
            dxfattribs={"layer": LABEL_LAYER, "color": 1},
        )
        title.set_placement(
            (min_x - margin, max_y + margin + 400.0),
            align=TextEntityAlignment.BOTTOM_LEFT,
        )


def set_modelspace_extents(doc) -> None:
    entities = list(doc.modelspace())
    if not entities:
        return
    extents = bbox.extents(entities)
    doc.header["$EXTMIN"] = (
        float(extents.extmin.x),
        float(extents.extmin.y),
        0.0,
    )
    doc.header["$EXTMAX"] = (
        float(extents.extmax.x),
        float(extents.extmax.y),
        0.0,
    )
    doc.header["$LIMMIN"] = (
        float(extents.extmin.x),
        float(extents.extmin.y),
    )
    doc.header["$LIMMAX"] = (
        float(extents.extmax.x),
        float(extents.extmax.y),
    )
    center = (
        (float(extents.extmin.x) + float(extents.extmax.x)) / 2.0,
        (float(extents.extmin.y) + float(extents.extmax.y)) / 2.0,
    )
    height = max(
        float(extents.extmax.y) - float(extents.extmin.y),
        float(extents.extmax.x) - float(extents.extmin.x),
        1.0,
    )
    doc.set_modelspace_vport(height * 1.15, center=center)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "arialbd.ttf" if bold else "arial.ttf"
    path = Path(r"C:\Windows\Fonts") / name
    if path.exists():
        return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def write_preview_png(
    parts,
    records: list[dict],
    output_path: Path,
) -> Path:
    """Write a spatial overview plus a large readable grid of the 14 panels."""
    canvas = Image.new("RGB", (2000, 1400), "white")
    draw = ImageDraw.Draw(canvas)
    title_font = _font(36, bold=True)
    heading_font = _font(24, bold=True)
    body_font = _font(19)
    small_font = _font(15)

    draw.text(
        (40, 24),
        "ST-8: 14 panels in the source DXF but absent from the 2018 numbered DXF",
        fill="#111827",
        font=title_font,
    )
    draw.text(
        (40, 72),
        "All 14 are in L-49; red rectangles preserve their original positions.",
        fill="#4b5563",
        font=body_font,
    )

    left_box = (40, 120, 620, 1350)
    draw.rounded_rectangle(left_box, radius=8, outline="#d1d5db", width=2)
    draw.text(
        (60, 136),
        "Original-position overview",
        fill="#111827",
        font=heading_font,
    )

    min_x = min(part.outer_polygon.bounds[0] for part in parts)
    min_y = min(part.outer_polygon.bounds[1] for part in parts)
    max_x = max(part.outer_polygon.bounds[2] for part in parts)
    max_y = max(part.outer_polygon.bounds[3] for part in parts)
    draw_x0, draw_y0, draw_x1, draw_y1 = 75, 190, 585, 1320
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    scale = min((draw_x1 - draw_x0) / span_x, (draw_y1 - draw_y0) / span_y)
    offset_x = draw_x0 + ((draw_x1 - draw_x0) - span_x * scale) / 2.0
    offset_y = draw_y0 + ((draw_y1 - draw_y0) - span_y * scale) / 2.0

    def world_to_preview(x: float, y: float) -> tuple[float, float]:
        return (
            offset_x + (x - min_x) * scale,
            offset_y + (max_y - y) * scale,
        )

    for index, part in enumerate(parts, start=1):
        bounds = part.outer_polygon.bounds
        x0, y1 = world_to_preview(bounds[0], bounds[1])
        x1, y0 = world_to_preview(bounds[2], bounds[3])
        draw.rectangle(
            (x0, y0, max(x1, x0 + 2), max(y1, y0 + 2)),
            fill="#fecaca",
            outline="#dc2626",
            width=2,
        )
        draw.text(
            (x0 + 2, y0 - 15),
            f"A{index:02d}",
            fill="#991b1b",
            font=small_font,
        )

    grid_left = 660
    grid_top = 120
    cell_w = 320
    cell_h = 300
    shape_left = 25
    shape_top = 55
    shape_w = cell_w - 50
    shape_h = 150
    for index, (part, record) in enumerate(
        zip(parts, records),
        start=1,
    ):
        column = (index - 1) % 4
        row = (index - 1) // 4
        x = grid_left + column * cell_w
        y = grid_top + row * cell_h
        draw.rounded_rectangle(
            (x, y, x + cell_w - 12, y + cell_h - 12),
            radius=8,
            outline="#d1d5db",
            fill="#fafafa",
            width=2,
        )
        width = record["width_mm"]
        height = record["height_mm"]
        shape_scale = min(shape_w / max(width, 1.0), shape_h / max(height, 1.0))
        rendered_w = width * shape_scale
        rendered_h = height * shape_scale
        left = x + shape_left + (shape_w - rendered_w) / 2.0
        top = y + shape_top + (shape_h - rendered_h) / 2.0
        draw.rectangle(
            (left, top, left + rendered_w, top + rendered_h),
            fill="#fee2e2",
            outline="#dc2626",
            width=3,
        )
        draw.text(
            (x + 16, y + 12),
            f"A{index:02d}  {part.number}",
            fill="#991b1b",
            font=heading_font,
        )
        draw.text(
            (x + 16, y + 223),
            f"{width:.2f} x {height:.2f} mm",
            fill="#374151",
            font=body_font,
        )
        draw.text(
            (x + 16, y + 250),
            f"Area {part.area / 1e6:.3f} m2  |  handle {part.outer_handle}",
            fill="#6b7280",
            font=small_font,
        )

    temporary = output_path.with_name(output_path.stem + ".tmp.png")
    canvas.save(temporary, format="PNG", optimize=True)
    try:
        os.replace(temporary, output_path)
        return output_path
    except OSError:
        fallback = output_path.with_name(output_path.stem + "_new.png")
        os.replace(temporary, fallback)
        return fallback


def write_l49_context_png(
    all_parts,
    extra_parts,
    output_path: Path,
) -> Path:
    l49_parts = [
        part for part in all_parts if str(part.number).startswith("49")
    ]
    extra_ids = {int(part.id) for part in extra_parts}
    canvas = Image.new("RGB", (1500, 2300), "white")
    draw = ImageDraw.Draw(canvas)
    title_font = _font(34, bold=True)
    body_font = _font(20)
    label_font = _font(15)
    draw.text(
        (38, 24),
        "L-49 comparison: red = absent from 2018 file; gray = retained",
        fill="#111827",
        font=title_font,
    )
    draw.text(
        (38, 70),
        "All 59 recognized L-49 panels are shown in their original positions.",
        fill="#4b5563",
        font=body_font,
    )
    draw.rectangle((40, 115, 1460, 2240), outline="#d1d5db", width=2)

    min_x = min(part.outer_polygon.bounds[0] for part in l49_parts)
    min_y = min(part.outer_polygon.bounds[1] for part in l49_parts)
    max_x = max(part.outer_polygon.bounds[2] for part in l49_parts)
    max_y = max(part.outer_polygon.bounds[3] for part in l49_parts)
    draw_x0, draw_y0, draw_x1, draw_y1 = 75, 160, 1420, 2180
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    scale = min((draw_x1 - draw_x0) / span_x, (draw_y1 - draw_y0) / span_y)
    offset_x = draw_x0 + ((draw_x1 - draw_x0) - span_x * scale) / 2.0
    offset_y = draw_y0 + ((draw_y1 - draw_y0) - span_y * scale) / 2.0

    def world_to_image(x: float, y: float) -> tuple[float, float]:
        return (
            offset_x + (x - min_x) * scale,
            offset_y + (max_y - y) * scale,
        )

    annotation_by_id = {
        int(part.id): f"A{index:02d}"
        for index, part in enumerate(extra_parts, start=1)
    }
    for part in l49_parts:
        bounds = part.outer_polygon.bounds
        x0, y1 = world_to_image(bounds[0], bounds[1])
        x1, y0 = world_to_image(bounds[2], bounds[3])
        is_extra = int(part.id) in extra_ids
        draw.rectangle(
            (x0, y0, max(x1, x0 + 2), max(y1, y0 + 2)),
            fill="#fecaca" if is_extra else "#e5e7eb",
            outline="#dc2626" if is_extra else "#6b7280",
            width=3 if is_extra else 1,
        )
        label = annotation_by_id.get(int(part.id), part.number)
        draw.text(
            (x0 + 2, y0 - 16),
            label,
            fill="#991b1b" if is_extra else "#374151",
            font=label_font,
        )

    temporary = output_path.with_name(output_path.stem + ".tmp.png")
    canvas.save(temporary, format="PNG", optimize=True)
    try:
        os.replace(temporary, output_path)
        return output_path
    except OSError:
        fallback = output_path.with_name(output_path.stem + "_new.png")
        os.replace(temporary, fallback)
        return fallback


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    prepared = prepare_drawing(
        str(SOURCE),
        use_case_library=False,
        skip_unnumbered=False,
    )
    if prepared.error:
        raise RuntimeError(prepared.error)

    source_signatures = [
        geometry_signature(part.outer_polygon) for part in prepared.parts
    ]
    numbered_signatures = numbered_geometry(NUMBERED)
    extra_indices = extra_source_indices(
        source_signatures,
        numbered_signatures,
    )
    extra_parts = [prepared.parts[index] for index in extra_indices]
    if len(extra_parts) != 14:
        raise RuntimeError(
            f"Expected 14 extra panels, found {len(extra_parts)}"
        )

    records = []
    for annotation_index, (source_index, part) in enumerate(
        zip(extra_indices, extra_parts),
        start=1,
    ):
        min_x, min_y, max_x, max_y = part.outer_polygon.bounds
        records.append(
            {
                "annotation_id": f"A{annotation_index:02d}",
                "source_order_index": source_index,
                "number": part.number,
                "handle": part.outer_handle,
                "area_mm2": round(part.area, 3),
                "width_mm": round(max_x - min_x, 3),
                "height_mm": round(max_y - min_y, 3),
                "bounds": [
                    round(min_x, 3),
                    round(min_y, 3),
                    round(max_x, 3),
                    round(max_y, 3),
                ],
            }
        )

    full_doc = ezdxf.readfile(SOURCE)
    add_layers(full_doc)
    add_highlights(full_doc.modelspace(), extra_parts, include_frame=True)
    set_modelspace_extents(full_doc)
    full_path = OUTPUT_DIR / "citizen_ST-08_extra14_annotated.dxf"
    full_doc.saveas(full_path)

    full_doc.dxfversion = "AC1027"
    full_r2013_path = (
        OUTPUT_DIR / "citizen_ST-08_extra14_annotated_R2013.dxf"
    )
    full_doc.saveas(full_r2013_path)

    local_doc = ezdxf.new("R2018", setup=True, units=4)
    add_layers(local_doc)
    add_highlights(local_doc.modelspace(), extra_parts, include_frame=True)
    set_modelspace_extents(local_doc)
    local_path = OUTPUT_DIR / "citizen_ST-08_extra14_only.dxf"
    local_doc.saveas(local_path)

    local_r2013_doc = ezdxf.new("R2013", setup=True, units=4)
    add_layers(local_r2013_doc)
    add_highlights(
        local_r2013_doc.modelspace(),
        extra_parts,
        include_frame=True,
    )
    set_modelspace_extents(local_r2013_doc)
    local_r2013_path = OUTPUT_DIR / "citizen_ST-08_extra14_only_R2013.dxf"
    local_r2013_doc.saveas(local_r2013_path)

    local_r2000_doc = ezdxf.new("R2000", setup=True, units=4)
    add_layers(local_r2000_doc)
    add_highlights(
        local_r2000_doc.modelspace(),
        extra_parts,
        include_frame=True,
    )
    set_modelspace_extents(local_r2000_doc)
    local_r2000_path = OUTPUT_DIR / "citizen_ST-08_extra14_only_R2000.dxf"
    local_r2000_doc.saveas(local_r2000_path)

    preview_path = write_preview_png(
        extra_parts,
        records,
        OUTPUT_DIR / "citizen_ST-08_extra14_preview.png",
    )
    context_preview_path = write_l49_context_png(
        prepared.parts,
        extra_parts,
        OUTPUT_DIR / "citizen_ST-08_L49_context.png",
    )

    manifest = {
        "source_dxf": str(SOURCE),
        "numbered_dxf": str(NUMBERED),
        "source_panel_count": len(prepared.parts),
        "numbered_panel_count": len(numbered_signatures),
        "extra_count": len(extra_parts),
        "extra_area_m2": round(sum(part.area for part in extra_parts) / 1e6, 6),
        "shape_signature_counts": {
            str(key): value
            for key, value in sorted(
                Counter(
                    geometry_signature(part.outer_polygon)
                    for part in extra_parts
                ).items()
            )
        },
        "panels": records,
        "outputs": {
            "annotated_dxf": str(full_path),
            "annotated_r2013_dxf": str(full_r2013_path),
            "extra_only_dxf": str(local_path),
            "extra_only_r2013_dxf": str(local_r2013_path),
            "extra_only_r2000_dxf": str(local_r2000_path),
            "preview_png": str(preview_path),
            "l49_context_png": str(context_preview_path),
        },
    }
    manifest_path = OUTPUT_DIR / "citizen_ST-08_extra14_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(records, ensure_ascii=False, indent=2))
    print(f"Annotated DXF: {full_path}")
    print(f"Annotated R2013 DXF: {full_r2013_path}")
    print(f"Extra-only DXF: {local_path}")
    print(f"Extra-only R2013 DXF: {local_r2013_path}")
    print(f"Extra-only R2000 DXF: {local_r2000_path}")
    print(f"Preview PNG: {preview_path}")
    print(f"L-49 context PNG: {context_preview_path}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
