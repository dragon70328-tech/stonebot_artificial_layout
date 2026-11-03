"""排板 DXF 回读门禁：交付前把写出的文件读回来，与版本布局逐件比对。

写出（write_nested_dxf）只是第一步；真正的交付可信来自"读回的文件
与 active 版本记录一致"。比对失败时调用方必须删除文件并拒绝交付
（fail closed），绝不让未核实的 DXF 流出。
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

import ezdxf
from shapely.geometry import LineString, MultiLineString
from shapely.ops import polygonize, unary_union
from shapely import wkt as shapely_wkt
from shapely.geometry import Point, Polygon

from src.dxf_reader import _bulge_arc_points, _sample_arc

_PRECISION = 4  # 坐标比对精度 0.1mm 级（round 到 4 位小数）
# 圆弧实体与展平多边形之间的几何容差：读图端采样误差在 1.5mm 内
# （SOURCE_CURVE_TOL_MM），回读用同量级容差比对轮廓。
_GEOMETRY_TOL_MM = 2.0


def sheets_payload_from_result(result) -> list[dict[str, Any]]:
    """从 NestingResult 提取回读比对所需的板材/零件载荷。"""
    sheets = []
    for sheet in result.sheets:
        parts = []
        for part in sheet.parts:
            parts.append(
                {
                    "number": part.number,
                    "outer_polygon_wkt": part.outer_polygon.wkt,
                    "holes_wkt": [hole.wkt for hole in part.holes],
                }
            )
        sheets.append(
            {
                "index": sheet.index,
                "width": sheet.width,
                "height": sheet.height,
                "parts": parts,
            }
        )
    return sheets


def _canonical_ring(points, precision: int = _PRECISION) -> tuple:
    """规范化坐标环：闭合重复点去除 + 旋转到字典序最小点。"""
    pts = [
        (round(float(x), precision), round(float(y), precision))
        for x, y in points
    ]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return ()
    start = min(range(len(pts)), key=lambda i: pts[i])
    return tuple(pts[start:] + pts[:start])


def _ring_from_wkt(wkt_text: str) -> tuple:
    """从 WKT 提取外环（MultiPolygon 取最大瓣，与写出端口径一致）。"""
    geom = shapely_wkt.loads(wkt_text)
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return _canonical_ring(geom.exterior.coords)


def _ring_polygon(wkt_text: str) -> Polygon:
    """WKT → 外环多边形（MultiPolygon 取最大瓣）。"""
    geom = shapely_wkt.loads(wkt_text)
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return Polygon(geom.exterior.coords)


def _entity_anchor(entity) -> tuple[float, float]:
    dtype = entity.dxftype()
    if dtype == "LWPOLYLINE":
        points = list(entity.get_points(format="xy"))
        if points:
            return (float(points[0][0]), float(points[0][1]))
        return (0.0, 0.0)
    if dtype == "ARC":
        start = entity.start_point
        return (float(start.x), float(start.y))
    if dtype == "CIRCLE":
        center = entity.dxf.center
        return (float(center.x), float(center.y))
    try:
        insert = entity.dxf.insert
        return (float(insert.x), float(insert.y))
    except Exception:  # noqa: BLE001 - 无锚点实体归第一张板
        return (0.0, 0.0)


def _entity_points(entity, offset_x: float, offset_y: float) -> list[tuple[float, float]]:
    """把一条 OUTER/HOLES 实体展开成点列（含 bulge 弧段与 ARC 采样）。"""
    dtype = entity.dxftype()
    points: list[tuple[float, float]] = []
    if dtype == "LWPOLYLINE":
        vertices = [
            (float(x), float(y), float(bulge))
            for x, y, bulge in entity.get_points("xyb")
        ]
        closed = bool(entity.closed)
        count = len(vertices)
        for index in range(count):
            start = vertices[index]
            if index == count - 1 and not closed:
                points.append((start[0], start[1]))
                break
            end = vertices[(index + 1) % count]
            points.append((start[0], start[1]))
            if abs(start[2]) > 1e-9:
                points.extend(
                    _bulge_arc_points(start[:2], end[:2], start[2])[1:]
                )
        if closed and points:
            points.append(points[0])
    elif dtype == "ARC":
        spec = {
            "center": (entity.dxf.center.x, entity.dxf.center.y),
            "radius": float(entity.dxf.radius),
            "start_angle": float(entity.dxf.start_angle),
            "end_angle": float(entity.dxf.end_angle),
        }
        points.extend(_sample_arc(spec))
    elif dtype == "CIRCLE":
        center = entity.dxf.center
        radius = float(entity.dxf.radius)
        for index in range(65):
            angle = index * 2.0 * math.pi / 64.0
            points.append(
                (
                    float(center.x) + radius * math.cos(angle),
                    float(center.y) + radius * math.sin(angle),
                )
            )
    elif dtype in {"ELLIPSE", "SPLINE"}:
        try:
            points.extend(
                (float(p.x), float(p.y)) for p in entity.flattening(0.1)
            )
        except Exception:  # noqa: BLE001 - 畸形曲线不参与比对
            return []
    return [(x - offset_x, y - offset_y) for x, y in points]


def _polygonize_entities(entities, offset_x: float, offset_y: float) -> list[Polygon]:
    """把同层实体解析成闭合轮廓。

    先按"单条实体本身就是闭合环"解析（多段线/圆/封闭轮廓），
    只有剩余的开放段（典型是 ARC + 折线围一件）才做拼环。
    这样相邻共边零件不会被合并成一件。
    """
    polygons: list[Polygon] = []
    lines = []
    for entity in entities:
        points = _entity_points(entity, offset_x, offset_y)
        if len(points) < 2:
            continue
        closed = (
            len(points) >= 4
            and abs(points[0][0] - points[-1][0]) <= 1e-6
            and abs(points[0][1] - points[-1][1]) <= 1e-6
        )
        if closed:
            polygon = Polygon(points)
            if not polygon.is_empty and polygon.area > 1e-6:
                polygons.append(polygon)
                continue
        lines.append(LineString(points))
    if lines:
        merged = unary_union(
            lines if len(lines) > 1 else lines[0]
        )
        for polygon in polygonize(merged):
            if not polygon.is_empty and polygon.area > 1e-6:
                polygons.append(polygon)
    return polygons


def _group_boundary(entities, offset_x: float, offset_y: float):
    """一个零件组内所有外轮廓实体拼成的边界几何（线串/多线串）。"""
    lines = []
    for entity in entities:
        points = _entity_points(entity, offset_x, offset_y)
        if len(points) >= 2:
            lines.append(LineString(points))
    if not lines:
        return None
    return unary_union(lines)


def _hole_boundaries(entities, offset_x: float, offset_y: float) -> list:
    """孔洞边界：闭合实体逐个算一个孔，剩余开放段再拼。"""
    boundaries: list = []
    open_lines = []
    for entity in entities:
        points = _entity_points(entity, offset_x, offset_y)
        if len(points) < 2:
            continue
        closed = (
            len(points) >= 4
            and abs(points[0][0] - points[-1][0]) <= 1e-6
            and abs(points[0][1] - points[-1][1]) <= 1e-6
        )
        if closed:
            boundaries.append(LineString(points))
        else:
            open_lines.append(LineString(points))
    if open_lines:
        boundaries.append(unary_union(open_lines))
    return boundaries


def _match_boundaries(
    expected: list[Polygon],
    actual: list,
    tol: float = _GEOMETRY_TOL_MM,
) -> tuple[int, int]:
    """按边界几何做多重集匹配：Hausdorff + 周长双重校验。"""
    used = [False] * len(actual)
    missing = 0
    for want in expected:
        boundary = want.exterior
        want_length = boundary.length
        matched = None
        for index, got in enumerate(actual):
            if used[index] or got is None:
                continue
            if boundary.hausdorff_distance(got) > tol:
                continue
            got_length = float(getattr(got, "length", 0.0))
            allowed = max(2.0, 0.02 * want_length)
            if abs(got_length - want_length) > allowed:
                continue
            matched = index
            break
        if matched is None:
            missing += 1
        else:
            used[matched] = True
    extra = sum(1 for flag in used if not flag)
    return missing, extra


def _match_polygons(
    expected: list[Polygon],
    actual: list[Polygon],
    tol: float = _GEOMETRY_TOL_MM,
) -> tuple[int, int]:
    """按几何容差做多重集匹配，返回 (缺件数, 多出数)。"""
    used = [False] * len(actual)
    missing = 0
    for want in expected:
        matched = None
        for index, got in enumerate(actual):
            if used[index]:
                continue
            if want.hausdorff_distance(got) <= tol:
                matched = index
                break
        if matched is None:
            missing += 1
        else:
            used[matched] = True
    extra = sum(1 for flag in used if not flag)
    return missing, extra


def verify_nested_dxf(
    path: str | Path,
    sheets_payload: list[dict[str, Any]],
    *,
    gap: float = 200.0,
) -> list[str]:
    """回读排板 DXF 并与期望布局比对，返回问题清单（空 = 通过）。

    比对维度：大板数量与外框位置、每张板的零件外轮廓多重集、孔洞
    归属与数量、编号文本多重集。坐标按 0.1mm 级精度规范化。
    """
    errors: list[str] = []
    try:
        doc = ezdxf.readfile(str(path))
    except Exception as exc:  # noqa: BLE001 - 读不回就是失败
        return [f"readback: 无法读回排板 DXF：{type(exc).__name__}: {exc}"]

    msp = doc.modelspace()
    borders = []
    outers = []
    holes = []
    numbers = []
    for entity in msp:
        layer = entity.dxf.layer
        dtype = entity.dxftype()
        if layer == "SHEET_BORDER" and dtype == "LWPOLYLINE":
            points = [
                (float(x), float(y))
                for x, y, *_ in entity.get_points(format="xy")
            ]
            if points:
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                borders.append((min(xs), min(ys), max(xs), max(ys)))
        elif layer == "OUTER" and dtype in {
            "LWPOLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"
        }:
            outers.append(entity)
        elif layer == "HOLES" and dtype in {
            "LWPOLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"
        }:
            holes.append(entity)
        elif layer == "NUMBERS" and dtype in {"TEXT", "MTEXT"}:
            insert = entity.dxf.insert
            numbers.append(
                (
                    float(insert.x),
                    float(insert.y),
                    getattr(entity.dxf, "text", ""),
                )
            )

    if len(borders) != len(sheets_payload):
        errors.append(
            f"readback: 大板外框数量 {len(borders)} != 版本板数 "
            f"{len(sheets_payload)}"
        )
        return errors  # 板数都对不上，逐板比对没有意义

    def locate_sheet(x: float, y: float) -> int | None:
        for index, (x0, y0, x1, y1) in enumerate(borders):
            if x0 - 1e-6 <= x <= x1 + 1e-6 and y0 - 1e-6 <= y <= y1 + 1e-6:
                return index
        return None

    def locate_entity_sheet(entity) -> int | None:
        """Locate an entity by its sampled extent when an oversized part
        crosses the nominal sheet border.

        The first polyline vertex is not a reliable sheet locator: a part may
        legally use the physical oversize margin and start just outside the
        nominal border.  Board blocks are separated by a large gap, so the
        nearest border centre remains unambiguous.
        """
        index = locate_sheet(*_entity_anchor(entity))
        if index is not None:
            return index
        points = _entity_points(entity, 0.0, 0.0)
        if not points:
            return None
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        centre_x = (min(xs) + max(xs)) * 0.5
        centre_y = (min(ys) + max(ys)) * 0.5
        return min(
            range(len(borders)),
            key=lambda candidate: (
                abs(
                    centre_x
                    - (borders[candidate][0] + borders[candidate][2]) * 0.5
                )
                + abs(
                    centre_y
                    - (borders[candidate][1] + borders[candidate][3]) * 0.5
                )
            ),
        )

    def locate_point_sheet(x: float, y: float) -> int | None:
        index = locate_sheet(x, y)
        if index is not None:
            return index
        return min(
            range(len(borders)),
            key=lambda candidate: (
                abs(x - (borders[candidate][0] + borders[candidate][2]) * 0.5)
                + abs(
                    y - (borders[candidate][1] + borders[candidate][3]) * 0.5
                )
            ),
        )

    # 外框位置：第 i 个外框必须落在第 i 张板的网格位置
    for index, (x0, y0, _x1, _y1) in enumerate(borders):
        sheet = sheets_payload[index]
        col, row = index % 6, index // 6
        expect_x = col * (float(sheet["width"]) + gap)
        expect_y = row * (float(sheet["height"]) + gap)
        if abs(x0 - expect_x) > 0.01 or abs(y0 - expect_y) > 0.01:
            errors.append(
                f"readback: 第 {index + 1} 张板外框位置 "
                f"({x0:.2f},{y0:.2f}) 偏离网格 ({expect_x:.2f},{expect_y:.2f})"
            )

    # 每个零件写成独立 GROUP（外轮廓 + 孔洞 + 编号）：按组解析轮廓，
    # 相邻共边零件不会被拼成一件。
    group_of_entity: dict[str, str] = {}
    try:
        for name, entities in doc.groups:
            for entity in entities:
                group_of_entity[entity.dxf.handle] = str(name)
    except Exception:  # noqa: BLE001 - 无组信息时退回逐实体解析
        group_of_entity = {}

    def grouped_contours(entities):
        """把一层的实体按零件组切分，逐组解析成轮廓。"""
        buckets: dict[str, list] = {}
        loose: list = []
        for entity in entities:
            key = group_of_entity.get(entity.dxf.handle)
            if key is None:
                loose.append(entity)
            else:
                buckets.setdefault(key, []).append(entity)
        result: list[tuple[str | None, list]] = list(buckets.items())
        result.extend((None, [entity]) for entity in loose)
        return result

    for index, sheet in enumerate(sheets_payload):
        ox, oy = borders[index][0], borders[index][1]
        label = f"第 {index + 1} 张板"

        sheet_outers = [e for e in outers if locate_entity_sheet(e) == index]
        sheet_holes = [e for e in holes if locate_entity_sheet(e) == index]
        sheet_numbers = [
            text for x, y, text in numbers if locate_point_sheet(x, y) == index
        ]

        expected_parts = [_ring_polygon(part["outer_polygon_wkt"])
                          for part in sheet["parts"]]
        actual_parts = [
            _group_boundary(contours, ox, oy)
            for _key, contours in grouped_contours(sheet_outers)
        ]
        missing, extra = _match_boundaries(expected_parts, actual_parts)
        if missing or extra:
            errors.append(
                f"readback: {label} 零件外轮廓不一致"
                f"（缺 {missing} 件 / 多出 {extra} 件，"
                f"期望 {len(expected_parts)} 件 / 实际 {len(actual_parts)} 件）"
            )

        expected_holes = [
            _ring_polygon(hole_wkt)
            for part in sheet["parts"]
            for hole_wkt in part["holes_wkt"]
        ]
        actual_holes = []
        for _key, contours in grouped_contours(sheet_holes):
            actual_holes.extend(_hole_boundaries(contours, ox, oy))
        hole_missing, hole_extra = _match_boundaries(
            expected_holes, actual_holes
        )
        if hole_missing or hole_extra:
            errors.append(
                f"readback: {label} 孔洞不一致（缺 {hole_missing} 个 / "
                f"多出 {hole_extra} 个，期望 {len(expected_holes)} 个 / "
                f"实际 {len(actual_holes)} 个）"
            )

        expected_numbers = Counter(part["number"] for part in sheet["parts"])
        actual_numbers = Counter(sheet_numbers)
        if actual_numbers != expected_numbers:
            errors.append(
                f"readback: {label} 编号文本不一致（期望 "
                f"{sum(expected_numbers.values())} 个 / 实际 "
                f"{sum(actual_numbers.values())} 个）"
            )

    return errors
