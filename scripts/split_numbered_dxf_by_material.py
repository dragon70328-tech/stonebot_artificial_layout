"""按材料拆分编号原位 DXF。

编号原位 DXF（OUTER 外框 + NUMBERS 编号 + MATERIALS 材料文本，材料文本
插入点落在所属零件内）按材料过滤，只保留指定材料的零件及其编号/材料文本，
生成新的原位 DXF，供排板 CLI 直接读取。

用法::

    python scripts/split_numbered_dxf_by_material.py 图纸.dxf --material ST-6
    python scripts/split_numbered_dxf_by_material.py 图纸.dxf --material ST-6 --out 输出.dxf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ezdxf
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree


def _part_polygons(doc) -> tuple[list[str], list[Polygon]]:
    """收集 OUTER 层闭合 LWPOLYLINE 零件。"""
    handles: list[str] = []
    polygons: list[Polygon] = []
    for entity in doc.modelspace():
        if entity.dxftype() != "LWPOLYLINE" or entity.dxf.layer != "OUTER":
            continue
        points = [(float(p[0]), float(p[1])) for p in entity.get_points()]
        if len(points) < 3:
            continue
        if points[0] == points[-1]:
            points = points[:-1]
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.geom_type != "Polygon":
            continue
        handles.append(str(entity.dxf.handle))
        polygons.append(polygon)
    return handles, polygons


def _assign_texts(doc, layer: str, polygons: list[Polygon], tree: STRtree) -> dict[str, str]:
    """把指定图层的 TEXT 按插入点归属到包含它的零件，返回 handle -> text。"""
    owner: dict[str, str] = {}
    for entity in doc.modelspace():
        if entity.dxftype() != "TEXT" or entity.dxf.layer != layer:
            continue
        point = Point(entity.dxf.insert.x, entity.dxf.insert.y)
        for idx in tree.query(point):
            if polygons[idx].contains(point):
                owner[str(idx)] = str(entity.dxf.text)
                break
    return owner


def main() -> int:
    parser = argparse.ArgumentParser(description="按材料拆分编号原位 DXF")
    parser.add_argument("dxf", help="编号原位 DXF 路径")
    parser.add_argument("--material", required=True, help="要保留的材料，如 ST-6")
    parser.add_argument("--out", default=None, help="输出 DXF 路径（默认 <原名>_<材料>.dxf）")
    args = parser.parse_args()

    wanted = args.material.strip().upper()
    doc = ezdxf.readfile(args.dxf)
    handles, polygons = _part_polygons(doc)
    if not polygons:
        print("错误：OUTER 层没有闭合零件。", file=sys.stderr)
        return 1
    tree = STRtree(polygons)

    material_of = _assign_texts(doc, "MATERIALS", polygons, tree)
    number_of = _assign_texts(doc, "NUMBERS", polygons, tree)

    available = sorted({text.upper() for text in material_of.values()})
    keep_idx = [i for i, poly in enumerate(polygons)
                if (material_of.get(str(i)) or "").upper() == wanted]
    if not keep_idx:
        print(f"错误：找不到材料 {wanted}。图中可用材料：{', '.join(available)}",
              file=sys.stderr)
        return 1
    keep_handles = {handles[i] for i in keep_idx}
    keep_numbers = {number_of.get(str(i)) for i in keep_idx}

    msp = doc.modelspace()
    removed = 0
    for entity in list(msp):
        dtype = entity.dxftype()
        layer = entity.dxf.layer
        if dtype == "LWPOLYLINE" and layer == "OUTER":
            if str(entity.dxf.handle) not in keep_handles:
                msp.delete_entity(entity)
                removed += 1
        elif dtype == "TEXT" and layer in ("NUMBERS", "MATERIALS"):
            if str(entity.dxf.text) not in keep_numbers and layer == "NUMBERS":
                msp.delete_entity(entity)
                removed += 1
            elif layer == "MATERIALS" and str(entity.dxf.text).strip().upper() != wanted:
                msp.delete_entity(entity)
                removed += 1
        elif dtype == "TEXT" and layer == "MATERIAL_HEADER":
            if wanted not in str(entity.dxf.text).upper():
                msp.delete_entity(entity)
                removed += 1

    out_path = Path(args.out) if args.out else (
        Path(args.dxf).with_name(f"{Path(args.dxf).stem}_{args.material}.dxf")
    )
    doc.saveas(str(out_path))

    area_m2 = sum(polygons[i].area for i in keep_idx) / 1e6
    numbers = [number_of.get(str(i)) for i in keep_idx]
    unique = len({n for n in numbers if n})
    print(f"材料筛选：只保留 {wanted} —— {len(keep_idx)} 件"
          f"（编号 {unique} 个唯一），合计 {area_m2:.2f} m²，删除其他实体 {removed} 个。")
    print(f"图中可用材料：{', '.join(available)}")
    print(f"输出: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
