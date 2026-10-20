"""按材料标签提取规格板（单元格归属），生成原位 DXF 与规格清单。

适用图纸：材料标签压在"单元格"下沿、零件画在标签正上方的下单图
（酒店 model-room 图就是这样：每格一个 `材料：ST-201`，格内 1~2 块板）。

用法::

    python scripts/extract_material_panels.py 酒店.dxf --material ST-201

判定规则：

1. **零件候选**：闭合 LWPOLYLINE/POLYLINE/CIRCLE，面积 ≤ ``max_part_area``
   （默认 10 m²）——更大的视为户型/房间外轮廓，不是零件；
2. **锚点**：材料标签的 x 落在零件的 x 跨度内，且零件在标签**正上方**
   （y 方向缝隙 0~700mm），取缝隙最小的一批（±150mm）；
3. **整格归属**：从锚点出发，把"同一行/同一列、与已有零件相距 ≤350mm"的
   零件并入同一格（同行的判据是 y 跨度重叠 ≥50%，同列是 x 跨度重叠 ≥50%）；
4. 每个材料标签对应一个单元格，单元格内的零件都算该材料。

输出（``--out`` 指定目录，默认 output/<时间戳>_<图纸名>_<材料>）::

    <图纸名>_<材料>_numbered_原位.dxf    OUTER/HOLES/NUMBERS/MATERIALS
    <图纸名>_<材料>_规格清单.csv/.json    材料 × 区域 分组、尺寸/数量/孔洞
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import ezdxf
from shapely.geometry import Point, Polygon

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.dxf_writer import write_numbered_parts_dxf  # noqa: E402
from src.numbering import assign_numbers  # noqa: E402
from src.spec_list import (  # noqa: E402
    build_spec_list,
    extract_annotations,
    write_spec_list_files,
)

MAX_PART_AREA = 10_000_000.0  # mm²
ANCHOR_GAP_MAX = 700.0        # 标签到锚点零件上沿的最大缝隙 mm
ANCHOR_TOL = 150.0            # 同批锚点的缝隙容差
CELL_GAP = 350.0              # 同格零件的最大缝隙 mm
OVERLAP_RATIO = 0.5           # 同行/同列的跨度重叠比


def _closed_polygons(doc) -> dict[str, Polygon]:
    """收集可作为零件的闭合图形（外轮廓用面积上限排除）。"""
    polygons: dict[str, Polygon] = {}
    for entity in doc.modelspace():
        handle = str(getattr(entity.dxf, "handle", "") or "")
        if not handle:
            continue
        dtype = entity.dxftype()
        if dtype in ("LWPOLYLINE", "POLYLINE"):
            if dtype == "LWPOLYLINE":
                points = [(float(p[0]), float(p[1])) for p in entity.get_points()]
                closed = bool(entity.closed)
            else:
                points = [(float(v.dxf.location.x), float(v.dxf.location.y))
                          for v in entity.vertices]
                closed = bool(getattr(entity, "is_closed", False))
            if len(points) < 3:
                continue
            if points[0] == points[-1]:
                points = points[:-1]
            elif not closed:
                continue
            polygon = Polygon(points)
        elif dtype == "CIRCLE":
            cx, cy, r = entity.dxf.center.x, entity.dxf.center.y, entity.dxf.radius
            from math import cos, pi, sin

            points = [(cx + r * cos(a), cy + r * sin(a))
                      for a in [i * 2 * pi / 64 for i in range(64)]]
            polygon = Polygon(points)
        else:
            continue
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.geom_type != "Polygon":
            continue
        if polygon.area <= 0.01 or polygon.area > MAX_PART_AREA:
            continue
        polygons[handle] = polygon
    return polygons


def _overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
    intersection = min(a1, b1) - max(a0, b0)
    if intersection <= 0:
        return 0.0
    span = min(a1 - a0, b1 - b0)
    return intersection / span if span > 0 else 0.0


def _anchor(
    point: Point,
    polygons: dict[str, Polygon],
) -> tuple[str, Polygon] | None:
    candidates = []
    for handle, polygon in polygons.items():
        min_x, min_y, max_x, max_y = polygon.bounds
        if not (min_x - 1.0 <= point.x <= max_x + 1.0):
            continue
        gap = min_y - point.y
        if 0.0 <= gap <= ANCHOR_GAP_MAX:
            candidates.append((gap, round(polygon.area, 3), handle, polygon))
    if not candidates:
        return None
    best_gap = min(candidate[0] for candidate in candidates)
    nearest = [
        candidate for candidate in candidates
        if candidate[0] <= best_gap + ANCHOR_TOL
    ]
    # 同一缝隙上有多个零件时，取面积最大的那个作为锚点（通常是大板）
    _gap, _area, handle, polygon = max(nearest, key=lambda item: item[1])
    return handle, polygon


def _cell(
    anchor_handle: str,
    polygons: dict[str, Polygon],
) -> list[str]:
    """从锚点出发，把同一单元格内的零件并进来。"""
    group = {anchor_handle}
    changed = True
    while changed:
        changed = False
        boxes = [polygons[handle].bounds for handle in group]
        gx0 = min(box[0] for box in boxes)
        gx1 = max(box[2] for box in boxes)
        gy0 = min(box[1] for box in boxes)
        gy1 = max(box[3] for box in boxes)
        for handle, polygon in polygons.items():
            if handle in group:
                continue
            min_x, min_y, max_x, max_y = polygon.bounds
            dx = max(gx0 - max_x, min_x - gx1, 0.0)
            dy = max(gy0 - max_y, min_y - gy1, 0.0)
            if max(dx, dy) > CELL_GAP:
                continue
            same_row = _overlap_ratio(gy0, gy1, min_y, max_y) >= OVERLAP_RATIO
            same_column = _overlap_ratio(gx0, gx1, min_x, max_x) >= OVERLAP_RATIO
            if same_row or same_column:
                group.add(handle)
                changed = True
    return sorted(group)


def extract(dxf_path: Path, material: str, out_dir: Path) -> dict:
    doc = ezdxf.readfile(str(dxf_path))
    polygons = _closed_polygons(doc)

    labels = []
    for entity in doc.modelspace():
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        text = (entity.dxf.text or "").strip()
        if material.casefold() in text.casefold():
            labels.append(Point(float(entity.dxf.insert.x), float(entity.dxf.insert.y)))
    if not labels:
        raise SystemExit(f"没有找到材料标签 {material!r}")

    claimed: dict[str, set[str]] = {}
    cells = 0
    for point in labels:
        found = _anchor(point, polygons)
        if found is None:
            continue
        anchor_handle, _polygon = found
        handles = _cell(anchor_handle, polygons)
        for handle in handles:
            claimed.setdefault(handle, set()).add((point.x, point.y))
        cells += 1

    conflicts = {handle: owners for handle, owners in claimed.items() if len(owners) > 1}
    parts_data = []
    for index, handle in enumerate(sorted(claimed)):
        polygon = polygons[handle]
        parts_data.append(
            {
                "polygon": polygon,
                "outer_polygon": polygon,
                "holes": [],
                "hole_handles": [],
                "centroid": (float(polygon.centroid.x), float(polygon.centroid.y)),
                "area": polygon.area,
                "original_number": None,
                "index": index,
                "outer_handle": handle,
                "layer": "",
                "source": "material_cell",
                "closure_gap": 0.0,
                "material_group": material,
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = dxf_path.stem
    parts = assign_numbers(parts_data, skip_unnumbered=False)
    numbered = out_dir / f"{stem}_{material}_numbered_原位.dxf"
    write_numbered_parts_dxf(parts, str(numbered))

    spec = build_spec_list(
        parts,
        annotations=extract_annotations(doc),
        source_name=dxf_path.name,
    )
    spec_json, spec_csv = write_spec_list_files(spec, out_dir, f"{stem}_{material}")

    return {
        "labels": len(labels),
        "cells": cells,
        "parts": len(parts_data),
        "conflicts": conflicts,
        "sizes": spec.get("totals", {}),
        "numbered": numbered,
        "spec_csv": spec_csv,
        "spec_json": spec_json,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", help="DXF 文件")
    parser.add_argument("--material", required=True, help="材料号，如 ST-201")
    parser.add_argument("--out", default=None, help="输出目录")
    args = parser.parse_args()

    dxf_path = Path(args.dxf)
    material = args.material.strip()
    out_dir = Path(args.out) if args.out else (
        ROOT / "output" / f"{datetime.now():%Y%m%d_%H%M%S}_{dxf_path.stem}_{material}"
    )
    result = extract(dxf_path, material, out_dir)
    print(f"材料标签 {result['labels']} 个，单元格 {result['cells']} 个，零件 {result['parts']} 件")
    if result["conflicts"]:
        print(f"警告：{len(result['conflicts'])} 个零件被多个标签认领："
              f"{list(result['conflicts'])[:5]}")
    print(f"原位 DXF：{result['numbered']}")
    print(f"规格清单：{result['spec_csv']}")
    print(f"           {result['spec_json']}")


if __name__ == "__main__":
    main()
