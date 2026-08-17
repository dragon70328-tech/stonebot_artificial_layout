# -*- coding: utf-8 -*-
"""为东莞奥特莱斯 DXF 生成材料/形状分组检查图。

该脚本只做读图检查，不参与排板。输出一个 DXF：
- Model: 原位核对图，按材料着色并标注实例编号
- 分组检查: 按材料分组、形状为行、按数量排列的检查图
"""

from __future__ import annotations

import math
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment
from shapely import affinity
from shapely.geometry import Point, Polygon
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.dxf_reader import _entity_to_polygon


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DXF = r"C:\Users\drago\Desktop\临时文件\东莞奥特莱斯1.dxf"
OUTPUT_NAME = "东莞奥特莱斯1_编号检查.dxf"

MATERIAL_COLORS = {
    "01B": 1,   # red
    "02B": 2,   # yellow
    "03B": 3,   # green
    "04B": 4,   # cyan
    "05B": 6,   # magenta
    "UNKNOWN": 8,
    "CONFLICT": 30,
}

LABEL_RE = re.compile(r"^(\d{2}B)-?(\d+)$")


def parse_label(text: str):
    """从文本解析 (material, shape)，仅接受 01B/02B 这类材料编码。"""
    value = text.strip()
    match = LABEL_RE.match(value)
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def text_box_polygon(entity) -> Polygon | None:
    """返回 TEXT 实体的大致包围盒多边形。"""
    try:
        box = bbox.extents([entity])
        points = [(vertex.x, vertex.y) for vertex in box.rect_vertices()]
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        return polygon
    except Exception:
        return None


def overlap_ratio(polygon: Polygon, box: Polygon) -> float:
    """文字包围盒落入面板的比例。"""
    if box.is_empty or box.area <= 0:
        return 0.0
    return polygon.intersection(box).area / box.area


def edge_points(path, arc_segments: int = 32) -> list[tuple[float, float]]:
    """把 HATCH 的一条 EdgePath 转为折线点。"""
    points: list[tuple[float, float]] = []
    for edge in path.edges:
        if edge.EDGE_TYPE == "LineEdge":
            points.append((edge.start_point.x, edge.start_point.y))
            continue

        center = edge.center
        radius = edge.radius
        start = math.radians(edge.start_angle)
        end = math.radians(edge.end_angle)
        sweep = end - start
        if not edge.ccw:
            sweep = -sweep

        segment_count = max(4, arc_segments)
        for i in range(segment_count):
            angle = start + sweep * i / segment_count
            points.append((
                center.x + radius * math.cos(angle),
                center.y + radius * math.sin(angle),
            ))

    deduped: list[tuple[float, float]] = []
    for point in points:
        if not deduped:
            deduped.append(point)
            continue
        previous = deduped[-1]
        if abs(point[0] - previous[0]) > 1e-6 or abs(point[1] - previous[1]) > 1e-6:
            deduped.append(point)

    if deduped and (
        abs(deduped[0][0] - deduped[-1][0]) > 1e-6
        or abs(deduped[0][1] - deduped[-1][1]) > 1e-6
    ):
        deduped.append(deduped[0])

    return deduped


def polygon_from_paths(paths) -> tuple[Polygon, list[Polygon]] | None:
    """从 HATCH paths 生成带孔洞多边形，返回 (combined_poly, holes)。"""
    path_polys = []
    for path in paths:
        points = edge_points(path)
        if len(points) < 4:
            continue
        poly = Polygon(points)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty and poly.area > 0.01:
            path_polys.append(poly)

    if not path_polys:
        return None

    outer = max(path_polys, key=lambda p: p.area)
    holes = [p for p in path_polys if p is not outer]
    if holes:
        combined = outer.difference(__import__("shapely.ops", fromlist=["unary_union"]).unary_union(holes))
    else:
        combined = outer
    return combined, holes


