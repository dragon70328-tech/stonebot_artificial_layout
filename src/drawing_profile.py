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
from dataclasses import MISSING, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import ezdxf
import ezdxf.recover
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from .dxf_reader import (
    _build_part_hierarchy,
    _entity_to_polygon,
    _is_closed,
    _is_recoverable_closed,
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
    panel_layers: list[str] = field(default_factory=list)
    hole_layers: list[str] = field(default_factory=list)
    hatch_layer: str | None = None
    use_hatch: bool = True
    number_layers: list[str] = field(default_factory=list)
    label_pattern: str = r"^(?P<material>\d{2}B)-?(?P<shape>\d+)$"
    zone_layers: list[str] = field(default_factory=list)
    zone_label_pattern: str = r"^(?P<zone>\d+#)$"
    zone_max_distance: float = 100000.0
    material_group_enabled: bool = False
    material_prefix_pattern: str = r"^(?P<prefix>\d{2}B)"
    allowed_material_prefixes: list[str] = field(default_factory=list)
    first_part_left_edge: bool = False
    assignment_mode: str = "point_then_bbox"
    room_number_format: str = "{unit}-{room}-{index:02d}"
    room_max_distance: float = 5000.0
    number_fallback_radius: float = 0.0
    bbox_overlap_threshold: float = 0.5
    build_hierarchy: bool = True
    conflict_resolution: str = "random"
    closed_tolerance: float = 0.01
    exclude_entity_types: list[str] = field(default_factory=list)
    exclude_entity_handles: list[str] = field(default_factory=list)
    exclude_linetypes: list[str] = field(default_factory=lambda: ["DASH", "PHANTOM"])
    small_area_threshold: float = 100.0
    text_height: float = 40.0
    low_confidence_threshold: float = 0.8
    notes: str = ""
    expected_counts: dict[str, int] = field(default_factory=dict)
    audit_ignore_rules: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DrawingProfile":
        known: dict[str, Any] = {}
        for field in fields(cls):
            if not field.init:
                continue
            if field.default is not MISSING:
                known[field.name] = field.default
            elif field.default_factory is not MISSING:
                known[field.name] = field.default_factory()
        known.update(
            {
                key: value
                for key, value in data.items()
                if key in cls.__dataclass_fields__
            }
        )
        return cls(**known)

    def compiled_label_pattern(self) -> re.Pattern[str]:
        return re.compile(self.label_pattern)

    def compiled_zone_label_pattern(self) -> re.Pattern[str]:
        return re.compile(self.zone_label_pattern)


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
    entity_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

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
            "entity_type": self.entity_type,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DrawingIssue":
        return cls(
            issue_id=data["issue_id"],
            severity=data["severity"],
            type=data["type"],
            entity_handle=data.get("entity_handle"),
            layer=data.get("layer", ""),
            coordinates=tuple(data["coordinates"]),
            message=data["message"],
            suggestion=data["suggestion"],
            entity_type=data.get("entity_type"),
            metadata=data.get("metadata", {}),
        )


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
            if number_hits == 0:
                results.append((profile, 0.0))
                continue
            score += number_hits / len(profile.number_layers) * 40.0

        profile_panel_layers = _profile_panel_layers(profile)
        if any(layer in layer_set for layer in profile_panel_layers):
            score += 20.0

        if profile.use_hatch:
            if fingerprint.get("has_hatch"):
                score += 20.0
        elif fingerprint.get("has_lwpolyline") and not fingerprint.get("has_hatch"):
            score += 20.0

        if any(layer in layer_set for layer in profile_panel_layers) and fingerprint.get("has_line"):
            score += 5.0

        # 同为 0 层 + 编号层 的图纸仍需区分是否构建孔洞层级：
        # 有 CIRCLE 的图纸倾向 build_hierarchy=True，无 CIRCLE 的平面图
        # 倾向 build_hierarchy=False，避免 countertop2/cgr45 这类画像同分串用。
        if fingerprint.get("has_circle"):
            if profile.build_hierarchy:
                score += 20.0
        elif not profile.build_hierarchy:
            score += 20.0

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
    return _is_closed(entity, tolerance)


def _profile_panel_layers(profile: DrawingProfile) -> set[str]:
    if profile.panel_layers:
        return set(profile.panel_layers)
    if profile.panel_layer:
        return {profile.panel_layer}
    return set()


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
    entity_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    entity_handle = None
    if entity is not None:
        entity_handle = getattr(entity.dxf, "handle", None)
        if entity_type is None:
            entity_type = entity.dxftype()
    metadata = metadata or {}
    if entity is not None and "vertex_count" not in metadata:
        try:
            if entity.dxftype() == "LWPOLYLINE":
                points = list(entity.get_points("xy"))
            elif entity.dxftype() == "POLYLINE":
                points = [
                    (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
                    for vertex in entity.vertices
                ]
            else:
                points = []
            metadata["vertex_count"] = len(points)
        except Exception:
            pass
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
            entity_type=entity_type,
            metadata=metadata,
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


def _hatch_polygon_from_paths(paths) -> tuple[Polygon, list[Polygon], Polygon] | None:
    path_polygons: list[Polygon] = []
    for path in paths:
        if hasattr(path, "vertices"):
            points = [
                (float(vertex[0]), float(vertex[1]))
                for vertex in path.vertices
                if len(vertex) >= 2
            ]
        else:
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
    return combined, holes, outer


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
    panel_layer_override: list[str] | None = None,
    exclude_layers: list[str] | None = None,
) -> list[dict[str, Any]]:
    panel_layer = profile.panel_layer or None
    panel_layers = (
        panel_layer_override
        or profile.panel_layers
        or ([panel_layer] if panel_layer else None)
    )
    allowed_panel_layers = set(panel_layers) if panel_layers else None
    excluded_layers = {value for value in (exclude_layers or [])}
    panels: list[dict[str, Any]] = []

    if profile.use_hatch:
        hatch_layer = profile.hatch_layer or None
        for entity in doc.modelspace():
            if entity.dxftype() != "HATCH":
                continue
            layer = getattr(entity.dxf, "layer", "0") or "0"
            if layer in excluded_layers:
                continue
            if hatch_layer is not None and layer != hatch_layer:
                continue
            result = _hatch_polygon_from_paths(entity.paths)
            if result is None:
                continue
            combined, holes, outer = result
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
                    "handle": getattr(entity.dxf, "handle", None),
                    "layer": layer,
                    "source": "hatch",
                    "confidence": 1.0,
                }
            )
        if panels:
            return panels

    polygon_items = extract_closed_polygons(
        doc,
        panel_layers=panel_layers,
        exclude_layers=exclude_layers,
        exclude_linetypes=profile.exclude_linetypes,
        closed_tolerance=profile.closed_tolerance,
        exclude_handles=set(profile.exclude_entity_handles),
    )
    handle_to_layer: dict[str, str] = {}
    for entity in doc.modelspace():
        handle = getattr(entity.dxf, "handle", None)
        if handle is not None:
            handle_to_layer[str(handle)] = getattr(entity.dxf, "layer", "0") or "0"

    if polygon_items:
        if profile.build_hierarchy:
            hierarchy = _build_part_hierarchy(polygon_items)
            for item in hierarchy:
                if item["parent"] != -1:
                    continue

                outer = item["poly"]
                holes = [hierarchy[child]["poly"] for child in item["children"]]
                hole_handles = [
                    hierarchy[child].get("handle") for child in item["children"]
                ]
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
                        "hole_handles": hole_handles,
                        "centroid": combined.centroid,
                        "area": combined.area,
                        "handle": item.get("handle"),
                        "layer": handle_to_layer.get(
                            str(item.get("handle")), profile.panel_layer or ""
                        ),
                        "source": "closed_polygon",
                        "confidence": 0.9,
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
                        "hole_handles": [],
                        "centroid": polygon.centroid,
                        "area": polygon.area,
                        "handle": handle,
                        "layer": handle_to_layer.get(
                            str(handle), profile.panel_layer or ""
                        ),
                        "source": "closed_polygon",
                        "confidence": 0.9,
                    }
                )

    line_entities = []
    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    allowed_panel_layers = _profile_panel_layers(profile)
    for entity in doc.modelspace():
        if entity.dxftype() != "LINE":
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers:
            continue
        if allowed_panel_layers and layer not in allowed_panel_layers:
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
                "hole_handles": [],
                "centroid": polygon.centroid,
                "area": polygon.area,
                "handle": None,
                "layer": profile.panel_layer or (
                    panel_layers[0] if panel_layers and len(panel_layers) == 1 else ""
                ),
                "source": "line",
                "confidence": 0.7,
            }
        )
    _assign_profile_holes(panels, doc, profile, excluded_layers)
    return panels


