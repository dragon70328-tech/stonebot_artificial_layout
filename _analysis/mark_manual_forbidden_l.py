"""Mark forbidden L orientations and frame overruns in the manual DXF."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import Polygon


def is_l_shape(polygon: Polygon) -> bool:
    coords = list(polygon.exterior.coords)
    min_x, min_y, max_x, max_y = polygon.bounds
    box_area = (max_x - min_x) * (max_y - min_y)
    return (
        5 <= len(coords) <= 7
        and box_area > 0
        and polygon.area / box_area < 0.999
    )


def classify_l(polygon: Polygon) -> str | None:
    if not is_l_shape(polygon):
        return None
    min_x, min_y, max_x, max_y = polygon.bounds
    width = max_x - min_x
    height = max_y - min_y
    area = polygon.area
    if abs(area - 1_677_416.0) < 2000.0:
        # The paired L is allowed at 0/180, with width < height.
        return "L_PAIR_ALLOWED" if width < height else "L_PAIR_FORBIDDEN_90"
    if abs(area - 1_213_707.0) < 2000.0:
        # The source contour is tall/narrow; the required 90-degree rotation
        # produces a wide/short contour.
        return "LL_GROUP_ALLOWED" if width > height else "LL_GROUP_FORBIDDEN"
    if abs(area - 2_415_156.0) < 2000.0:
        # This member must rotate 90, producing a wide short contour.
        return "LL_GROUP_ALLOWED" if width > height else "LL_GROUP_FORBIDDEN"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()

    doc = ezdxf.readfile(str(args.source))
    msp = doc.modelspace()
    closed = [
        entity
        for entity in msp
        if entity.dxftype() == "LWPOLYLINE" and bool(entity.closed)
    ]
    sheet_frames: list[tuple[object, Polygon]] = []
    part_items: list[tuple[object, Polygon]] = []
    for entity in closed:
        polygon = Polygon(
            [(float(x), float(y)) for x, y in entity.get_points("xy")]
        )
        min_x, min_y, max_x, max_y = polygon.bounds
        width = max_x - min_x
        height = max_y - min_y
        if (
            abs(width - 3000.0) <= 1.0
            and abs(height - 2000.0) <= 1.0
        ):
            sheet_frames.append((entity, polygon))
        else:
            part_items.append((entity, polygon))

    for name, color in (
        ("L_ALLOWED", 3),
        ("L_FORBIDDEN_90", 1),
        ("L_FORBIDDEN_LL", 6),
        ("OUT_OF_FRAME", 30),
        ("MARKERS", 7),
        ("MARK_LEGEND", 7),
    ):
        if name not in doc.layers:
            doc.layers.add(name, color=color, linetype="CONTINUOUS")

    records: list[dict] = []
    counter = Counter()
    marker_entities = []
    for entity, polygon in part_items:
        owner = next(
            (
                (sheet_entity, sheet_polygon)
                for sheet_entity, sheet_polygon in sheet_frames
                if sheet_polygon.covers(polygon.centroid)
            ),
            None,
        )
        classification = classify_l(polygon)
        outside_area = 0.0
        outside_distance = 0.0
        if owner is not None:
            _sheet_entity, sheet_polygon = owner
            outside = polygon.difference(sheet_polygon)
            outside_area = float(outside.area)
            min_x, min_y, max_x, max_y = polygon.bounds
            sx0, sy0, sx1, sy1 = sheet_polygon.bounds
            outside_distance = max(
                sx0 - min_x,
                sy0 - min_y,
                max_x - sx1,
                max_y - sy1,
                0.0,
            )

        color = None
        layer = None
        markers = []
        if classification == "L_PAIR_ALLOWED":
            counter["l_allowed"] += 1
            layer, color = "L_ALLOWED", 3
        elif classification == "LL_GROUP_ALLOWED":
            counter["l_allowed"] += 1
            layer, color = "L_ALLOWED", 3
        elif classification == "L_PAIR_FORBIDDEN_90":
            counter["forbidden_l_pair"] += 1
            layer, color = "L_FORBIDDEN_90", 1
            markers.append("禁L-90/270")
        elif classification == "LL_GROUP_FORBIDDEN":
            counter["forbidden_ll_group"] += 1
            layer, color = "L_FORBIDDEN_LL", 6
            markers.append("禁L-L组未转90")

        if outside_distance > 0.05:
            counter["out_of_frame"] += 1
            counter["out_of_frame_board"] += 1
            if owner is not None:
                counter[f"board:{owner[0].dxf.handle}"] += 1
            if color is None:
                layer, color = "OUT_OF_FRAME", 30
            markers.append(f"越框{outside_distance:.2f}mm")

        if layer is not None:
            entity.dxf.layer = layer
            entity.dxf.color = color
            entity.dxf.linetype = "CONTINUOUS"
        if markers and owner is not None:
            board_entity, _board_polygon = owner
            point = polygon.representative_point()
            label = msp.add_text(
                "；".join(markers),
                height=55.0,
                dxfattribs={
                    "layer": "MARKERS",
                    "color": color,
                    "style": "Standard",
                },
            )
            label.set_placement(
                (float(point.x), float(point.y)),
                align=TextEntityAlignment.MIDDLE_CENTER,
            )
            marker_entities.append(
                {
                    "part_handle": str(entity.dxf.handle),
                    "board_handle": str(board_entity.dxf.handle),
                    "markers": markers,
                    "outside_distance_mm": round(outside_distance, 3),
                    "bbox": [round(float(value), 3) for value in polygon.bounds],
                }
            )
        records.append(
            {
                "part_handle": str(entity.dxf.handle),
                "board_handle": str(owner[0].dxf.handle) if owner else None,
                "classification": classification,
                "outside_area_mm2": round(outside_area, 3),
                "outside_distance_mm": round(outside_distance, 3),
            }
        )

    style = doc.styles.get("Standard")
    style.dxf.font = "SimSun.ttf"
    style.dxf.bigfont = ""
    drawing_box = bbox.extents(
        [item for item in msp if item.dxftype() != "TEXT"],
        fast=True,
    )
    x0 = float(drawing_box.extmax.x) + 1200.0
    y = float(drawing_box.extmax.y)
    title = msp.add_text(
        "人工排板：禁止方向 L 型及越框检查",
        height=220.0,
        dxfattribs={"layer": "MARK_LEGEND", "color": 7, "style": "Standard"},
    )
    title.set_placement((x0, y), align=TextEntityAlignment.BOTTOM_LEFT)
    y -= 350.0
    legend_rows = [
        (3, f"绿色：方向符合当前规则的 L 型（{counter['l_allowed']}件）"),
        (1, f"红色：L+矩形组误用90/270°（{counter['forbidden_l_pair']}件，禁止）"),
        (6, f"品红：L-L超2000组合未整组90°（{counter['forbidden_ll_group']}件，禁止）"),
        (30, f"橙色：局部越出板框0.25～3.18mm（{counter['out_of_frame']}件）"),
        (7, "说明：越框是单件轮廓局部伸出，不是多件合成长度超过大板。"),
    ]
    for color, text in legend_rows:
        msp.add_line(
            (x0, y),
            (x0 + 850.0, y),
            dxfattribs={
                "layer": "MARK_LEGEND",
                "color": color,
                "linetype": "CONTINUOUS",
            },
        )
        label = msp.add_text(
            text,
            height=125.0,
            dxfattribs={
                "layer": "MARK_LEGEND",
                "color": color,
                "style": "Standard",
            },
        )
        label.set_placement(
            (x0 + 1000.0, y),
            align=TextEntityAlignment.MIDDLE_LEFT,
        )
        y -= 190.0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    doc.header["$DWGCODEPAGE"] = "ANSI_936"
    doc.saveas(args.output, encoding="utf-8")
    summary = {
        "source": str(args.source.resolve()),
        "output": str(args.output),
        "sheet_count": len(sheet_frames),
        "part_count": len(part_items),
        "l_allowed": counter["l_allowed"],
        "forbidden_l_pair": counter["forbidden_l_pair"],
        "forbidden_ll_group": counter["forbidden_ll_group"],
        "forbidden_l_total": (
            counter["forbidden_l_pair"] + counter["forbidden_ll_group"]
        ),
        "out_of_frame": counter["out_of_frame"],
        "out_of_frame_boards": {
            key.split(":", 1)[1]: value
            for key, value in counter.items()
            if key.startswith("board:")
        },
        "out_of_frame_note": (
            "These are individual contours protruding 0.25-3.18 mm past "
            "their sheet frame, not a combined part length exceeding the sheet."
        ),
        "marked_items": marker_entities,
    }
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
