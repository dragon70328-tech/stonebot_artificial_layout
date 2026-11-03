"""Render the Fremont pre-nesting orientation check drawing."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment

from nest_fremont_st12_fixed_orientation import parse_parts


COLORS = {
    "ST2_ORIGINAL": 4,          # cyan
    "ST1_YELLOW_ORIGINAL": 40,  # yellow
    "ST1_L_PAIR_ORIGINAL": 3,   # green
    "ST1_L_GROUP_ROTATE": 6,    # magenta
    "ST1_OTHER_ROTATE": 1,      # red
    "ST4_IGNORED": 8,           # gray
}


def classify(record: dict) -> str:
    if record["material"] == "ST/2":
        return "ST2_ORIGINAL"
    if record["yellow"]:
        return "ST1_YELLOW_ORIGINAL"
    if record["frozen_by_l_pair"]:
        return "ST1_L_PAIR_ORIGINAL"
    if record["is_l"] and record["rotation_deg"] == -90.0:
        return "ST1_L_GROUP_ROTATE"
    return "ST1_OTHER_ROTATE"


def add_legend(
    doc: ezdxf.document.Drawing,
    counts: Counter[str],
) -> None:
    msp = doc.modelspace()
    if "ORIENT_LEGEND" not in doc.layers:
        doc.layers.add("ORIENT_LEGEND", color=7, linetype="CONTINUOUS")
    style = doc.styles.get("Standard")
    style.dxf.font = "SimSun.ttf"
    style.dxf.bigfont = ""

    model_box = bbox.extents(
        [entity for entity in msp if entity.dxftype() != "TEXT"],
        fast=True,
    )
    if not model_box.has_data:
        raise ValueError("source drawing has no usable extents")
    x0 = float(model_box.extmax.x) + 1200.0
    y = float(model_box.extmax.y)
    title_height = 220.0
    row_height = 180.0
    sample_length = 850.0
    text_gap = 180.0

    title = msp.add_text(
        "排板前朝向确认图（ST-1 + ST-2 共708件）",
        height=title_height,
        dxfattribs={
            "layer": "ORIENT_LEGEND",
            "color": 7,
            "style": "Standard",
        },
    )
    title.set_placement((x0, y), align=TextEntityAlignment.BOTTOM_LEFT)
    y -= title_height * 1.6

    rows = [
        (
            COLORS["ST2_ORIGINAL"],
            f"青色：ST-2 规格板，保持原向，不旋转（{counts['ST2_ORIGINAL']}件）",
        ),
        (
            COLORS["ST1_YELLOW_ORIGINAL"],
            f"黄色：ST-1 黄色实线，保持原向，不旋转（{counts['ST1_YELLOW_ORIGINAL']}件）",
        ),
        (
            COLORS["ST1_L_PAIR_ORIGINAL"],
            "绿色：ST-1 L型最长边≤2000mm，及同户型相邻矩形，保持原向"
            f"（{counts['ST1_L_PAIR_ORIGINAL']}件）",
        ),
        (
            COLORS["ST1_L_GROUP_ROTATE"],
            "品红：ST-1 L型组合中任一最长边>2000mm，整组顺时针转90°"
            f"（{counts['ST1_L_GROUP_ROTATE']}件）",
        ),
        (
            COLORS["ST1_OTHER_ROTATE"],
            f"红色：其他 ST-1 规格板，顺时针转90°（{counts['ST1_OTHER_ROTATE']}件）",
        ),
        (
            COLORS["ST4_IGNORED"],
            f"灰色：ST-4 规格板，不参与本次排板（{counts['ST4_IGNORED']}件）",
        ),
    ]
    for color, text in rows:
        msp.add_line(
            (x0, y),
            (x0 + sample_length, y),
            dxfattribs={
                "layer": "ORIENT_LEGEND",
                "color": color,
                "linetype": "CONTINUOUS",
            },
        )
        label = msp.add_text(
            text,
            height=row_height * 0.72,
            dxfattribs={
                "layer": "ORIENT_LEGEND",
                "color": color,
                "style": "Standard",
            },
        )
        label.set_placement(
            (x0 + sample_length + text_gap, y),
            align=TextEntityAlignment.MIDDLE_LEFT,
        )
        y -= row_height


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()

    parts, records, orientation_summary = parse_parts(
        args.source,
        3000.0,
        2000.0,
        args.manifest,
    )
    categories = {record["number"]: classify(record) for record in records}
    counts = Counter(categories.values())

    doc = ezdxf.readfile(str(args.source))
    msp = doc.modelspace()
    if "ORIENT_CHECK" not in doc.layers:
        doc.layers.add("ORIENT_CHECK", color=7, linetype="CONTINUOUS")

    changed = 0
    for record in records:
        entity = msp.entitydb[record["handle"]]
        category = categories[record["number"]]
        entity.dxf.layer = "ORIENT_CHECK"
        entity.dxf.color = COLORS[category]
        entity.dxf.linetype = "CONTINUOUS"
        changed += 1

    st4_handles = set()
    for entity in msp:
        if entity.dxftype() != "LWPOLYLINE" or not bool(entity.closed):
            continue
        handle = str(entity.dxf.handle)
        if handle in {record["handle"] for record in records}:
            continue
        if str(entity.dxf.get("layer", "")) == "COUNTERTOPS":
            entity.dxf.color = COLORS["ST4_IGNORED"]
            entity.dxf.linetype = "CONTINUOUS"
            st4_handles.add(handle)
    counts["ST4_IGNORED"] = len(st4_handles)

    add_legend(doc, counts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    doc.header["$DWGCODEPAGE"] = "ANSI_936"
    doc.saveas(args.output, encoding="utf-8")

    check = ezdxf.readfile(str(args.output))
    check_msp = check.modelspace()
    panel_entities = [check.entitydb[record["handle"]] for record in records]
    color_counts = Counter(
        (
            f"{record['material']}:{categories[record['number']]}",
            int(check.entitydb[record["handle"]].dxf.color),
        )
        for record in records
    )
    legend_texts = [
        str(entity.dxf.text)
        for entity in check_msp
        if entity.dxftype() == "TEXT"
        and str(entity.dxf.get("layer", "")) == "ORIENT_LEGEND"
    ]
    summary = {
        **orientation_summary,
        "preview_dxf": str(args.output),
        "colored_panel_count": changed,
        "color_counts": {
            category: counts.get(category, 0)
            for category in COLORS
        },
        "legend_texts": legend_texts,
        "validation": {
            "lwpolyline_count": sum(
                1 for entity in check_msp if entity.dxftype() == "LWPOLYLINE"
            ),
            "dimension_count": sum(
                1 for entity in check_msp if entity.dxftype() == "DIMENSION"
            ),
            "countertop_number_count": sum(
                1
                for entity in check_msp
                if entity.dxftype() == "TEXT"
                and str(entity.dxf.get("layer", "")) == "COUNTERTOP_NUMBER"
            ),
            "all_colored_panels_present": len(panel_entities) == 708,
            "legend_present": len(legend_texts) == 7,
        },
    }
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