def _assign_profile_holes(
    panels: list[dict[str, Any]],
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    excluded_layers: set[str],
) -> None:
    hole_layers = set(profile.hole_layers)
    if not hole_layers or not panels:
        return

    assigned_handles: set[str] = set()
    for entity in doc.modelspace():
        dtype = entity.dxftype()
        if dtype == "CIRCLE":
            hole_polygon = _entity_to_polygon(entity)
        elif dtype in ("LWPOLYLINE", "POLYLINE"):
            if not _entity_is_closed(entity, profile.closed_tolerance):
                continue
            hole_polygon = _entity_to_polygon(entity)
        else:
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers or layer not in hole_layers:
            continue
        if hole_polygon is None or hole_polygon.is_empty or hole_polygon.area <= 0.01:
            continue
        handle = str(getattr(entity.dxf, "handle", None))

        for panel in panels:
            if panel["area"] <= hole_polygon.area + 1e-9:
                continue
            if handle in panel.get("hole_handles", []):
                break
            try:
                if not panel["outer_polygon"].contains(hole_polygon):
                    continue
            except Exception:
                continue

            combined = panel["polygon"].difference(hole_polygon)
            if isinstance(combined, MultiPolygon):
                combined = max(combined.geoms, key=lambda geom: geom.area)
            if combined.is_empty or combined.area <= 0.01:
                continue

            panel["holes"].append(hole_polygon)
            panel["hole_handles"] = panel.get("hole_handles", []) + [handle]
            panel["polygon"] = combined
            panel["area"] = combined.area
            panel["centroid"] = combined.centroid
            assigned_handles.add(handle)
            break

    if assigned_handles:
        panels[:] = [
            panel for panel in panels
            if str(panel.get("handle")) not in assigned_handles
        ]


