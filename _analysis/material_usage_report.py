# -*- coding: utf-8 -*-
"""按图纸列头的"材料：ST-x"标签统计各材料用量。

图纸布局约定（citizen.dxf 实测）：每个楼层占 x 方向的一列，列头同时给出
`L-XX LEVEL` 与 `材料：ST-x`；`ST-7/ST-8` 表示该列两种料都用。

用法：
  python -X utf8 _analysis/material_usage_report.py <dxf> <recognized.json> [输出目录]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ezdxf  # noqa: E402
from shapely import wkt  # noqa: E402

MATERIAL_PATTERN = re.compile(r"材料\s*[:：]\s*(?P<materials>[A-Za-z0-9\-/、,\s]+)")
LEVEL_PATTERN = re.compile(r"^(?P<level>L-\d+)\s*LEVEL$", re.IGNORECASE)


def _split_materials(value: str) -> list[str]:
    parts = re.split(r"[/、,]", value)
    return [part.strip().upper() for part in parts if part.strip()]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    dxf_path = Path(sys.argv[1])
    recognized_path = Path(sys.argv[2])
    out_dir = (
        Path(sys.argv[3])
        if len(sys.argv) > 3
        else PROJECT_ROOT / "output" / f"material_usage_{dxf_path.stem}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = ezdxf.readfile(str(dxf_path))
    level_marks: list[tuple[str, float]] = []
    material_marks: list[tuple[list[str], float, str]] = []
    for entity in doc.modelspace():
        if entity.dxftype() != "TEXT":
            continue
        text = (entity.dxf.text or "").strip()
        x = float(entity.dxf.insert.x)
        level = LEVEL_PATTERN.match(text)
        if level:
            level_marks.append((level.group("level").upper(), x))
            continue
        material = MATERIAL_PATTERN.search(text)
        if material:
            materials = _split_materials(material.group("materials"))
            if materials:
                material_marks.append((materials, x, text))

    level_marks.sort(key=lambda item: item[1])
    material_marks.sort(key=lambda item: item[1])
    if len(level_marks) != len(material_marks):
        print(
            f"警告：楼层标签 {len(level_marks)} 个 / 材料标签 "
            f"{len(material_marks)} 个，数量不一致，按 x 顺序尽量配对"
        )

    # 列 = [楼层标签 x, 下一个楼层标签 x)；材料标签与楼层标签按 x 顺序一一配对，
    # 并校验配对结果确实落在该列区间内。
    columns: list[dict] = []
    for index, (level, level_x) in enumerate(level_marks):
        next_x = (
            level_marks[index + 1][1]
            if index + 1 < len(level_marks)
            else float("inf")
        )
        materials: list[str] = []
        label = ""
        if index < len(material_marks):
            materials, material_x, label = material_marks[index]
            if not (level_x <= material_x < next_x):
                print(
                    f"警告：第 {index + 1} 列材料标签 x={material_x:.0f} "
                    f"不在 [{level_x:.0f}, {next_x:.0f}) 内"
                )
        columns.append(
            {
                "column": index + 1,
                "level": level,
                "x_min": level_x,
                "x_max": next_x,
                "materials": materials,
                "label": label,
                "panel_count": 0,
                "area_mm2": 0.0,
                "panel_min_x": None,
                "panel_max_x": None,
            }
        )

    recognized = json.loads(recognized_path.read_text(encoding="utf-8"))
    panels = recognized.get("panels") or []
    outside = 0
    for panel in panels:
        geometry = wkt.loads(panel["geometry_wkt"])
        centroid_x = float(geometry.centroid.x)
        min_x, _, max_x, _ = geometry.bounds
        target = None
        for column in columns:
            if column["x_min"] <= centroid_x < column["x_max"]:
                target = column
                break
        if target is None:
            outside += 1
            continue
        target["panel_count"] += 1
        target["area_mm2"] += float(panel.get("area") or geometry.area)
        target["panel_min_x"] = (
            min_x if target["panel_min_x"] is None
            else min(target["panel_min_x"], min_x)
        )
        target["panel_max_x"] = (
            max_x if target["panel_max_x"] is None
            else max(target["panel_max_x"], max_x)
        )

    # 校验：相邻列的零件 x 区间不得交叉（否则"按列归属"不成立）
    crossings = 0
    for left, right in zip(columns, columns[1:]):
        if left["panel_max_x"] is None or right["panel_min_x"] is None:
            continue
        if left["panel_max_x"] > right["panel_min_x"]:
            crossings += 1
            print(
                f"警告：{left['level']} 的零件最大 x={left['panel_max_x']:.0f} "
                f"超过 {right['level']} 的最小 x={right['panel_min_x']:.0f}"
            )

    single: dict[str, dict] = defaultdict(
        lambda: {"columns": 0, "panel_count": 0, "area_mm2": 0.0}
    )
    shared: dict[tuple[str, ...], dict] = defaultdict(
        lambda: {"columns": 0, "panel_count": 0, "area_mm2": 0.0}
    )
    for column in columns:
        materials = tuple(column["materials"])
        bucket = (
            single[materials[0]] if len(materials) == 1
            else shared[materials]
        )
        bucket["columns"] += 1
        bucket["panel_count"] += column["panel_count"]
        bucket["area_mm2"] += column["area_mm2"]

    print(f"图纸: {dxf_path.name}")
    print(f"识别面板: {len(panels)} 件；未能归列的零件: {outside} 件")
    print(f"列数: {len(columns)}（相邻列零件 x 区间交叉: {crossings} 处）")
    print()
    print("== 按材料汇总 ==")
    print(f"{'材料':<14}{'列数':>5}{'件数':>7}{'面积 m²':>12}")
    total_count = 0
    total_area = 0.0
    for material in sorted(single):
        item = single[material]
        total_count += item["panel_count"]
        total_area += item["area_mm2"]
        print(
            f"{material:<14}{item['columns']:>5}{item['panel_count']:>7}"
            f"{item['area_mm2'] / 1e6:>12.2f}"
        )
    for materials, item in sorted(shared.items()):
        total_count += item["panel_count"]
        total_area += item["area_mm2"]
        label = "/".join(materials)
        print(
            f"{label:<14}{item['columns']:>5}{item['panel_count']:>7}"
            f"{item['area_mm2'] / 1e6:>12.2f}   ← 该列两种料共用，未拆分"
        )
    print(
        f"{'合计':<14}{len(columns):>5}{total_count:>7}{total_area / 1e6:>12.2f}"
    )

    print()
    print("== 按列（楼层）明细 ==")
    print(f"{'#':>3}{'楼层':<10}{'材料':<12}{'件数':>6}{'面积 m²':>10}")
    for column in columns:
        print(
            f"{column['column']:>3}{column['level']:<10}"
            f"{'/'.join(column['materials']):<12}"
            f"{column['panel_count']:>6}"
            f"{column['area_mm2'] / 1e6:>10.2f}"
        )

    payload = {
        "dxf": str(dxf_path),
        "recognized": str(recognized_path),
        "panel_total": len(panels),
        "panels_outside_columns": outside,
        "column_crossings": crossings,
        "by_material": {
            material: {
                "columns": item["columns"],
                "panel_count": item["panel_count"],
                "area_m2": round(item["area_mm2"] / 1e6, 3),
                "shared_with": [],
            }
            for material, item in single.items()
        },
        "shared_materials": {
            "/".join(materials): {
                "columns": item["columns"],
                "panel_count": item["panel_count"],
                "area_m2": round(item["area_mm2"] / 1e6, 3),
            }
            for materials, item in shared.items()
        },
        "columns": [
            {
                "column": column["column"],
                "level": column["level"],
                "materials": column["materials"],
                "panel_count": column["panel_count"],
                "area_m2": round(column["area_mm2"] / 1e6, 3),
            }
            for column in columns
        ],
    }
    json_path = out_dir / f"{dxf_path.stem}_material_usage.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(f"已写入: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
