"""材料统计：按材料 + 规格（长×宽×厚）汇总件数与面积，导出 Excel。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.material_thickness import DEFAULT_THICKNESS_MM
from src.xlsx_writer import write_xlsx


HEADERS = [
    "材料名称",
    "数量",
    "长(mm)",
    "宽(mm)",
    "厚(mm)",
    "单件面积(m²)",
    "小计面积(m²)",
    "编号清单",
]


def build_material_summary(parts: list) -> dict[str, Any]:
    """按 材料 + 长 × 宽 × 厚 归类统计。"""
    groups: dict[tuple, dict[str, Any]] = {}
    for part in parts:
        bounds = part.outer_polygon.bounds
        width = abs(bounds[2] - bounds[0])
        height = abs(bounds[3] - bounds[1])
        length = max(width, height)
        short = min(width, height)
        material = str(getattr(part, "material_group", "") or "").strip()
        if not material:
            material = "未标注材料"
        thickness = float(getattr(part, "thickness_mm", 0.0) or 0.0)
        if thickness <= 0:
            thickness = DEFAULT_THICKNESS_MM
        key = (
            material,
            round(length, 1),
            round(short, 1),
            round(thickness, 1),
        )
        entry = groups.get(key)
        if entry is None:
            entry = {
                "material": material,
                "length_mm": key[1],
                "width_mm": key[2],
                "thickness_mm": key[3],
                "count": 0,
                "area_mm2": 0.0,
                "numbers": [],
            }
            groups[key] = entry
        entry["count"] += 1
        entry["area_mm2"] += float(part.area)
        entry["numbers"].append(str(part.number))

    rows = []
    for entry in groups.values():
        area_m2 = entry["area_mm2"] / 1_000_000.0
        rows.append(
            {
                **entry,
                "unit_area_m2": round(
                    area_m2 / max(entry["count"], 1), 4
                ),
                "area_m2": round(area_m2, 4),
            }
        )
    rows.sort(
        key=lambda row: (
            row["material"],
            -row["area_m2"],
            -row["length_mm"],
        )
    )
    total_parts = sum(row["count"] for row in rows)
    total_area = round(sum(row["area_m2"] for row in rows), 4)
    return {
        "rows": rows,
        "total_parts": total_parts,
        "total_area_m2": total_area,
        "materials": sorted({row["material"] for row in rows}),
    }


def write_material_summary_xlsx(
    parts: list,
    path: str | Path,
) -> Path:
    summary = build_material_summary(parts)
    table: list[list[Any]] = [list(HEADERS)]
    for row in summary["rows"]:
        table.append(
            [
                row["material"],
                row["count"],
                row["length_mm"],
                row["width_mm"],
                row["thickness_mm"],
                row["unit_area_m2"],
                row["area_m2"],
                "、".join(row["numbers"]),
            ]
        )
    table.append(
        [
            "合计",
            summary["total_parts"],
            "",
            "",
            "",
            "",
            summary["total_area_m2"],
            "",
        ]
    )
    return write_xlsx(
        path,
        table,
        sheet_name="材料统计",
        decimal_columns=(6, 7),
    )