def chain_lines_to_polygon(lines: list[tuple[tuple[float, float], tuple[float, float]]]) -> Polygon | None:
    """把首尾相连的 LINE 段拼成一个封闭多边形。"""
    if len(lines) < 3:
        return None
    start_map = {start: end for start, end in lines}
    points = [lines[0][0]]
    current = lines[0][1]
    for _ in range(len(lines)):
        points.append(current)
        if current in start_map:
            current = start_map[current]
        else:
            break
    if len(points) < 4:
        return None
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area <= 0.01:
        return None
    return poly


def read_panels(model) -> list[dict]:
    """优先读取 HATCH 边界；若没有 HATCH，则读取石材分缝 LWPOLYLINE 和 LINE。"""
    panels = []
    for entity in model:
        if entity.dxftype() != "HATCH":
            continue
        result = polygon_from_paths(entity.paths)
        if result is None:
            continue
        combined, holes = result
        panels.append({
            "poly": combined,
            "holes": holes,
            "centroid": combined.centroid,
            "bounds": combined.bounds,
        })

    if panels:
        return panels

    for entity in model:
        if entity.dxftype() != "LWPOLYLINE":
            continue
        if getattr(entity.dxf, "layer", "") != "石材分缝":
            continue
        poly = _entity_to_polygon(entity)
        if poly is None or poly.is_empty or poly.area <= 0.01:
            continue
        panels.append({
            "poly": poly,
            "holes": [],
            "centroid": poly.centroid,
            "bounds": poly.bounds,
        })

    line_segments = []
    for entity in model:
        if entity.dxftype() != "LINE":
            continue
        if getattr(entity.dxf, "layer", "") != "石材分缝":
            continue
        line_segments.append((
            (entity.dxf.start.x, entity.dxf.start.y),
            (entity.dxf.end.x, entity.dxf.end.y),
        ))
    line_poly = chain_lines_to_polygon(line_segments)
    if line_poly is not None:
        panels.append({
            "poly": line_poly,
            "holes": [],
            "centroid": line_poly.centroid,
            "bounds": line_poly.bounds,
        })

    return panels


def material_sort_key(material: str):
    if material in ("CONFLICT", "UNKNOWN"):
        return (999, material)
    match = re.match(r"(\d+)(.*)", material)
    if not match:
        return (998, material)
    return int(match.group(1)), match.group(2)


def group_sort_key(key):
    """排序键：材料优先，形状编号其次，冲突/未分类放最后。"""
    group_name, shape = key
    if group_name in ("CONFLICT", "UNKNOWN"):
        return (999, group_name, shape)
    return material_sort_key(group_name) + (shape,)


def ensure_layer(doc, name: str, color: int):
    if name in doc.layers:
        return doc.layers.get(name)
    return doc.layers.add(name, color=color, linetype="CONTINUOUS")


def add_polyline(space, polygon: Polygon, layer: str):
    coords = [(x, y) for x, y in polygon.exterior.coords]
    if len(coords) >= 3:
        space.add_lwpolyline(coords, dxfattribs={"layer": layer})
    for interior in polygon.interiors:
        hole_coords = [(x, y) for x, y in interior.coords]
        if len(hole_coords) >= 3:
            space.add_lwpolyline(hole_coords, dxfattribs={"layer": layer})


def add_text(space, text: str, position, height: float, layer: str):
    entity = space.add_text(
        text,
        dxfattribs={
            "height": height,
            "layer": layer,
        },
    )
    entity.set_placement(position, align=TextEntityAlignment.MIDDLE_CENTER)