def _collect_profile_number_texts(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    exclude_layers: list[str] | None = None,
) -> list[dict[str, Any]]:
    allowed_layers = set(profile.number_layers)
    excluded_layers = {value for value in (exclude_layers or [])}
    pattern = profile.compiled_label_pattern()
    texts: list[dict[str, Any]] = []

    for entity in doc.modelspace():
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers:
            continue
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


def _collect_zone_texts(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    exclude_layers: list[str] | None = None,
) -> list[dict[str, Any]]:
    """收集用于分区/中庭编号的标签，如 1#、2#。"""
    allowed_layers = set(profile.zone_layers)
    excluded_layers = {value for value in (exclude_layers or [])}
    pattern = profile.compiled_zone_label_pattern()
    texts: list[dict[str, Any]] = []

    for entity in doc.modelspace():
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers:
            continue
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
        match = pattern.match(text)
        if not match:
            continue
        zone = match.group("zone") if "zone" in match.groupdict() else match.group(0)
        texts.append(
            {
                "entity": entity,
                "text": zone,
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

    if profile.number_fallback_radius > 0:
        for panel in panels:
            panel_index = panel["index"]
            if assignments[panel_index]:
                continue
            centroid = panel["centroid"]
            best_index: int | None = None
            best_dist = profile.number_fallback_radius
            for text_index, text in enumerate(texts):
                if text_index in matched_texts:
                    continue
                dist = math.hypot(
                    float(centroid.x) - text["point"][0],
                    float(centroid.y) - text["point"][1],
                )
                if dist < best_dist:
                    best_dist = dist
                    best_index = text_index
            if best_index is not None:
                assignments[panel_index].append(best_index)
                matched_texts.add(best_index)

    return assignments, matched_texts


def _room_label_parts(
    text: str,
    profile: DrawingProfile,
) -> tuple[str, str] | None:
    value = " ".join(text.strip().split())
    match = profile.compiled_label_pattern().match(value)
    if not match:
        return None
    groups = match.groupdict()
    unit = groups.get("unit") or groups.get("material") or ""
    room = groups.get("room") or groups.get("shape") or ""
    unit = re.sub(r"\s+", "", unit)
    room = re.sub(r"\s+", "", room)
    if not unit:
        return None
    return unit, room


def _assign_room_numbers_to_panels(
    panels: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    profile: DrawingProfile,
) -> dict[int, str]:
    assignments: dict[int, str] = {}
    groups: dict[tuple[str, str], list[tuple[int, float, float]]] = {}

    for panel in panels:
        panel_index = panel["index"]
        centroid = panel["centroid"]
        cx = float(centroid.x)
        cy = float(centroid.y)
        best_text = None
        best_dist = profile.room_max_distance
        for text in texts:
            tx, ty = text["point"]
            dist = math.hypot(cx - tx, cy - ty)
            if dist < best_dist:
                best_dist = dist
                best_text = text
        if best_text is None:
            continue
        parts = _room_label_parts(best_text["text"], profile)
        if parts is None:
            continue
        unit, room = parts
        groups.setdefault((unit, room), []).append((panel_index, cx, cy))

    for (unit, room), items in groups.items():
        items.sort(key=lambda item: (item[1], item[2]))
        for index, (panel_index, _, _) in enumerate(items, start=1):
            assignments[panel_index] = profile.room_number_format.format(
                unit=unit,
                room=room,
                index=index,
            )

    return assignments


def _assign_numbers_by_panel_layer(
    panels: list[dict[str, Any]],
    profile: DrawingProfile,
    zone_texts: list[dict[str, Any]] | None = None,
) -> dict[int, str]:
    """按面板所在图层生成唯一编号。

    与最近文字标签模式不同，这里直接使用面板图层本身作为材料/类型来源，
    避免同一张图中多个材料标签距离过近时把面板串到错误的材料前缀。
    若提供 zone_texts，则先按最近分区标签再按材料分组。
    """
    assignments: dict[int, str] = {}
    groups: dict[tuple[str, str, str], list[tuple[int, float, float]]] = {}
    pattern = profile.compiled_label_pattern()

    for panel in panels:
        panel_index = panel["index"]
        layer = panel.get("layer") or profile.panel_layer or ""
        match = pattern.match(layer)
        if match:
            groups_dict = match.groupdict()
            unit = groups_dict.get("unit") or groups_dict.get("material") or layer
            room = groups_dict.get("room") or groups_dict.get("shape") or ""
        else:
            unit = layer
            room = ""
        unit = re.sub(r"\s+", "", unit)
        room = re.sub(r"\s+", "", room)

        centroid = panel["centroid"]
        zone = ""
        if zone_texts:
            cx = float(centroid.x)
            cy = float(centroid.y)
            best_zone = None
            best_dist = profile.zone_max_distance
            for text in zone_texts:
                tx, ty = text["point"]
                dist = math.hypot(cx - tx, cy - ty)
                if dist < best_dist:
                    best_dist = dist
                    best_zone = text["text"]
            if best_zone is not None:
                zone = best_zone

        groups.setdefault((zone, unit, room), []).append(
            (panel_index, float(centroid.x), float(centroid.y))
        )

    for (zone, unit, room), items in groups.items():
        items.sort(key=lambda item: (item[1], item[2]))
        for index, (panel_index, _, _) in enumerate(items, start=1):
            assignments[panel_index] = profile.room_number_format.format(
                zone=zone,
                unit=unit,
                room=room,
                index=index,
            )

    return assignments


def read_dxf_with_profile(
    filepath: str | Path,
    profile: DrawingProfile,
    panel_layers: list[str] | None = None,
    exclude_layers: list[str] | None = None,
) -> tuple[list[dict[str, Any]], ezdxf.document.Drawing]:
    """按图纸画像读取 DXF 并返回与 read_dxf 相同结构的 parts_data。

    面板来源优先使用画像中的 HATCH/LINE 规则；编号归属使用
    point_then_bbox：编号插入点优先，其次用文字包围盒与面板的重叠比例补配。
    """
    filepath = Path(filepath)
    doc = ezdxf.readfile(str(filepath))
    panels = _extract_profile_panels(
        doc,
        profile,
        panel_layer_override=panel_layers,
        exclude_layers=exclude_layers,
    )
    texts = _collect_profile_number_texts(
        doc,
        profile,
        exclude_layers=exclude_layers,
    )
    zone_texts = _collect_zone_texts(
        doc,
        profile,
        exclude_layers=exclude_layers,
    )
    room_assignments = None
    panel_layer_assignments = None
    assignments = None
    if profile.assignment_mode == "nearest_room":
        room_assignments = _assign_room_numbers_to_panels(panels, texts, profile)
    elif profile.assignment_mode == "panel_layer":
        panel_layer_assignments = _assign_numbers_by_panel_layer(
            panels,
            profile,
            zone_texts=zone_texts or None,
        )
    else:
        assignments, _ = _assign_texts_to_panels(panels, texts, profile)

    parts_data: list[dict[str, Any]] = []
    for panel in panels:
        if room_assignments is not None:
            original_number = room_assignments.get(panel["index"])
        elif panel_layer_assignments is not None:
            original_number = panel_layer_assignments.get(panel["index"])
        else:
            text_indexes = assignments.get(panel["index"], []) if assignments else []
            original_number = None
            if text_indexes:
                original_number = _normalize_label(
                    texts[text_indexes[0]]["text"],
                    profile,
                )

        centroid = panel["centroid"]
        parts_data.append(
            {
                "polygon": panel["polygon"],
                "outer_polygon": panel["outer_polygon"],
                "holes": panel["holes"],
                "centroid": (float(centroid.x), float(centroid.y)),
                "area": panel["area"],
                "original_number": original_number,
                "index": panel["index"],
                "outer_handle": panel.get("handle"),
                "hole_handles": panel.get("hole_handles", []),
                "layer": panel.get("layer"),
            }
        )
    return parts_data, doc


def _add_entity_filter_issues(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    excluded_types = set(profile.exclude_entity_types)

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
def _add_unclosed_polyline_issues(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    allowed_panel_layers = _profile_panel_layers(profile)
    for entity in doc.modelspace():
        if entity.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if allowed_panel_layers and layer not in allowed_panel_layers:
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
    allowed_panel_layers = _profile_panel_layers(profile)
    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    for entity in doc.modelspace():
        if entity.dxftype() != "LINE":
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if allowed_panel_layers and layer not in allowed_panel_layers:
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
                type_="open_chain",
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
    allowed_panel_layers = _profile_panel_layers(profile)
    for entity in doc.modelspace():
        if entity.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if allowed_panel_layers and layer not in allowed_panel_layers:
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
            if _is_recoverable_closed(entity, profile.closed_tolerance):
                continue
            fixed = raw.buffer(0)
            if fixed.is_empty or fixed.area <= 0.01:
                _make_issue(
                    issues,
                    severity="error",
                    type_="self_intersecting_geometry",
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
                    type_="self_intersecting_geometry",
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


def _add_hole_outside_panel_issues(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    panels: list[dict[str, Any]],
    issues: list[DrawingIssue],
) -> None:
    if not panels:
        return

    excluded_types = set(profile.exclude_entity_types)
    hole_layers = set(profile.hole_layers)
    for entity in doc.modelspace():
        if entity.dxftype() != "CIRCLE":
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if hole_layers and layer not in hole_layers:
            continue
        if "CIRCLE" in excluded_types:
            continue

        hole_polygon = _entity_to_polygon(entity)
        if hole_polygon is None or hole_polygon.is_empty or hole_polygon.area <= 0.01:
            continue

        contained = False
        for panel in panels:
            if panel["area"] <= hole_polygon.area + 1e-9:
                continue
            try:
                if panel["outer_polygon"].contains(hole_polygon):
                    contained = True
                    break
            except Exception:
                continue

        if not contained:
            _make_issue(
                issues,
                severity="warning",
                type_="hole_outside_panel",
                entity=entity,
                layer=layer,
                coordinates=_entity_coordinates(entity),
                message="CIRCLE 疑似孔洞，但未包含在任何面板外轮廓内",
                suggestion="检查孔洞位置、面板边界，或删除多余圆",
            )


def _add_panel_confidence_issues(
    panels: list[dict[str, Any]],
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    source_labels = {
        "hatch": "HATCH",
        "closed_polygon": "封闭图形",
        "line": "LINE 线段",
    }
    for panel in panels:
        confidence = float(panel.get("confidence", 1.0))
        if confidence >= profile.low_confidence_threshold:
            continue
        source = panel.get("source", "unknown")
        label = source_labels.get(source, source)
        centroid = panel["centroid"]
        _make_issue(
            issues,
            severity="warning",
            type_="low_confidence_entity",
            entity=None,
            layer=profile.panel_layer or "",
            coordinates=(float(centroid.x), float(centroid.y)),
            message=f"面板由 {label} 提取，归属置信度较低 ({confidence:.2f})",
            suggestion="请人工确认面板边界、孔洞归属及是否需要排板",
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


def _add_expected_count_issues(
    panels: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    assignments: dict[int, list[int]],
    profile: DrawingProfile,
    issues: list[DrawingIssue],
    actual_numbers: set[str] | None = None,
) -> None:
    if not profile.expected_counts:
        return

    if actual_numbers is not None:
        numbers = len(actual_numbers)
    else:
        numbers = len(
            {
                _normalize_label(texts[indexes[0]]["text"], profile)
                for indexes in assignments.values()
                if indexes
            }
        )
    actual = {
        "panels": len(panels),
        "holes": sum(len(panel["holes"]) for panel in panels),
        "numbers": numbers,
    }
    centroid = panels[0]["centroid"] if panels else None
    coordinates = (
        (float(centroid.x), float(centroid.y))
        if centroid is not None
        else (0.0, 0.0)
    )
    for key, expected in profile.expected_counts.items():
        if key not in actual:
            continue
        if actual[key] == expected:
            continue
        _make_issue(
            issues,
            severity="warning",
            type_="expected_count_mismatch",
            entity=None,
            layer=profile.panel_layer or "",
            coordinates=coordinates,
            message=(
                f"画像预期 {key}={expected}，实际 {actual[key]}"
            ),
            suggestion="确认图纸版本、画像匹配，或检查是否新增/删除了面板、孔洞或编号",
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
        for text_index in assignments.get(panel_index, []):
            text = texts[text_index]
            point = Point(text["point"][0], text["point"][1])
            try:
                if not panel["polygon"].contains(point):
                    _make_issue(
                        issues,
                        severity="warning",
                        type_="number_outside_panel",
                        entity=text["entity"],
                        layer=text["layer"],
                        coordinates=text["point"],
                        message=f"编号 '{text['text']}' 的插入点不在面板内",
                        suggestion="确认编号插入点/引线归属；若编号确实属于该面板，可标记已接受",
                    )
            except Exception:
                continue
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

    label_to_panels: dict[str, list[tuple[float, float]]] = {}
    for panel in panels:
        panel_index = panel["index"]
        for text_index in assignments.get(panel_index, []):
            label = _normalize_label(texts[text_index]["text"], profile)
            centroid = panel["centroid"]
            label_to_panels.setdefault(label, []).append(
                (float(centroid.x), float(centroid.y))
            )

    for label, panel_coordinates in label_to_panels.items():
        if len(panel_coordinates) <= 1:
            continue
        x, y = panel_coordinates[0]
        _make_issue(
            issues,
            severity="warning",
            type_="duplicate_label",
            entity=None,
            layer=profile.panel_layer or "",
            coordinates=(x, y),
            message=f"编号 '{label}' 出现在 {len(panel_coordinates)} 个面板中",
            suggestion="确认相同编号是否需要合并数量，或改为唯一编号",
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


def _add_room_assignment_issues(
    panels: list[dict[str, Any]],
    assignments: dict[int, str],
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    """Validate nearest-room numbering generated from room labels."""

    number_to_panels: dict[str, list[tuple[float, float]]] = {}
    for panel in panels:
        panel_index = panel["index"]
        number = assignments.get(panel_index)
        if not number:
            centroid = panel["centroid"]
            _make_issue(
                issues,
                severity="warning",
                type_="panel_without_number",
                entity=None,
                layer=profile.panel_layer or "",
                coordinates=(float(centroid.x), float(centroid.y)),
                message="面板没有匹配到户型编号",
                suggestion="请确认户型标签位置，或调整 room_max_distance",
            )
            continue
        centroid = panel["centroid"]
        number_to_panels.setdefault(number, []).append(
            (float(centroid.x), float(centroid.y))
        )

    for number, panel_coordinates in number_to_panels.items():
        if len(panel_coordinates) <= 1:
            continue
        x, y = panel_coordinates[0]
        _make_issue(
            issues,
            severity="warning",
            type_="duplicate_label",
            entity=None,
            layer=profile.panel_layer or "",
            coordinates=(x, y),
            message=f"户型编号 '{number}' 出现在 {len(panel_coordinates)} 个面板中",
            suggestion="检查户型标签是否重复，或调整 room_max_distance",
        )


def _add_material_conflict_issues(
    panels: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    assignments: dict[int, list[int]],
    profile: DrawingProfile,
    issues: list[DrawingIssue],
) -> None:
    if not profile.material_group_enabled:
        return

    allowed = {value.upper() for value in profile.allowed_material_prefixes}
    for panel in panels:
        panel_index = panel["index"]
        for text_index in assignments.get(panel_index, []):
            text = texts[text_index]
            match = re.match(profile.material_prefix_pattern, text["text"])
            if match and "prefix" in match.groupdict():
                prefix = (match.group("prefix") or "").upper()
            else:
                prefix = match.group(0).upper() if match else ""

            if not prefix or (allowed and prefix not in allowed):
                _make_issue(
                    issues,
                    severity="warning",
                    type_="material_conflict",
                    entity=text["entity"],
                    layer=text["layer"],
                    coordinates=text["point"],
                    message=(
                        f"编号 '{text['text']}' 的材料前缀 "
                        f"'{prefix or '未知'}' 不在允许集合内"
                    ),
                    suggestion="确认材料编号，或更新画像 allowed_material_prefixes",
                )


def _apply_audit_ignore_rules(
    issues: list[DrawingIssue],
    profile: DrawingProfile,
) -> list[DrawingIssue]:
    """Apply profile-specific audit suppression rules.

    Supported rule keys:
    - issue_type: str | list[str]
    - entity_type: str | list[str]
    - layer: str | list[str]
    - entity_handle: str | list[str]
    - message_contains: str | list[str]
    - vertex_count_max: int
    - action: "ignore" | "downgrade"

    ignore removes the issue; downgrade lowers severity to info.
    """
    filtered: list[DrawingIssue] = []
    for issue in issues:
        action = None
        for rule in profile.audit_ignore_rules:
            if _issue_matches_rule(issue, rule):
                action = rule.get("action", "ignore")
                break
        if action == "ignore":
            continue
        if action == "downgrade":
            issue = replace(issue, severity="info")
        filtered.append(issue)

    for index, issue in enumerate(filtered, start=1):
        if issue.issue_id != index:
            filtered[index - 1] = replace(issue, issue_id=index)
    return filtered


def _issue_matches_rule(issue: DrawingIssue, rule: dict[str, Any]) -> bool:
    def _values_match(actual: Any, expected: Any) -> bool:
        if expected is None:
            return True
        if isinstance(expected, list):
            return actual in expected
        return actual == expected

    if not _values_match(issue.type, rule.get("issue_type")):
        return False
    if not _values_match(issue.entity_type, rule.get("entity_type")):
        return False
    if not _values_match(issue.layer, rule.get("layer")):
        return False
    if not _values_match(issue.entity_handle, rule.get("entity_handle")):
        return False
    if "message_contains" in rule:
        expected = rule.get("message_contains")
        haystack = issue.message or ""
        if isinstance(expected, list):
            if not any(value in haystack for value in expected):
                return False
        elif expected not in haystack:
            return False
    if "vertex_count_max" in rule:
        vertex_count = issue.metadata.get("vertex_count")
        if not isinstance(vertex_count, int) or vertex_count > rule["vertex_count_max"]:
            return False
    return True


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
    _add_hole_outside_panel_issues(doc, profile, panels, issues)
    _add_panel_confidence_issues(panels, profile, issues)

    texts = _collect_profile_number_texts(doc, profile)
    zone_texts = _collect_zone_texts(doc, profile)
    _add_duplicate_text_issues(texts, issues)
    if profile.assignment_mode == "nearest_room":
        room_assignments = _assign_room_numbers_to_panels(panels, texts, profile)
        _add_expected_count_issues(
            panels,
            texts,
            {},
            profile,
            issues,
            actual_numbers=set(room_assignments.values()),
        )
        _add_room_assignment_issues(
            panels,
            room_assignments,
            profile,
            issues,
        )
    elif profile.assignment_mode == "panel_layer":
        panel_layer_assignments = _assign_numbers_by_panel_layer(
            panels,
            profile,
            zone_texts=zone_texts or None,
        )
        _add_expected_count_issues(
            panels,
            texts,
            {},
            profile,
            issues,
            actual_numbers=set(panel_layer_assignments.values()),
        )
        _add_room_assignment_issues(
            panels,
            panel_layer_assignments,
            profile,
            issues,
        )
    else:
        assignments, matched_texts = _assign_texts_to_panels(
            panels,
            texts,
            profile,
        )
        _add_expected_count_issues(panels, texts, assignments, profile, issues)
        _add_number_assignment_issues(
            panels,
            texts,
            assignments,
            matched_texts,
            profile,
            issues,
        )
        _add_material_conflict_issues(
            panels,
            texts,
            assignments,
            profile,
            issues,
        )
    return _apply_audit_ignore_rules(issues, profile)


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
    "self_intersecting_geometry": "自相交",
    "open_chain": "开放链",
    "duplicate_text": "重复文字",
    "duplicate_label": "重复编号",
    "duplicate_geometry": "重复图形",
    "panel_without_number": "缺编号",
    "number_without_panel": "编号无面板",
    "number_outside_panel": "编号在面板外",
    "hole_outside_panel": "孔洞在面板外",
    "conflicting_number_in_panel": "编号冲突",
    "suspicious_small_area": "面积过小",
    "non_panel_text": "非编号文字",
    "low_confidence_entity": "低置信度实体",
    "material_conflict": "材料冲突",
}


def write_audit_dxf(
    source_filepath: str | Path,
    issues: list[DrawingIssue],
    output_path: str | Path,
) -> Path:
    """在原始 DXF 上叠加问题标记，并生成问题清单布局。"""
    source_filepath = Path(source_filepath)
    output_path = Path(output_path)
    doc, _auditor = ezdxf.recover.readfile(str(source_filepath))

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
