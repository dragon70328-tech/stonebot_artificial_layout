"""Material-based specification sheet generated from a finished in-place DXF."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf import bbox

from src.xlsx_writer import write_xlsx_sheets

MATERIALS: dict[str, dict[str, str | float]] = {
    "ST-101": {
        "name": "White",
        "color_code": "BQ3025",
        "thickness_mm": 20.0,
        "finish": "哑光20度",
    },
    "ST-102": {
        "name": "Grey",
        "color_code": "BQ3037",
        "thickness_mm": 20.0,
        "finish": "哑光20度",
    },
    "ST-103": {
        "name": "Beige",
        "color_code": "BQ4025",
        "thickness_mm": 20.0,
        "finish": "哑光20度",
    },
    "ST-104": {
        "name": "Brown",
        "color_code": "BQ4037",
        "thickness_mm": 20.0,
        "finish": "哑光20度",
    },
}
MATERIAL_RE = re.compile(r"ST-\d+")


@dataclass(frozen=True)
class ScheduledPart:
    floor: str
    number: str
    material_code: str
    material_name: str
    color_code: str
    thickness_mm: float
    finish: str
    irregular: bool
    raw_width_mm: float
    raw_height_mm: float
    spec_length_mm: int
    spec_width_mm: int


@dataclass
class MaterialSchedule:
    source_dxf: str
    parts: list[ScheduledPart]

    @property
    def summary_rows(self) -> list[dict]:
        groups: dict[tuple, dict] = {}
        for part in self.parts:
            key = (
                part.material_code,
                part.spec_length_mm,
                part.spec_width_mm,
                part.irregular,
            )
            entry = groups.setdefault(
                key,
                {
                    "material_code": part.material_code,
                    "material_name": part.material_name,
                    "color_code": part.color_code,
                    "thickness_mm": part.thickness_mm,
                    "finish": part.finish,
                    "spec_length_mm": part.spec_length_mm,
                    "spec_width_mm": part.spec_width_mm,
                    "irregular": part.irregular,
                    "count": 0,
                    "numbers": [],
                },
            )
            entry["count"] += 1
            entry["numbers"].append(part.number)
        return sorted(
            groups.values(),
            key=lambda row: (
                row["material_code"],
                row["irregular"],
                -row["spec_length_mm"],
                -row["spec_width_mm"],
            ),
        )

    @property
    def material_totals(self) -> list[dict]:
        groups: dict[str, dict] = {}
        for part in self.parts:
            entry = groups.setdefault(
                part.material_code,
                {
                    "material_code": part.material_code,
                    "material_name": part.material_name,
                    "color_code": part.color_code,
                    "thickness_mm": part.thickness_mm,
                    "finish": part.finish,
                    "total_count": 0,
                    "irregular_count": 0,
                },
            )
            entry["total_count"] += 1
            if part.irregular:
                entry["irregular_count"] += 1
        return sorted(groups.values(), key=lambda row: row["material_code"])


def _spec_size(value: float) -> int:
    return int(math.ceil(value - 1e-9))


def _group_material(group, group_name: str) -> str:
    for entity in group:
        if entity.dxftype() != "TEXT":
            continue
        if entity.dxf.layer != "MATERIALS":
            continue
        match = MATERIAL_RE.search(entity.dxf.text)
        if match:
            return match.group(0)
    raise ValueError(f"GROUP {group_name} 缺少材料编号")


def _group_outline(group, group_name: str):
    for entity in group:
        if (
            entity.dxftype() == "LWPOLYLINE"
            and entity.dxf.layer == "OUTER"
        ):
            return entity
    raise ValueError(f"GROUP {group_name} 缺少 OUTER 多段线")


def _is_irregular(group, outline) -> bool:
    if any(
        abs(float(point[4])) > 1e-9 for point in outline.get_points()
    ):
        return True
    return any(
        entity.dxftype() == "ARC"
        and entity.dxf.layer == "R450_BOUNDARY"
        for entity in group
    )


def build_material_schedule(path: str | Path) -> MaterialSchedule:
    source = Path(path).expanduser().resolve()
    doc = ezdxf.readfile(str(source))
    parts: list[ScheduledPart] = []
    for group_name, group in list(doc.groups):
        outline = _group_outline(group, group_name)
        material_code = _group_material(group, group_name)
        material = MATERIALS.get(material_code)
        if material is None:
            raise ValueError(f"GROUP {group_name} 使用未知材料 {material_code}")
        extents = bbox.extents([outline])
        raw_width = float(extents.extmax.x - extents.extmin.x)
        raw_height = float(extents.extmax.y - extents.extmin.y)
        length = _spec_size(max(raw_width, raw_height))
        width = _spec_size(min(raw_width, raw_height))
        number = group_name.removesuffix("-ST101")
        floor = number.split("-", 1)[0]
        parts.append(
            ScheduledPart(
                floor=floor,
                number=group_name,
                material_code=material_code,
                material_name=str(material["name"]),
                color_code=str(material["color_code"]),
                thickness_mm=float(material["thickness_mm"]),
                finish=str(material["finish"]),
                irregular=_is_irregular(group, outline),
                raw_width_mm=raw_width,
                raw_height_mm=raw_height,
                spec_length_mm=length,
                spec_width_mm=width,
            )
        )
    parts.sort(
        key=lambda part: (
            part.material_code,
            not part.irregular,
            -part.spec_length_mm,
            -part.spec_width_mm,
            part.number,
        )
    )
    return MaterialSchedule(source_dxf=str(source), parts=parts)


def write_material_schedule_xlsx(
    schedule: MaterialSchedule,
    path: str | Path,
) -> Path:
    summary_table: list[list[object]] = [
        [
            "材料编号",
            "材料名称",
            "颜色编号",
            "厚度(mm)",
            "表面处理",
            "规格长(mm)",
            "规格宽(mm)",
            "数量",
            "类型",
            "板号清单",
        ]
    ]
    for material_code in sorted({part.material_code for part in schedule.parts}):
        material_rows = [
            row
            for row in schedule.summary_rows
            if row["material_code"] == material_code
        ]
        for row in material_rows:
            summary_table.append(
                [
                    row["material_code"],
                    row["material_name"],
                    row["color_code"],
                    row["thickness_mm"],
                    row["finish"],
                    row["spec_length_mm"],
                    row["spec_width_mm"],
                    row["count"],
                    "异形包络" if row["irregular"] else "矩形",
                    "、".join(row["numbers"]),
                ]
            )
        summary_table.append(
            [
                material_code,
                material_rows[0]["material_name"] if material_rows else "",
                material_rows[0]["color_code"] if material_rows else "",
                material_rows[0]["thickness_mm"] if material_rows else 20.0,
                material_rows[0]["finish"] if material_rows else "",
                "",
                "",
                sum(row["count"] for row in material_rows),
                "材料小计",
                "",
            ]
        )
    summary_table.append(
        [
            "合计",
            "",
            "",
            "",
            "",
            "",
            "",
            len(schedule.parts),
            "",
            "",
        ]
    )

    material_table: list[list[object]] = [
        [
            "材料编号",
            "材料名称",
            "颜色编号",
            "厚度(mm)",
            "表面处理",
            "总数量",
            "异形数量",
        ]
    ]
    for row in schedule.material_totals:
        material_table.append(
            [
                row["material_code"],
                row["material_name"],
                row["color_code"],
                row["thickness_mm"],
                row["finish"],
                row["total_count"],
                row["irregular_count"],
            ]
        )
    material_table.append(
        [
            "合计",
            "",
            "",
            "",
            "",
            len(schedule.parts),
            sum(row["irregular_count"] for row in schedule.material_totals),
        ]
    )

    detail_table: list[list[object]] = [
        [
            "楼层",
            "板号",
            "材料编号",
            "材料名称",
            "类型",
            "实际包围宽(mm)",
            "实际包围高(mm)",
            "规格长(mm)",
            "规格宽(mm)",
        ]
    ]
    for part in schedule.parts:
        detail_table.append(
            [
                part.floor,
                part.number,
                part.material_code,
                part.material_name,
                "异形包络" if part.irregular else "矩形",
                round(part.raw_width_mm, 3),
                round(part.raw_height_mm, 3),
                part.spec_length_mm,
                part.spec_width_mm,
            ]
        )

    return write_xlsx_sheets(
        path,
        [
            ("规格板汇总", summary_table),
            ("材料总计", material_table),
            ("逐件明细", detail_table),
        ],
        decimal_columns=((4,), (4,), (6, 7)),
    )