def translate_polygon(polygon: Polygon, dx: float, dy: float) -> Polygon:
    return affinity.translate(polygon, xoff=dx, yoff=dy)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    source_path = Path(SOURCE_DXF)
    if not source_path.exists():
        print(f"错误：找不到源文件 {SOURCE_DXF}")
        return 1

    output_dir = PROJECT_ROOT / "output" / f"{datetime.now():%Y%m%d_%H%M%S}_东莞奥特莱斯1"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dxf = output_dir / OUTPUT_NAME
    summary_path = output_dir / "东莞奥特莱斯1_分组汇总.txt"

    doc = ezdxf.readfile(str(source_path))
    model = doc.modelspace()

    # 1. 读取面板边界：优先 HATCH，其次石材分缝 LWPOLYLINE/LINE。
    panels = read_panels(model)

    # 2. 收集石材文字 / 石材编号文本，并计算文字包围盒。
    text_items = []
    for entity in model:
        if entity.dxftype() != "TEXT":
            continue
        layer = getattr(entity.dxf, "layer", "")
        if layer not in ("石材文字", "石材编号", "石材箱号"):
            continue
        parsed = parse_label(entity.dxf.text.strip())
        if not parsed:
            continue
        box = text_box_polygon(entity)
        if box is None or box.is_empty:
            continue
        text_items.append({
            "layer": layer,
            "point": Point(entity.dxf.insert.x, entity.dxf.insert.y),
            "text": entity.dxf.text.strip(),
            "parsed": parsed,
            "box": box,
        })

    # 3. 编号归属：优先按编号文字插入点所在面板；若多个文字落入同一面板，取文字包围盒占比最大者。
    assigned_text_ids = set()
    for panel in panels:
        inside = [item for item in text_items if panel["poly"].contains(item["point"])]
        if inside:
            best = random.Random(0).choice(inside)
            assigned_text_ids.add(id(best))
            panel["status"] = "ASSIGNED"
            panel["material"], panel["shape"] = best["parsed"]
            panel["candidates"] = []
        else:
            panel["status"] = "UNKNOWN"
            panel["material"] = None
            panel["shape"] = None
            panel["candidates"] = []

    # 4. 对插入点不在任何面板内的情况，按文字包围盒落入面板的比例补配。
    for panel in panels:
        if panel["status"] == "ASSIGNED":
            continue
        best_item = None
        best_ratio = 0.0
        for item in text_items:
            if id(item) in assigned_text_ids:
                continue
            ratio = overlap_ratio(panel["poly"], item["box"])
            if ratio > best_ratio:
                best_ratio = ratio
                best_item = item
        if best_item is not None and best_ratio >= 0.5:
            assigned_text_ids.add(id(best_item))
            panel["status"] = "ASSIGNED"
            panel["material"], panel["shape"] = best_item["parsed"]

    # 4. 组内排序并生成实例编号。
    groups = defaultdict(list)
    for index, panel in enumerate(panels):
        if panel["status"] == "ASSIGNED":
            key = (panel["material"], panel["shape"])
        else:
            key = (panel["status"], 0)
        groups[key].append((index, panel))

    summary_lines = []
    for key in sorted(groups, key=group_sort_key):
        group_panels = groups[key]
        # 按原图位置从上到下、从左到右排序，便于和原位图对照。
        group_panels.sort(key=lambda item: (-item[1]["centroid"].y, item[1]["centroid"].x))
        for serial, (_, panel) in enumerate(group_panels, start=1):
            panel["serial"] = serial
            if panel["status"] == "ASSIGNED":
                panel["code"] = f"{panel['material']}-{panel['shape']}"
                panel["label"] = f"{panel['code']}-{serial:02d}"
                panel["color_material"] = panel["material"]
            elif panel["status"] == "CONFLICT":
                panel["code"] = "CONFLICT"
                candidate_text = "/".join(f"{m}-{s}" for m, s in panel["candidates"])
                panel["label"] = f"CONFLICT-{serial:02d} ({candidate_text})"
                panel["color_material"] = "CONFLICT"
            else:
                panel["code"] = "UNKNOWN"
                panel["label"] = f"UNKNOWN-{serial:02d}"
                panel["color_material"] = "UNKNOWN"

        # 汇总行在分组循环结束后统一生成，避免重复标题。

    summary_lines.insert(0, f"总面板数: {len(panels)}")
    summary_lines.insert(1, f"已分类: {sum(1 for p in panels if p['status'] == 'ASSIGNED')}")
    summary_lines.insert(2, f"冲突: {sum(1 for p in panels if p['status'] == 'CONFLICT')}")
    summary_lines.insert(3, f"未分类: {sum(1 for p in panels if p['status'] == 'UNKNOWN')}")
    summary_lines.append("")

    current_material = None
    for key in sorted(groups, key=group_sort_key):
        group_panels = groups[key]
        first = group_panels[0][1]
        if first["status"] == "ASSIGNED":
            material, shape = key
            if material != current_material:
                summary_lines.append(f"材料 {material}")
                current_material = material
            summary_lines.append(f"  {material}-{shape}: {len(group_panels)}")
        else:
            summary_lines.append(f"{key[0]}: {len(group_panels)}")

    # 5. 创建输出 DXF。
    out_doc = ezdxf.new("R2010", setup=True)
    out_doc.header["$INSUNITS"] = 4

    for material, color in MATERIAL_COLORS.items():
        ensure_layer(out_doc, material, color)
    ensure_layer(out_doc, "TITLE", 7)
    ensure_layer(out_doc, "CHECK", 8)

    inplace_space = out_doc.modelspace()
    for panel in panels:
        layer = panel["color_material"]
        add_polyline(inplace_space, panel["poly"], layer)
        add_text(
            inplace_space,
            panel["label"],
            (panel["centroid"].x, panel["centroid"].y),
            40.0,
            layer,
        )

    # 6. 分组检查图，放在独立布局中。
    catalog = out_doc.layouts.new("分组检查")
    y_cursor = 0.0
    row_gap = 650.0
    material_gap = 1400.0
    title_height = 320.0
    row_header_height = 200.0
    instance_text_height = 40.0

    for key in sorted(groups, key=group_sort_key):
        group_panels = groups[key]
        first = group_panels[0][1]
        section_name = (
            f"材料 {key[0]}"
            if first["status"] == "ASSIGNED"
            else f"{key[0]} 面板"
        )
        add_text(catalog, section_name, (0, y_cursor), title_height, "TITLE")
        y_cursor -= 700

        if first["status"] == "ASSIGNED":
            add_text(
                catalog,
                f"{first['code']}  数量 {len(group_panels)}",
                (0, y_cursor),
                row_header_height,
                first["material"],
            )
            y_cursor -= 400

            current_x = 3500.0
            max_height = 0.0
            for _, panel in group_panels:
                minx, miny, maxx, maxy = panel["bounds"]
                width = maxx - minx
                height = maxy - miny
                dx = current_x - minx
                dy = y_cursor - miny
                shifted = translate_polygon(panel["poly"], dx, dy)
                add_polyline(catalog, shifted, panel["material"])
                centroid = shifted.centroid
                add_text(
                    catalog,
                    panel["label"],
                    (centroid.x, centroid.y),
                    instance_text_height,
                    panel["material"],
                )
                current_x += width + 120.0
                max_height = max(max_height, height)
            y_cursor -= max_height + row_gap
        else:
            # 冲突/未分类逐块列出，便于人工核对。
            current_x = 3500.0
            max_height = 0.0
            for _, panel in group_panels:
                minx, miny, maxx, maxy = panel["bounds"]
                width = maxx - minx
                height = maxy - miny
                dx = current_x - minx
                dy = y_cursor - miny
                shifted = translate_polygon(panel["poly"], dx, dy)
                add_polyline(catalog, shifted, panel["color_material"])
                add_text(
                    catalog,
                    panel["label"],
                    (shifted.centroid.x, shifted.centroid.y),
                    instance_text_height,
                    panel["color_material"],
                )
                current_x += width + 120.0
                max_height = max(max_height, height)
            y_cursor -= max_height + row_gap

        y_cursor -= material_gap

    out_doc.saveas(output_dxf)
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    print(f"输出 DXF: {output_dxf}")
    print(f"汇总文件: {summary_path}")
    print(f"面板总数: {len(panels)}")
    print(f"已分类: {sum(1 for p in panels if p['status'] == 'ASSIGNED')}")
    print(f"冲突: {sum(1 for p in panels if p['status'] == 'CONFLICT')}")
    print(f"未分类: {sum(1 for p in panels if p['status'] == 'UNKNOWN')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
