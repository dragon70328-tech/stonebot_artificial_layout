"""图纸画像与匹配模块。

把一个已经跑通的人工读图流程沉淀为 DrawingProfile，后续上传新图纸时先计算
图纸指纹，再匹配已有画像，避免每次从零开始分析。
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import Counter
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from .dxf_reader import (
    _build_part_hierarchy,
    _entity_to_polygon,
    _linetype_name,
    _lwpolyline_points,
    extract_closed_polygons,
)


@dataclass
class DrawingProfile:
    """一张图纸的读图规则画像。"""

    name: str
    version: str
    description: str = ""
    panel_layer: str = ""
    hatch_layer: str | None = None
    use_hatch: bool = True
    number_layers: list[str] = field(default_factory=list)
    label_pattern: str = r"^(?P<material>\d{2}B)-?(?P<shape>\d+)$"
    material_group_enabled: bool = False
    material_prefix_pattern: str = r"^(?P<prefix>\d{2}B)"
    allowed_material_prefixes: list[str] = field(default_factory=list)
    first_part_left_edge: bool = False
    assignment_mode: str = "point_then_bbox"
    bbox_overlap_threshold: float = 0.5
    build_hierarchy: bool = True
    conflict_resolution: str = "random"
    closed_tolerance: float = 0.01
    exclude_entity_types: list[str] = field(default_factory=list)
    exclude_linetypes: list[str] = field(default_factory=lambda: ["DASH", "PHANTOM"])
    small_area_threshold: float = 100.0
    text_height: float = 40.0
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DrawingProfile":
        known = {
            field.name: field.default
            for field in fields(cls)
            if field.default is not MISSING
        }
        known.update(data)
        return cls(**{key: known[key] for key in cls.__dataclass_fields__})

    def compiled_label_pattern(self) -> re.Pattern[str]:
        return re.compile(self.label_pattern)


@dataclass
class DrawingIssue:
    """图纸体检发现的一个问题。"""

    issue_id: int
    severity: str
    type: str
    entity_handle: str | None
    layer: str
    coordinates: tuple[float, float]
    message: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "severity": self.severity,
            "type": self.type,
            "entity_handle": self.entity_handle,
            "layer": self.layer,
            "coordinates": self.coordinates,
            "message": self.message,
            "suggestion": self.suggestion,
        }


def load_profile(path: str | Path) -> DrawingProfile:
    """从 JSON 文件加载一个画像。"""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return DrawingProfile.from_dict(data)


def load_profiles(directory: str | Path) -> list[DrawingProfile]:
    """加载目录下所有 JSON 画像。"""
    directory = Path(directory)
    if not directory.exists():
        return []
    profiles = []
    for path in sorted(directory.glob("*.json")):
        profiles.append(load_profile(path))
    return profiles


def analyze_drawing(filepath: str | Path) -> dict[str, Any]:
    """生成图纸指纹，用于后续画像匹配。"""
    filepath = Path(filepath)
    doc = ezdxf.readfile(str(filepath))
    model = doc.modelspace()
    entities = list(model)

    entity_counts = Counter(entity.dxftype() for entity in entities)
    layer_counts = Counter(
        getattr(entity.dxf, "layer", "0") or "0"
        for entity in entities
    )
    linetype_counts = Counter(
        getattr(entity.dxf, "linetype", "BYLAYER") or "BYLAYER"
        for entity in entities
    )

    return {
        "path": str(filepath),
        "dxf_version": doc.dxfversion,
        "insunits": doc.header.get("$INSUNITS"),
        "entity_counts": dict(entity_counts),
        "layer_counts": dict(layer_counts),
        "linetype_counts": dict(linetype_counts),
        "has_hatch": entity_counts.get("HATCH", 0) > 0,
        "has_lwpolyline": entity_counts.get("LWPOLYLINE", 0) > 0,
        "has_line": entity_counts.get("LINE", 0) > 0,
        "has_circle": entity_counts.get("CIRCLE", 0) > 0,
    }


def rank_profiles(
    fingerprint: dict[str, Any],
    profiles: list[DrawingProfile],
) -> list[tuple[DrawingProfile, float]]:
    """按画像与图纸指纹的匹配度排序。"""
    layer_set = set(fingerprint.get("layer_counts", {}))
    entity_counts = fingerprint.get("entity_counts", {})
    results: list[tuple[DrawingProfile, float]] = []

    for profile in profiles:
        score = 0.0

        if profile.number_layers:
            number_hits = sum(
                1 for layer in profile.number_layers if layer in layer_set
            )
            score += number_hits / len(profile.number_layers) * 40.0

        if profile.panel_layer and profile.panel_layer in layer_set:
            score += 20.0

        if profile.use_hatch:
            if fingerprint.get("has_hatch"):
                score += 20.0
        elif fingerprint.get("has_lwpolyline") and not fingerprint.get("has_hatch"):
            score += 20.0

        if profile.panel_layer in layer_set and fingerprint.get("has_line"):
            score += 5.0

        for entity_type in profile.exclude_entity_types:
            if entity_counts.get(entity_type, 0) == 0:
                score += 2.0

        results.append((profile, score))

    results.sort(key=lambda item: item[1], reverse=True)
    return results


def match_profile(
    fingerprint: dict[str, Any],
    profiles: list[DrawingProfile],
) -> tuple[DrawingProfile, float] | None:
    """返回匹配度最高的画像；没有画像时返回 None。"""
    ranked = rank_profiles(fingerprint, profiles)
    return ranked[0] if ranked else None


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _entity_coordinates(entity) -> tuple[float, float]:
    """返回一个用于定位问题的坐标点。"""
    dtype = entity.dxftype()
    try:
        if dtype in ("TEXT", "MTEXT", "INSERT"):
            return (float(entity.dxf.insert.x), float(entity.dxf.insert.y))
        if dtype in ("CIRCLE", "ARC"):
            return (float(entity.dxf.center.x), float(entity.dxf.center.y))
        if dtype == "LINE":
            start = entity.dxf.start
            end = entity.dxf.end
            return (
                (float(start.x) + float(end.x)) / 2.0,
                (float(start.y) + float(end.y)) / 2.0,
            )
    except Exception:
        pass

    try:
        points = list(entity.get_points())
        if points:
            return (float(points[0][0]), float(points[0][1]))
    except Exception:
        pass
    return (0.0, 0.0)


def _text_box_polygon(entity) -> Polygon | None:
    try:
        extent = bbox.extents([entity])
        points = [(vertex.x, vertex.y) for vertex in extent.rect_vertices()]
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        return polygon
    except Exception:
        return None


def _entity_is_closed(entity, tolerance: float = 0.01) -> bool:
    dtype = entity.dxftype()
    try:
        if dtype == "LWPOLYLINE":
            if entity.closed:
                return True
            points = list(entity.get_points("xy"))
            return len(points) >= 3 and _distance(
                (float(points[0][0]), float(points[0][1])),
                (float(points[-1][0]), float(points[-1][1])),
            ) < tolerance
        if dtype == "POLYLINE":
            if getattr(entity, "is_closed", False):
                return True
            points = [
                (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
                for vertex in entity.vertices
            ]
            return len(points) >= 3 and _distance(points[0], points[-1]) < tolerance
        if dtype in ("CIRCLE", "SPLINE"):
            return True
    except Exception:
        return False
    return False


def _normalize_label(text: str, profile: DrawingProfile) -> str:
    value = " ".join(text.strip().upper().split())
    match = profile.compiled_label_pattern().match(value)
    if match:
        groups = match.groupdict()
        if "material" in groups and "shape" in groups:
            return f"{groups['material'].upper()}-{groups['shape']}"
    return value


def _make_issue(
    issues: list[DrawingIssue],
    *,
    severity: str,
    type_: str,
    entity,
    layer: str,
    coordinates: tuple[float, float],
    message: str,
    suggestion: str,
) -> None:
    entity_handle = None
    if entity is not None:
        entity_handle = getattr(entity.dxf, "handle", None)
    issues.append(
        DrawingIssue(
            issue_id=len(issues) + 1,
            severity=severity,
            type=type_,
            entity_handle=entity_handle,
            layer=layer,
            coordinates=coordinates,
            message=message,
            suggestion=suggestion,
        )
    )


def _hatch_edge_points(path, arc_segments: int = 32) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for edge in path.edges:
        try:
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
            for index in range(segment_count):
                angle = start + sweep * index / segment_count
                points.append(
                    (
                        center.x + radius * math.cos(angle),
                        center.y + radius * math.sin(angle),
                    )
                )
        except Exception:
            continue

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


def _hatch_polygon_from_paths(paths) -> tuple[Polygon, list[Polygon]] | None:
    path_polygons: list[Polygon] = []
    for path in paths:
        points = _hatch_edge_points(path)
        if len(points) < 4:
            continue
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty and polygon.area > 0.01:
            path_polygons.append(polygon)

    if not path_polygons:
        return None

    outer = max(path_polygons, key=lambda polygon: polygon.area)
    holes = [polygon for polygon in path_polygons if polygon is not outer]
    combined = outer
    if holes:
        combined = outer.difference(unary_union(holes))
        if isinstance(combined, MultiPolygon):
            combined = max(combined.geoms, key=lambda geom: geom.area)
    return combined, holes


def _polygons_from_line_entities(line_entities) -> list[Polygon]:
    """把 LINE 线段按端点闭合成多边形；返回面积有效的多边形列表。"""
    if len(line_entities) < 3:
        return []
    segments = []
    for line in line_entities:
        start = (
            round(float(line.dxf.start.x), 6),
            round(float(line.dxf.start.y), 6),
        )
        end = (
            round(float(line.dxf.end.x), 6),
            round(float(line.dxf.end.y), 6),
        )
        if _distance(start, end) < 1e-9:
            continue
        segments.append(LineString([start, end]))
    if not segments:
        return []
    polygons = list(polygonize(segments))
    return [
        polygon
        for polygon in polygons
        if not polygon.is_empty and polygon.area > 0.01
    ]


def _has_hatch_entities(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
) -> bool:
    if not profile.use_hatch:
        return False
    hatch_layer = profile.hatch_layer or None
    for entity in doc.modelspace():
        if entity.dxftype() != "HATCH":
            continue
        if hatch_layer is None:
            return True
        if (getattr(entity.dxf, "layer", "0") or "0") == hatch_layer:
            return True
    return False


def _extract_profile_panels(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
) -> list[dict[str, Any]]:
    panel_layer = profile.panel_layer or None
    panel_layers = [panel_layer] if panel_layer else None
    panels: list[dict[str, Any]] = []

    if profile.use_hatch:
        hatch_layer = profile.hatch_layer or None
        for entity in doc.modelspace():
            if entity.dxftype() != "HATCH":
                continue
            layer = getattr(entity.dxf, "layer", "0") or "0"
            if hatch_layer is not None and layer != hatch_layer:
                continue
            result = _hatch_polygon_from_paths(entity.paths)
            if result is None:
                continue
            combined, holes = result
            if combined.is_empty or combined.area <= 0.01:
                continue
            panels.append(
                {
                    "index": len(panels),
                    "polygon": combined,
                    "outer_polygon": combined,
                    "holes": holes,
                    "centroid": combined.centroid,
                    "area": combined.area,
                    "handle": getattr(entity.dxf, "handle", None),
                }
            )
        if panels:
            return panels

    polygon_items = extract_closed_polygons(
        doc,
        panel_layers=panel_layers,
        exclude_linetypes=profile.exclude_linetypes,
    )
    if polygon_items:
        if profile.build_hierarchy:
            hierarchy = _build_part_hierarchy(polygon_items)
            for item in hierarchy:
                if item["parent"] != -1:
                    continue

                outer = item["poly"]
                holes = [hierarchy[child]["poly"] for child in item["children"]]
                combined = outer
                if holes:
                    combined = outer.difference(unary_union(holes))
                    if isinstance(combined, MultiPolygon):
                        combined = max(combined.geoms, key=lambda geom: geom.area)

                if combined.is_empty or combined.area <= 0.01:
                    continue

                panels.append(
                    {
                        "index": len(panels),
                        "polygon": combined,
                        "outer_polygon": outer,
                        "holes": holes,
                        "centroid": combined.centroid,
                        "area": combined.area,
                        "handle": item.get("handle"),
                    }
                )
        else:
            for polygon, handle in polygon_items:
                if polygon.is_empty or polygon.area <= 0.01:
                    continue
                panels.append(
                    {
                        "index": len(panels),
                        "polygon": polygon,
                        "outer_polygon": polygon,
                        "holes": [],
                        "centroid": polygon.centroid,
                        "area": polygon.area,
                        "handle": handle,
                    }
                )

    line_entities = []
    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    for entity in doc.modelspace():
        if entity.dxftype() != "LINE":
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if panel_layer and layer != panel_layer:
            continue
        linetype = _linetype_name(entity, doc).upper()
        if linetype in excluded_linetypes:
            continue
        line_entities.append(entity)

    for polygon in _polygons_from_line_entities(line_entities):
        if polygon.is_empty or polygon.area <= 0.01:
            continue
        panels.append(
            {
                "index": len(panels),
                "polygon": polygon,
                "outer_polygon": polygon,
                "holes": [],
                "centroid": polygon.centroid,
                "area": polygon.area,
                "handle": None,
            }
        )
    return panels


def _collect_profile_number_texts(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
) -> list[dict[str, Any]]:
    allowed_layers = set(profile.number_layers)
    pattern = profile.compiled_label_pattern()
    texts: list[dict[str, Any]] = []

    for entity in doc.modelspace():
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if allowed_layers and layer not in allowed_layers:
            continue
        try:
            text = (
                entity.dxf.text
                if entity.dxftype() == "TEXT"
                else entity.plain_text()
            )
        except Exception:
            continue
        if not text or not text.strip():
            continue
        text = text.strip()
        if not pattern.match(text):
            continue
        texts.append(
            {
                "entity": entity,
                "text": text,
                "layer": layer,
                "point": _entity_coordinates(entity),
                "box": _text_box_polygon(entity),
            }
        )
    return texts


def _overlap_ratio(polygon: Polygon, box: Polygon | None) -> float:
    if box is None or box.is_empty or box.area <= 0:
        return 0.0
    return polygon.intersection(box).area / box.area


def _assign_texts_to_panels(
    panels: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    profile: DrawingProfile,
) -> tuple[dict[int, list[int]], set[int]]:
    assignments: dict[int, list[int]] = {
        panel["index"]: [] for panel in panels
    }
    matched_texts: set[int] = set()

    # 第一轮：编号插入点直接落在面板内时，优先归属到该面板。
    # 如果一个面板内有多个编号文字，选择和面板重叠面积最大的那个，
    # 这与当前人工整理后的“编号在哪个封闭图形里就是哪个图形的编号”一致。
    for panel in panels:
        panel_index = panel["index"]
        inside = []
        for text_index, text in enumerate(texts):
            if text_index in matched_texts:
                continue
            point = Point(text["point"][0], text["point"][1])
            try:
                if panel["polygon"].contains(point):
                    inside.append(text_index)
            except Exception:
                continue
        if inside:
            if profile.conflict_resolution == "best_overlap":
                best = max(
                    inside,
                    key=lambda index: _overlap_ratio(
                        panel["polygon"], texts[index]["box"]
                    ),
                )
            else:
                best = random.Random(0).choice(inside)
            assignments[panel_index].append(best)
            matched_texts.add(best)

    # 第二轮：编号插入点不在面板内时，用文字包围盒与面板的重叠比例补配。
    for panel in panels:
        panel_index = panel["index"]
        if assignments[panel_index]:
            continue
        best_index: int | None = None
        best_ratio = 0.0
        for text_index, text in enumerate(texts):
            if text_index in matched_texts:
                continue
            ratio = _overlap_ratio(panel["polygon"], text["box"])
            if ratio > best_ratio:
                best_ratio = ratio
                best_index = text_index
        if best_index is not None and best_ratio >= profile.bbox_overlap_threshold:
            assignments[panel_index].append(best_index)
            matched_texts.add(best_index)

    return assignments, matched_texts


def _add_entity_filter_issues(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    excluded_types = set(profile.exclude_entity_types)
    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    number_layers = set(profile.number_layers)

    for entity in doc.modelspace():
        dtype = entity.dxftype()
        layer = getattr(entity.dxf, "layer", "0") or "0"
        coordinates = _entity_coordinates(entity)

        if dtype in excluded_types:
            message = f"检测到当前画像排除的实体类型 {dtype}"
            if dtype == "CIRCLE":
                message += "；若它是水槽/龙头孔，请确认孔洞归属"
            _make_issue(
                issues,
                severity="warning",
                type_="unnecessary_entity",
                entity=entity,
                layer=layer,
                coordinates=coordinates,
                message=message,
                suggestion="若不是排板需要的孔洞/标注，请删除；否则应调整画像规则",
            )

        if dtype in ("LWPOLYLINE", "POLYLINE", "LINE", "ARC", "SPLINE"):
            linetype = _linetype_name(entity, doc).upper()
            if linetype in excluded_linetypes:
                _make_issue(
                    issues,
                    severity="warning",
                    type_="excluded_linetype_entity",
                    entity=entity,
                    layer=layer,
                    coordinates=coordinates,
                    message=f"检测到辅助线型实体，线型为 {linetype}",
                    suggestion="这些辅助线不参与排板，可清理或保留在独立辅助图层",
                )

        if dtype in ("TEXT", "MTEXT") and number_layers and layer not in number_layers:
            _make_issue(
                issues,
                severity="info",
                type_="non_panel_text",
                entity=entity,
                layer=layer,
                coordinates=coordinates,
                message="检测到非编号图层文字",
                suggestion="若不是图纸标题/说明，建议删除或移动到编号图层；否则可加入忽略规则",
            )


def _add_unclosed_polyline_issues(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    for entity in doc.modelspace():
        if entity.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if profile.panel_layer and layer != profile.panel_layer:
            continue
        linetype = _linetype_name(entity, doc).upper()
        if linetype in excluded_linetypes:
            continue
        if not _entity_is_closed(entity, profile.closed_tolerance):
            _make_issue(
                issues,
                severity="warning",
                type_="unclosed_geometry",
                entity=entity,
                layer=layer,
                coordinates=_entity_coordinates(entity),
                message="面板边界疑似未封闭，首尾点不重合",
                suggestion="请闭合 LWPOLYLINE/POLYLINE，或确认它是否为不需要排板的辅助线",
            )


def _add_line_chain_issues(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    lines = []
    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    for entity in doc.modelspace():
        if entity.dxftype() != "LINE":
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if profile.panel_layer and layer != profile.panel_layer:
            continue
        linetype = _linetype_name(entity, doc).upper()
        if linetype in excluded_linetypes:
            continue
        lines.append(entity)

    if not lines:
        return

    def snap(point: tuple[float, float]) -> tuple[int, int]:
        return (round(point[0] / 0.5), round(point[1] / 0.5))

    degree: Counter[tuple[int, int]] = Counter()
    for line in lines:
        start = (float(line.dxf.start.x), float(line.dxf.start.y))
        end = (float(line.dxf.end.x), float(line.dxf.end.y))
        degree[snap(start)] += 1
        degree[snap(end)] += 1

    for line in lines:
        start = (float(line.dxf.start.x), float(line.dxf.start.y))
        end = (float(line.dxf.end.x), float(line.dxf.end.y))
        if degree[snap(start)] < 2 or degree[snap(end)] < 2:
            _make_issue(
                issues,
                severity="warning",
                type_="unclosed_geometry",
                entity=line,
                layer=getattr(line.dxf, "layer", "0") or "0",
                coordinates=_entity_coordinates(line),
                message="LINE 线段未形成闭合链，无法作为面板边界读取",
                suggestion="请将面板边界闭合为 LWPOLYLINE/POLYLINE，或删除多余的开放线段",
            )


def _add_invalid_geometry_issues(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    for entity in doc.modelspace():
        if entity.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if profile.panel_layer and layer != profile.panel_layer:
            continue
        linetype = _linetype_name(entity, doc).upper()
        if linetype in excluded_linetypes:
            continue

        try:
            if entity.dxftype() == "LWPOLYLINE":
                points = _lwpolyline_points(entity)
            else:
                points = [
                    (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
                    for vertex in entity.vertices
                ]
        except Exception:
            points = []

        if len(points) < 3:
            _make_issue(
                issues,
                severity="error",
                type_="invalid_geometry",
                entity=entity,
                layer=layer,
                coordinates=_entity_coordinates(entity),
                message="实体顶点不足，无法构成封闭图形",
                suggestion="删除该实体，或补齐顶点后重新闭合",
            )
            continue

        try:
            if _distance(points[0], points[-1]) > 1e-9:
                points = [*points, points[0]]
            raw = Polygon(points)
            if raw.is_valid:
                continue
            fixed = raw.buffer(0)
            if fixed.is_empty or fixed.area <= 0.01:
                _make_issue(
                    issues,
                    severity="error",
                    type_="invalid_geometry",
                    entity=entity,
                    layer=layer,
                    coordinates=_entity_coordinates(entity),
                    message="图形自相交且修复后无有效面积",
                    suggestion="检查并修正自相交线段或删除该图形",
                )
            else:
                _make_issue(
                    issues,
                    severity="warning",
                    type_="invalid_geometry",
                    entity=entity,
                    layer=layer,
                    coordinates=_entity_coordinates(entity),
                    message="图形自相交，但可通过 buffer(0) 自动修复",
                    suggestion="建议手工修正自相交点，避免排板几何异常",
                )
        except Exception:
            continue


def _add_duplicate_panel_issues(
    panels: list[dict[str, Any]],
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    if not panels:
        return

    tree = STRtree([panel["polygon"] for panel in panels])
    duplicate_indices: set[int] = set()

    for index, panel in enumerate(panels):
        for other_index in tree.query(panel["polygon"]):
            other_index = int(other_index)
            if other_index <= index:
                continue
            other = panels[other_index]
            denom = min(panel["area"], other["area"])
            if denom <= 0:
                continue
            try:
                overlap = panel["polygon"].intersection(other["polygon"]).area
            except Exception:
                continue
            if overlap / denom >= 0.8:
                duplicate_indices.add(index)
                duplicate_indices.add(other_index)

    for index in sorted(duplicate_indices):
        panel = panels[index]
        centroid = panel["centroid"]
        _make_issue(
            issues,
            severity="warning",
            type_="duplicate_geometry",
            entity=None,
            layer=profile.panel_layer or "",
            coordinates=(float(centroid.x), float(centroid.y)),
            message="面板与其他面板大面积重叠，疑似重复绘制",
            suggestion="删除多余重复图形，或确认它们是否为孔洞/嵌套关系",
        )


def _add_small_area_issues(
    panels: list[dict[str, Any]],
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    for panel in panels:
        if panel["area"] < profile.small_area_threshold:
            centroid = panel["centroid"]
            _make_issue(
                issues,
                severity="warning",
                type_="suspicious_small_area",
                entity=None,
                layer=profile.panel_layer or "",
                coordinates=(float(centroid.x), float(centroid.y)),
                message=f"面板面积 {panel['area']:.2f} 小于阈值 {profile.small_area_threshold:.2f}",
                suggestion="请人工确认它是否需要排板；若为孔洞/辅助线，请排除",
            )


def _add_duplicate_text_issues(
    texts: list[dict[str, Any]],
    issues: list[DrawingIssue],
) -> None:
    groups: dict[tuple[str, str, int, int], list[int]] = {}
    for index, text in enumerate(texts):
        if text["box"] is None:
            x = round(text["point"][0], 1)
            y = round(text["point"][1], 1)
        else:
            centroid = text["box"].centroid
            x = round(centroid.x, 1)
            y = round(centroid.y, 1)
        key = (text["layer"], text["text"], x, y)
        groups.setdefault(key, []).append(index)

    for indexes in groups.values():
        if len(indexes) <= 1:
            continue
        first = texts[indexes[0]]
        _make_issue(
            issues,
            severity="warning",
            type_="duplicate_text",
            entity=first["entity"],
            layer=first["layer"],
            coordinates=first["point"],
            message=f"检测到 {len(indexes)} 个位置几乎相同的文字 '{first['text']}'",
            suggestion="保留一个，删除多余重复文字；重复的材料编号若位于不同面板则不受影响",
        )


def _add_number_assignment_issues(
    panels: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    assignments: dict[int, list[int]],
    matched_texts: set[int],
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    for panel in panels:
        panel_index = panel["index"]
        inside_labels: set[str] = set()
        for text_index, text in enumerate(texts):
            point = Point(text["point"][0], text["point"][1])
            try:
                if panel["polygon"].contains(point):
                    inside_labels.add(
                        _normalize_label(text["text"], profile)
                    )
            except Exception:
                continue
        if len(inside_labels) > 1 and profile.conflict_resolution == "report":
            centroid = panel["centroid"]
            _make_issue(
                issues,
                severity="error",
                type_="conflicting_number_in_panel",
                entity=None,
                layer=profile.panel_layer or "",
                coordinates=(float(centroid.x), float(centroid.y)),
                message=f"同一面板内出现多个不同编号：{', '.join(sorted(inside_labels))}",
                suggestion="确认编号插入点/引线归属，删除错误编号",
            )

        labels = {
            _normalize_label(texts[index]["text"], profile)
            for index in assignments.get(panel_index, [])
        }
        if not labels:
            centroid = panel["centroid"]
            _make_issue(
                issues,
                severity="warning",
                type_="panel_without_number",
                entity=None,
                layer=profile.panel_layer or "",
                coordinates=(float(centroid.x), float(centroid.y)),
                message="面板没有匹配到编号",
                suggestion="请补充编号，或确认该图形是否需要排板",
            )
        elif len(labels) > 1:
            centroid = panel["centroid"]
            _make_issue(
                issues,
                severity="error",
                type_="conflicting_number_in_panel",
                entity=None,
                layer=profile.panel_layer or "",
                coordinates=(float(centroid.x), float(centroid.y)),
                message=f"同一面板内出现多个不同编号：{', '.join(sorted(labels))}",
                suggestion="确认编号插入点/引线归属，删除错误编号",
            )

    for index, text in enumerate(texts):
        if index not in matched_texts:
            _make_issue(
                issues,
                severity="warning",
                type_="number_without_panel",
                entity=text["entity"],
                layer=text["layer"],
                coordinates=text["point"],
                message=f"编号 '{text['text']}' 没有匹配到面板",
                suggestion="检查编号位置、面板边界或引线，确认它是否应属于某个面板",
            )


def audit_drawing(
    filepath: str | Path,
    profile: DrawingProfile,
) -> list[DrawingIssue]:
    """按画像规则对图纸做一次体检，返回问题列表。"""
    filepath = Path(filepath)
    doc = ezdxf.readfile(str(filepath))
    issues: list[DrawingIssue] = []

    _add_entity_filter_issues(doc, profile, issues)
    panels_from_hatch = _has_hatch_entities(doc, profile)
    if not panels_from_hatch:
        _add_unclosed_polyline_issues(doc, profile, issues)
        _add_line_chain_issues(doc, profile, issues)
        _add_invalid_geometry_issues(doc, profile, issues)

    panels = _extract_profile_panels(doc, profile)
    _add_duplicate_panel_issues(panels, profile, issues)
    _add_small_area_issues(panels, profile, issues)

    texts = _collect_profile_number_texts(doc, profile)
    _add_duplicate_text_issues(texts, issues)
    assignments, matched_texts = _assign_texts_to_panels(panels, texts, profile)
    _add_number_assignment_issues(
        panels,
        texts,
        assignments,
        matched_texts,
        profile,
        issues,
    )
    return issues


def write_audit_json(
    issues: list[DrawingIssue],
    output_path: str | Path,
) -> Path:
    """把体检结果写入 JSON，便于 SaaS 端展示问题清单。"""
    output_path = Path(output_path)
    summary = Counter(issue.type for issue in issues)
    payload = {
        "issue_count": len(issues),
        "summary": dict(summary),
        "issues": [issue.to_dict() for issue in issues],
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


_AUDIT_ISSUE_LABELS = {
    "unnecessary_entity": "排除实体",
    "excluded_linetype_entity": "辅助线型",
    "unclosed_geometry": "未闭合",
    "invalid_geometry": "无效几何",
    "duplicate_text": "重复文字",
    "duplicate_geometry": "重复图形",
    "panel_without_number": "缺编号",
    "number_without_panel": "编号无面板",
    "conflicting_number_in_panel": "编号冲突",
    "suspicious_small_area": "面积过小",
    "non_panel_text": "非编号文字",
}


def write_audit_dxf(
    source_filepath: str | Path,
    issues: list[DrawingIssue],
    output_path: str | Path,
) -> Path:
    """在原始 DXF 上叠加问题标记，并生成问题清单布局。"""
    source_filepath = Path(source_filepath)
    output_path = Path(output_path)
    doc = ezdxf.readfile(str(source_filepath))

    severity_layers = {
        "error": ("AUDIT_ERROR", 1),
        "warning": ("AUDIT_WARNING", 2),
        "info": ("AUDIT_INFO", 4),
    }
    for layer_name, color in severity_layers.values():
        if layer_name not in doc.layers:
            doc.layers.add(layer_name, color=color, linetype="CONTINUOUS")

    msp = doc.modelspace()
    marker_radius = {"error": 90.0, "warning": 70.0, "info": 50.0}
    for issue in issues:
        layer_name, _ = severity_layers.get(issue.severity, severity_layers["warning"])
        x, y = issue.coordinates
        radius = marker_radius.get(issue.severity, 70.0)
        msp.add_circle((x, y), radius, dxfattribs={"layer": layer_name})
        marker_text = msp.add_text(
            str(issue.issue_id),
            dxfattribs={"height": 50.0, "layer": layer_name},
        )
        marker_text.set_placement((x, y), align=TextEntityAlignment.MIDDLE_CENTER)

    legend = doc.layouts.new("问题清单")
    legend.add_text(
        "问题清单",
        dxfattribs={"height": 200.0, "layer": "AUDIT_ERROR"},
    ).set_placement((0, 0))
    y_cursor = -350.0
    for issue in issues:
        label = _AUDIT_ISSUE_LABELS.get(issue.type, issue.type)
        line = (
            f"#{issue.issue_id} {label} "
            f"({issue.coordinates[0]:.1f}, {issue.coordinates[1]:.1f}) "
            f"{issue.message}"
        )
        layer_name, _ = severity_layers.get(issue.severity, severity_layers["warning"])
        legend.add_text(
            line,
            dxfattribs={"height": 70.0, "layer": layer_name},
        ).set_placement((0, y_cursor))
        y_cursor -= 110.0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(output_path))
    return output_path
