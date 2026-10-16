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

from .entity_classifier import classify_candidates
from .recognition_memory import recognition_memory_hints
from .dxf_reader import (
    _build_part_hierarchy,
    _entity_to_polygon,
    _is_contained_as_hole,
    _is_closed,
    _is_recoverable_closed,
    _linetype_name,
    _lwpolyline_points,
    attach_source_curves,
    extract_closed_entity_candidates,
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
    group_id_pattern: str | None = None
    first_part_left_edge: bool = False
    assignment_mode: str = "point_then_bbox"
    room_number_format: str = "{unit}-{room}-{index:02d}"
    room_max_distance: float = 5000.0
    # 区域轮廓（房间/户型/大区域）的最小边阈值 mm：封闭图形**最小外接边**
    # 达到该值的视为区域轮廓，不作为规格板；它们仍参与编号归属
    # （`assignment_mode="containing_region"`）。0 = 不启用。
    region_min_side_mm: float = 0.0
    # 列头材料标签（多层平面图：x 方向每列 = 一个楼层，列头给出
    # `L-XX LEVEL` + `材料：ST-x`）。启用后读图会给每个零件写入
    # material_group（取自它所在列），原位 DXF 也据此标注材料号。
    # 见 docs/drawing-profile-citizen.md。
    material_column_enabled: bool = False
    material_layers: list[str] = field(default_factory=list)
    material_label_pattern: str = (
        r"材料\s*[:：]\s*(?P<materials>[A-Za-z0-9\-/、,;；\s]+)"
    )
    level_label_pattern: str = r"^(?P<level>L-\d+)\s*LEVEL$"
    number_fallback_radius: float = 0.0
    bbox_overlap_threshold: float = 0.5
    build_hierarchy: bool = True
    conflict_resolution: str = "random"
    closed_tolerance: float = 0.01
    exclude_entity_types: list[str] = field(default_factory=list)
    exclude_entity_handles: list[str] = field(default_factory=list)
    # 按 ACI 颜色排除"标注/示意线"：如磨边示意线（M_LINE 颜色 2）、X5_LINE 颜色 6、
    # R_LINE 颜色 3。雪花白洗手台就是靠颜色区分"零件外框（BYLAYER 7）"与
    # "磨边/示意线"——不排除时，示意线与外框相交的角落会被当成缺口（立板两端
    # 各被切掉一块 20x20）。256 = BYLAYER，一般不要放进这个列表。
    exclude_colors: list[int] = field(default_factory=list)
    # 显式排除的图层（几何层面）：区域用料指示面板、重复算量图层、定位层等
    # 明确"不是规格板"的图形所在层。读图时不提取、不参与孔洞归属，
    # 也不给分类器"凭几何捡回来"的机会（hint 模式下同样生效）。
    # 只影响几何提取，不影响编号/区域/列头材料等文字收集。
    exclude_layers: list[str] = field(default_factory=list)
    exclude_linetypes: list[str] = field(default_factory=lambda: ["DASH", "PHANTOM"])
    small_area_threshold: float = 100.0
    text_height: float = 40.0
    low_confidence_threshold: float = 0.8
    grid_mode: bool = False
    grid_layer: str | None = None
    grid_min_line_length: float = 800.0
    grid_snap_tolerance: float = 10.0
    grid_min_area: float = 100000.0
    grid_max_area: float = 5000000.0
    grid_label_margin: float = 1000.0
    grid_split_layers: list[str] = field(default_factory=list)
    network_mode: bool = False
    network_layers: list[str] = field(default_factory=list)
    network_min_area: float = 1000.0
    network_max_area: float = 50000000.0
    network_gap_side_tolerance: float = 3.0
    network_arc_flattening: float = 1.0
    network_snap_tolerance: float = 1.5
    # 双线绘制图纸：台面/挡水等按**外框**成件，外框内侧 20~26mm 处的平行线
    # 只是"边缘加厚"的界线（不是孔洞、也不是另一件）。线网络读图会把这样一件
    # 拆成"内芯 + 一圈薄片"（雪花白洗手台：792x100 被拆成 752x80 内芯 +
    # 752x20 顶条 + 20x80 端帽 x2；800x430 台面被拆成 774x405 内芯 + 加厚带）。
    # 本阈值（mm）启用"薄片并回相邻零件、取并集外轮廓"的合并；0 = 关闭（默认）。
    # 见 AGENTS.md「双线图纸：外框成件」。
    band_face_merge_mm: float = 0.0
    # 线网络模式默认**不做编号归属**（避免把图面零碎文字乱挂到碎片上）；
    # 声明显式 True 时按 assignment_mode 正常匹配（雪花白洗手台：XST-1..6
    # 各落在一件里，是图纸自己给出的件定义）。
    network_assign_numbers: bool = False
    notes: str = ""
    expected_counts: dict[str, int] = field(default_factory=dict)
    # 已验证图纸指纹（可选）。填写后 _rank 会把指纹相似度当作主排序信号，
    # 避免"图层规则相同 -> 同分 -> 按文件名字典序"的静默误配。
    fingerprint: dict[str, Any] = field(default_factory=dict)
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


def fingerprint_from_doc(doc, source_path: str | Path) -> dict[str, Any]:
    """从已打开的 DXF 文档生成图纸指纹。

    与 :func:`analyze_drawing` 同源：后者只是"打开文件再调用本函数"的薄封装。
    已经拿在手里的文档（回填、画像提议）可直接复用，避免为大图重复读一遍。
    """
    entities = list(doc.modelspace())

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
        "path": str(source_path),
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


def analyze_drawing(filepath: str | Path) -> dict[str, Any]:
    """生成图纸指纹，用于后续画像匹配。"""
    filepath = Path(filepath)
    doc = ezdxf.readfile(str(filepath))
    return fingerprint_from_doc(doc, filepath)


def _fingerprint_jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _fingerprint_keys(mapping: dict[str, Any] | None) -> set[str]:
    return {str(key) for key in (mapping or {})}


# 画像指纹相似度达到该阈值才认为"这张画像就是给这张图纸写的"，从而进入
# 优先层；低于阈值不加分，避免某个画像凭指纹对所有图纸都有额外优势。
FINGERPRINT_MATCH_THRESHOLD = 60.0
# 指纹命中层的固定偏移：确保"指纹命中"优先于"仅图层规则命中"。
FINGERPRINT_TIER_BONUS = 100.0


def fingerprint_similarity(
    fingerprint: dict[str, Any],
    reference: dict[str, Any],
) -> float:
    """两个图纸指纹的相似度（0-100）。

    用于画像排序与已验证案例匹配：只看结构（图层集合、实体类型集合、线型集合
    与布尔特征），不比较具体数量，因此对同项目不同楼层的图纸仍然稳定。
    """
    if not reference:
        return 0.0

    score = _fingerprint_jaccard(
        _fingerprint_keys(fingerprint.get("layer_counts")),
        _fingerprint_keys(reference.get("layer_counts")),
    ) * 50.0
    score += _fingerprint_jaccard(
        _fingerprint_keys(fingerprint.get("entity_counts")),
        _fingerprint_keys(reference.get("entity_counts")),
    ) * 20.0
    score += _fingerprint_jaccard(
        _fingerprint_keys(fingerprint.get("linetype_counts")),
        _fingerprint_keys(reference.get("linetype_counts")),
    ) * 15.0

    # 结构性布尔特征必须"不一致就扣分"，不能只是不加分：
    # 集合型 Jaccard 对同族图纸过于宽松——CGR45（有 CIRCLE 孔洞）与
    # countertop2（无孔洞）的图层/实体集合大面积重合，只靠集合相似度会得到
    # 64.8 分，从而让 cgr45 画像抢走 countertop2 的图纸。
    # 有无 CIRCLE（盆孔/龙头孔）是工艺上最有区分度的结构特征：CGR45 类台面
    # 图纸有 153 个圆孔，countertop2 类平面板一个都没有，因此不一致时重罚，
    # 保证两类画像不会互相抢占（否则相似度只差 0.2 分，阈值形同虚设）。
    score += 5.0 if fingerprint.get("has_circle") == reference.get("has_circle") else -15.0
    for key in ("has_lwpolyline", "has_line"):
        score += 5.0 if fingerprint.get(key) == reference.get(key) else -5.0
    if fingerprint.get("dxf_version") == reference.get("dxf_version"):
        score += 5.0
    if fingerprint.get("insunits") == reference.get("insunits"):
        score += 5.0

    return max(0.0, min(100.0, score))


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

        # 画像自带已验证指纹且相似度达标时进入优先层：仅靠"图层名命中"会让
        # 同一类图纸（0 层 + 编号层 + 建层级）同分，并列后按文件名字典序选一个
        # （CGR45 曾被误配成 canada3l）。低于阈值则完全不加分，否则该画像会对
        # 所有结构相近的图纸都产生虚假优势（countertop2 曾被误配成 cgr45）。
        if profile.fingerprint:
            similarity = fingerprint_similarity(
                fingerprint, profile.fingerprint
            )
            if similarity >= FINGERPRINT_MATCH_THRESHOLD:
                score += FINGERPRINT_TIER_BONUS + similarity * 0.5

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
    raw = " ".join(text.strip().split())
    value = raw.upper()
    match = profile.compiled_label_pattern().match(value)
    if match:
        groups = match.groupdict()
        if "material" in groups and "shape" in groups:
            return f"{groups['material'].upper()}-{groups['shape']}"
    return raw


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


def _cluster_1d(values: list[float], tolerance: float) -> list[float]:
    """Cluster nearby 1D coordinates into representative grid lines."""
    if not values:
        return []
    ordered = sorted(values)
    clusters: list[list[float]] = []
    current = [ordered[0]]
    for value in ordered[1:]:
        if abs(value - current[-1]) <= tolerance:
            current.append(value)
        else:
            clusters.append(current)
            current = [value]
    clusters.append(current)
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _extract_grid_panels(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    panel_layers: list[str] | None,
    excluded_layers: set[str],
) -> list[dict[str, Any]]:
    """Reconstruct axis-aligned rectangular panels from LWPOLYLINE grid lines.

    Some CAD/PDF imports draw panel grids as many open two-point LWPOLYLINEs
    instead of closed polygons. This mode collects long horizontal/vertical
    LWPOLYLINE segments, clusters their coordinates into grid lines, forms
    adjacent rectangles, and keeps only rectangles containing exactly one
    number label.
    """
    layer_names = {profile.grid_layer} if profile.grid_layer else set(
        panel_layers or _profile_panel_layers(profile)
    )
    if not layer_names:
        return []

    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    axis_tolerance = max(profile.closed_tolerance, 0.5)
    segments: list[tuple[str, tuple[tuple[float, float], tuple[float, float]]]] = []

    for entity in doc.modelspace():
        if entity.dxftype() != "LWPOLYLINE":
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers or layer not in layer_names:
            continue
        linetype = _linetype_name(entity, doc).upper()
        if linetype in excluded_linetypes:
            continue

        points = _lwpolyline_points(entity)
        segment_pairs = list(zip(points, points[1:]))
        if entity.closed and len(points) >= 3:
            segment_pairs.append((points[-1], points[0]))

        for start, end in segment_pairs:
            dx = abs(start[0] - end[0])
            dy = abs(start[1] - end[1])
            if dx <= axis_tolerance and dy >= profile.grid_min_line_length:
                segments.append(("v", (start, end)))
            elif dy <= axis_tolerance and dx >= profile.grid_min_line_length:
                segments.append(("h", (start, end)))

    number_texts = _collect_profile_number_texts(
        doc,
        profile,
        exclude_layers=sorted(excluded_layers),
    )
    if not number_texts:
        return []

    label_margin = profile.grid_label_margin
    label_points = [text["point"] for text in number_texts]
    label_min_x = min(point[0] for point in label_points) - label_margin
    label_max_x = max(point[0] for point in label_points) + label_margin
    label_min_y = min(point[1] for point in label_points) - label_margin
    label_max_y = max(point[1] for point in label_points) + label_margin

    grid_min_x = float("inf")
    grid_max_x = float("-inf")
    grid_min_y = float("inf")
    grid_max_y = float("-inf")
    bounded_segments: list[tuple[str, tuple[tuple[float, float], tuple[float, float]]]] = []
    for direction, (start, end) in segments:
        min_x, max_x = sorted((start[0], end[0]))
        min_y, max_y = sorted((start[1], end[1]))
        if max_x < label_min_x or min_x > label_max_x:
            continue
        if max_y < label_min_y or min_y > label_max_y:
            continue
        bounded_segments.append((direction, (start, end)))
        grid_min_x = min(grid_min_x, min_x)
        grid_max_x = max(grid_max_x, max_x)
        grid_min_y = min(grid_min_y, min_y)
        grid_max_y = max(grid_max_y, max_y)

    # Keep only full-span grid lines. Internal decorative lines in this
    # drawing are shorter than 80% of the overall grid extent; retaining them
    # would split real cells into false smaller rectangles.
    min_horizontal_span = (grid_max_x - grid_min_x) * 0.8
    min_vertical_span = (grid_max_y - grid_min_y) * 0.8

    xs: list[float] = []
    ys: list[float] = []
    for direction, (start, end) in bounded_segments:
        min_x, max_x = sorted((start[0], end[0]))
        min_y, max_y = sorted((start[1], end[1]))
        if direction == "h":
            if max_x - min_x < min_horizontal_span:
                continue
            ys.append((start[1] + end[1]) / 2.0)
        else:
            if max_y - min_y < min_vertical_span:
                continue
            xs.append((start[0] + end[0]) / 2.0)

    xs = _cluster_1d(xs, profile.grid_snap_tolerance)
    ys = _cluster_1d(ys, profile.grid_snap_tolerance)
    xs.sort()
    ys.sort()

    split_layer_names = set(profile.grid_split_layers) if profile.grid_split_layers else set()
    candidate_layers = set(layer_names) | split_layer_names
    candidate_polys: list[tuple[Polygon, str, str]] = []
    for entity in doc.modelspace():
        if entity.dxftype() != "LWPOLYLINE":
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers or layer not in candidate_layers:
            continue
        linetype = _linetype_name(entity, doc).upper()
        if linetype in excluded_linetypes:
            continue
        if not entity.closed:
            continue
        points = _lwpolyline_points(entity)
        if len(points) < 3:
            continue
        try:
            polygon = Polygon(points)
            if polygon.is_empty:
                continue
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.is_empty or polygon.geom_type != "Polygon":
                continue
        except Exception:
            continue
        candidate_polys.append(
            (polygon, layer, str(getattr(entity.dxf, "handle", "") or ""))
        )

    output_layer = (
        profile.grid_layer
        or profile.panel_layer
        or (panel_layers[0] if panel_layers else "")
    )

    def split_panels_for_cell(
        rectangle: Polygon,
        label: str,
    ) -> list[dict[str, Any]] | None:
        cell_candidates: list[tuple[Polygon, str, str]] = []
        for polygon, layer, handle in candidate_polys:
            if polygon.area < profile.grid_min_area:
                continue
            if polygon.area > rectangle.area * 0.98:
                continue
            try:
                if not rectangle.contains(polygon.centroid):
                    continue
                if rectangle.intersection(polygon).area < polygon.area * 0.95:
                    continue
            except Exception:
                continue
            cell_candidates.append((polygon, layer, handle))

        if len(cell_candidates) < 2:
            return None

        cell_candidates.sort(
            key=lambda item: (
                0 if item[1] == output_layer else 1,
                -item[0].area,
            )
        )
        deduped: list[tuple[Polygon, str, str]] = []
        for polygon, layer, handle in cell_candidates:
            duplicate = False
            for kept, _, _ in deduped:
                try:
                    intersection_area = polygon.intersection(kept).area
                except Exception:
                    intersection_area = 0.0
                denominator = min(polygon.area, kept.area)
                if denominator > 0 and intersection_area / denominator >= 0.9:
                    duplicate = True
                    break
            if not duplicate:
                deduped.append((polygon, layer, handle))

        if len(deduped) != 2:
            return None

        try:
            union = unary_union([deduped[0][0], deduped[1][0]])
            coverage = union.intersection(rectangle).area / rectangle.area
        except Exception:
            coverage = 0.0
        if coverage < 0.90:
            return None

        ordered = sorted(
            deduped,
            key=lambda item: (
                float(item[0].centroid.x),
                float(item[0].centroid.y),
            ),
        )
        parts: list[dict[str, Any]] = []
        for sub_index, (polygon, layer, handle) in enumerate(ordered):
            centroid = polygon.centroid
            parts.append(
                {
                    "index": len(panels) + len(parts),
                    "polygon": polygon,
                    "outer_polygon": polygon,
                    "holes": [],
                    "hole_handles": [],
                    "centroid": centroid,
                    "area": polygon.area,
                    "handle": handle,
                    "layer": output_layer,
                    "source": "grid_split",
                    "confidence": 0.9,
                    "grid_column": x_index,
                    "grid_row": y_index,
                    "original_number": f"{label}{chr(97 + sub_index)}",
                }
            )
        return parts

    panels: list[dict[str, Any]] = []
    for x_index in range(len(xs) - 1):
        for y_index in range(len(ys) - 1):
            min_x, max_x = xs[x_index], xs[x_index + 1]
            min_y, max_y = ys[y_index], ys[y_index + 1]
            rectangle = Polygon(
                [
                    (min_x, min_y),
                    (max_x, min_y),
                    (max_x, max_y),
                    (min_x, max_y),
                ]
            )
            if not rectangle.is_valid:
                rectangle = rectangle.buffer(0)
            if rectangle.is_empty:
                continue
            if not (profile.grid_min_area <= rectangle.area <= profile.grid_max_area):
                continue

            inside = [
                text
                for text in number_texts
                if rectangle.contains(Point(text["point"][0], text["point"][1]))
            ]
            if len(inside) != 1:
                continue

            split_parts = split_panels_for_cell(
                rectangle,
                inside[0]["text"],
            )
            if split_parts is not None:
                panels.extend(split_parts)
                continue

            centroid = rectangle.centroid
            panels.append(
                {
                    "index": len(panels),
                    "polygon": rectangle,
                    "outer_polygon": rectangle,
                    "holes": [],
                    "hole_handles": [],
                    "centroid": centroid,
                    "area": rectangle.area,
                    "handle": None,
                    "layer": output_layer,
                    "source": "grid",
                    "confidence": 0.95,
                    "grid_column": x_index,
                    "grid_row": y_index,
                    "original_number": inside[0]["text"],
                }
            )

    return panels


def _snap_line_endpoints(
    lines: list[LineString],
    tolerance: float,
) -> list[LineString]:
    """Snap nearby line endpoints into a common coordinate.

    CAD/PDF exports often leave tiny gaps between adjacent LINE segments.
    This clusters endpoints whose distance is within ``tolerance`` and
    rewrites the line coordinates to the cluster representative before
    polygonization, so near-closed cells are not lost.
    """
    if tolerance <= 0 or not lines:
        return lines

    unique_points: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for line in lines:
        for x, y in line.coords:
            key = (float(x), float(y))
            if key not in seen:
                seen.add(key)
                unique_points.append(key)

    if len(unique_points) < 2:
        return lines

    point_geoms = [Point(x, y) for x, y in unique_points]
    tree = STRtree(point_geoms)
    parent = list(range(len(unique_points)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    tolerance_sq = tolerance * tolerance
    for index, (x, y) in enumerate(unique_points):
        search_area = Point(x, y).buffer(tolerance)
        for candidate in tree.query(search_area):
            candidate = int(candidate)
            if candidate <= index:
                continue
            cx, cy = unique_points[candidate]
            if (cx - x) ** 2 + (cy - y) ** 2 <= tolerance_sq:
                union(index, candidate)

    representatives: dict[tuple[float, float], tuple[float, float]] = {}
    for index, point in enumerate(unique_points):
        representatives[point] = unique_points[find(index)]

    snapped_lines: list[LineString] = []
    for line in lines:
        snapped_lines.append(
            LineString(
                [
                    representatives[(float(x), float(y))]
                    for x, y in line.coords
                ]
            )
        )
    return snapped_lines


def _merge_band_faces(
    panels: list[dict[str, Any]],
    thin_mm: float,
    gap_mm: float = 0.5,
    hollow_ratio: float = 0.3,
) -> list[dict[str, Any]]:
    """双线图纸：把被拆碎的"薄片"并回它所属的零件。

    双线绘制（台面/挡水按**外框**成件，外框内侧的平行线只是边缘加厚的界线）
    在线网络读图里会被拆成"内芯 + 一圈薄片"。规则：

    * **薄片判据**：最小外包边 < ``thin_mm``（20mm 宽的端帽/顶条），或
      面积/外包面积 < ``hollow_ratio``（环形加厚带，800x430 的带只占 0.09）；
    * **归属**：薄片并回**相邻**（边界距离 ≤ ``gap_mm``）的**非薄片**面，相邻有
      多个时取面积最大的那个——两个相邻零件之间的端帽因此各归各家，不会被粘成
      一件（雪花白洗手台 A 的 792x100 与 405x100 之间正是一对 20x80 端帽）；
    * **结果**：取并集的外轮廓（内缩线不再算边界），其余字段沿用被并入的那件。

    没有相邻非薄片面的薄片原样保留（可能是真的薄件，交给画像另加排除）。
    """
    if thin_mm <= 0 or len(panels) < 2:
        return panels

    polygons = [panel["polygon"] for panel in panels]
    band_flags: list[bool] = []
    for polygon in polygons:
        min_x, min_y, max_x, max_y = polygon.bounds
        width = max_x - min_x
        height = max_y - min_y
        box_area = width * height
        fill = polygon.area / box_area if box_area > 0 else 1.0
        band_flags.append(min(width, height) < thin_mm or fill < hollow_ratio)

    if not any(band_flags):
        return panels

    tree = STRtree(polygons)
    adopted: dict[int, list[int]] = {}
    orphans: list[int] = []
    for index, is_band in enumerate(band_flags):
        if not is_band:
            continue
        best_index = None
        best_area = -1.0
        for raw_index in tree.query(polygons[index]):
            other = int(raw_index)
            if other == index or band_flags[other]:
                continue
            if polygons[index].distance(polygons[other]) > gap_mm:
                continue
            if polygons[other].area > best_area:
                best_area = polygons[other].area
                best_index = other
        if best_index is None:
            orphans.append(index)
        else:
            adopted.setdefault(best_index, []).append(index)

    merged: list[dict[str, Any]] = []
    for index, is_band in enumerate(band_flags):
        if is_band:
            continue
        members = [index] + adopted.get(index, [])
        if len(members) == 1:
            merged.append(dict(panels[index]))
            continue
        union = unary_union([polygons[member] for member in members])
        if union.geom_type != "Polygon":
            union = max(
                (
                    geom
                    for geom in getattr(union, "geoms", [])
                    if geom.geom_type == "Polygon"
                ),
                key=lambda geom: geom.area,
                default=None,
            )
        if union is None or union.is_empty:
            continue
        panel = dict(panels[index])
        panel["polygon"] = union
        panel["outer_polygon"] = union
        panel["area"] = union.area
        panel["centroid"] = union.centroid
        merged.append(panel)

    for index in orphans:
        merged.append(dict(panels[index]))

    for new_index, panel in enumerate(merged):
        panel["index"] = new_index
    return merged


def _extract_line_network_panels(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    panel_layers: list[str] | None,
    excluded_layers: set[str],
) -> list[dict[str, Any]]:
    """Reconstruct closed panels from an open LINE/LWPOLYLINE network.

    This is used for drawings where tile boundaries are drawn as individual
    LINE segments instead of closed polygons. Polygonizing the complete
    network recovers both rectangular tiles and curve-split non-rectangular
    parts. Thin rectangles caused by expansion joints are filtered out by
    their bounding-box short side.
    """
    layer_names = set(profile.network_layers) if profile.network_layers else set(
        panel_layers or _profile_panel_layers(profile)
    )
    if not layer_names:
        return []

    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    excluded_handles = {
        str(value) for value in profile.exclude_entity_handles if value
    }
    excluded_colors = {int(value) for value in profile.exclude_colors}
    lines: list[LineString] = []

    for entity in doc.modelspace():
        dtype = entity.dxftype()
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers or layer not in layer_names:
            continue
        if str(getattr(entity.dxf, "handle", "")) in excluded_handles:
            continue
        if excluded_colors and int(getattr(entity.dxf, "color", 256)) in excluded_colors:
            continue
        linetype = _linetype_name(entity, doc).upper()
        if linetype in excluded_linetypes:
            continue

        if dtype == "LINE":
            start = (float(entity.dxf.start.x), float(entity.dxf.start.y))
            end = (float(entity.dxf.end.x), float(entity.dxf.end.y))
            line = LineString([start, end])
            if line.length > 1e-9:
                lines.append(line)
            continue

        if dtype == "LWPOLYLINE":
            points = _lwpolyline_points(entity)
            if len(points) < 2:
                continue
            points = [(float(x), float(y)) for x, y in points]
            segment_pairs = list(zip(points, points[1:]))
            if _is_closed(entity, profile.closed_tolerance):
                segment_pairs.append((points[-1], points[0]))
        elif dtype == "ARC":
            try:
                raw_points = list(entity.flattening(profile.network_arc_flattening))
            except Exception:
                continue
            points = [(float(point[0]), float(point[1])) for point in raw_points]
            if len(points) < 2:
                continue
            segment_pairs = list(zip(points, points[1:]))
        else:
            continue

        for start, end in segment_pairs:
            line = LineString([start, end])
            if line.length > 1e-9:
                lines.append(line)

    if not lines:
        return []

    if profile.network_snap_tolerance > 0:
        lines = _snap_line_endpoints(
            lines,
            profile.network_snap_tolerance,
        )

    polygons = list(polygonize(unary_union(lines)))
    panels: list[dict[str, Any]] = []
    output_layer = (
        profile.network_layers[0]
        if profile.network_layers
        else (panel_layers[0] if panel_layers else "")
    )

    for polygon in polygons:
        if polygon.is_empty:
            continue
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            continue

        candidates: list[Polygon] = []
        if polygon.geom_type == "Polygon":
            candidates.append(polygon)
        elif polygon.geom_type in {"MultiPolygon", "GeometryCollection"}:
            candidates.extend(
                geom
                for geom in polygon.geoms
                if geom.geom_type == "Polygon"
            )
        else:
            continue

        for cleaned in candidates:
            if cleaned.is_empty:
                continue
            if not (profile.network_min_area <= cleaned.area <= profile.network_max_area):
                continue
            min_x, min_y, max_x, max_y = cleaned.bounds
            width = max_x - min_x
            height = max_y - min_y
            if min(width, height) <= profile.network_gap_side_tolerance:
                continue

            centroid = cleaned.centroid
            panels.append(
                {
                    "index": len(panels),
                    "polygon": cleaned,
                    "outer_polygon": cleaned,
                    "holes": [],
                    "hole_handles": [],
                    "centroid": centroid,
                    "area": cleaned.area,
                    "handle": None,
                    "layer": output_layer,
                    "source": "line_network",
                    "confidence": 0.9,
                }
            )

    return panels


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


def _extract_profile_panels_impl(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    panel_layer_override: list[str] | None = None,
    exclude_layers: list[str] | None = None,
    label_boxes: list[Polygon | None] | None = None,
    recognition_memory: dict[str, Any] | None = None,
    recognition_model: dict[str, Any] | None = None,
    classification_sink: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    panel_layer = profile.panel_layer or None
    panel_layers = (
        panel_layer_override
        or profile.panel_layers
        or ([panel_layer] if panel_layer else None)
    )
    allowed_panel_layers = set(panel_layers) if panel_layers else None
    # 画像声明的排除图层与调用方传入的排除图层合并（几何层面生效，
    # 编号/区域/材料等文字收集不受影响）。
    excluded_layers = {
        value for value in (exclude_layers or [])
    } | {
        value for value in (profile.exclude_layers or [])
    }
    panels: list[dict[str, Any]] = []

    if profile.network_mode:
        panels = _extract_line_network_panels(
            doc,
            profile,
            panel_layers,
            excluded_layers,
        )
        if profile.band_face_merge_mm > 0:
            panels = _merge_band_faces(panels, profile.band_face_merge_mm)
        _assign_profile_holes(panels, doc, profile, excluded_layers)
        return panels

    if profile.grid_mode:
        panels = _extract_grid_panels(
            doc,
            profile,
            panel_layers,
            excluded_layers,
        )
        if profile.band_face_merge_mm > 0:
            panels = _merge_band_faces(panels, profile.band_face_merge_mm)
        _assign_profile_holes(panels, doc, profile, excluded_layers)
        return panels

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
            _assign_profile_holes(panels, doc, profile, excluded_layers)
            return panels

    raw_candidates = extract_closed_entity_candidates(
        doc,
        panel_layers=panel_layers,
        exclude_layers=sorted(excluded_layers),
        exclude_linetypes=profile.exclude_linetypes,
        closed_tolerance=profile.closed_tolerance,
        exclude_handles=set(profile.exclude_entity_handles),
        hole_layers=profile.hole_layers,
        exclude_colors=profile.exclude_colors or None,
        layer_filter_mode="hint",
    )
    classified_candidates = classify_candidates(
        raw_candidates,
        panel_layers=panel_layers,
        hole_layers=profile.hole_layers,
        exclude_layers=sorted(excluded_layers),
        small_area_threshold=profile.small_area_threshold,
        label_boxes=label_boxes,
        recognition_memory=recognition_memory,
        recognition_model=recognition_model,
    )
    if classification_sink is not None:
        classification_sink.extend(classified_candidates)
    accepted_candidates = [
        candidate
        for candidate in classified_candidates
        if candidate["classification"]["role"] in {"panel", "hole"}
    ]
    classified_holes = [
        candidate
        for candidate in accepted_candidates
        if candidate["classification"]["role"] == "hole"
    ]

    if accepted_candidates:
        polygon_items = [
            (candidate["polygon"], candidate.get("handle"))
            for candidate in accepted_candidates
        ]
        if profile.build_hierarchy:
            hierarchy = _build_part_hierarchy(polygon_items)
            for item in hierarchy:
                if item["parent"] != -1:
                    continue
                source_candidate = accepted_candidates[item["orig_index"]]
                source_classification = source_candidate["classification"]
                if source_classification["role"] != "panel":
                    continue

                # hierarchy 的 children 是"按面积降序"排列后的序号，必须用
                # hierarchy[...] 取值。被父轮廓直接包含的子轮廓在几何上就是
                # 孔洞，不能再要求分类器也标成 hole（图层弱化后分类器常把它
                # 标成 panel）。旧代码用 accepted_candidates[child_index] 取错
                # 元素、又用 role == "hole" 过滤一次，导致被包含的闭合多段线
                # 孔洞被静默丢弃 —— CGR45 曾丢 153 个盆孔。
                hole_items = [
                    hierarchy[child_index] for child_index in item["children"]
                ]
                outer = item["poly"]
                holes = [child["poly"] for child in hole_items]
                hole_handles = [child.get("handle") for child in hole_items]
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
                        "layer": source_candidate.get("layer"),
                        "closure_gap": source_candidate.get("closure_gap", 0.0),
                        "source": "classified_closed_polygon",
                        "confidence": source_classification["confidence"],
                        "classification": source_classification,
                    }
                )
        else:
            for source_candidate in accepted_candidates:
                source_classification = source_candidate["classification"]
                if source_classification["role"] != "panel":
                    continue
                polygon = source_candidate["polygon"]
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
                        "handle": source_candidate.get("handle"),
                        "layer": source_candidate.get("layer"),
                        "closure_gap": source_candidate.get("closure_gap", 0.0),
                        "source": "classified_closed_polygon",
                        "confidence": source_classification["confidence"],
                        "classification": source_classification,
                    }
                )

    line_entities = []
    fallback_line_entities = []
    excluded_linetypes = {value.upper() for value in profile.exclude_linetypes}
    allowed_panel_layers = _profile_panel_layers(profile)
    for entity in doc.modelspace():
        if entity.dxftype() != "LINE":
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers:
            continue
        linetype = _linetype_name(entity, doc).upper()
        if linetype in excluded_linetypes:
            continue
        if allowed_panel_layers and layer not in allowed_panel_layers:
            fallback_line_entities.append(entity)
        else:
            line_entities.append(entity)
    if allowed_panel_layers and not line_entities:
        line_entities = fallback_line_entities

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
    _assign_classified_holes(panels, classified_holes)
    _assign_profile_holes(panels, doc, profile, excluded_layers)
    return panels


def _extract_profile_panels(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    panel_layer_override: list[str] | None = None,
    exclude_layers: list[str] | None = None,
    label_boxes: list[Polygon | None] | None = None,
    recognition_memory: dict[str, Any] | None = None,
    recognition_model: dict[str, Any] | None = None,
    classification_sink: list[dict[str, Any]] | None = None,
    region_sink: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """抽取规格板面板。

    ``profile.region_min_side_mm`` > 0 时，**最小边**达到该阈值的封闭图形按
    "区域轮廓"（房间/户型/大区域）处理：不作为规格板，但收集进 ``region_sink``
    供编号归属使用（见 ``assignment_mode="containing_region"``）。
    """
    panels = _extract_profile_panels_impl(
        doc,
        profile,
        panel_layer_override=panel_layer_override,
        exclude_layers=exclude_layers,
        label_boxes=label_boxes,
        recognition_memory=recognition_memory,
        recognition_model=recognition_model,
        classification_sink=classification_sink,
    )
    threshold = float(profile.region_min_side_mm or 0.0)
    if threshold <= 0:
        return panels
    kept: list[dict[str, Any]] = []
    for panel in panels:
        minx, miny, maxx, maxy = panel["polygon"].bounds
        if min(maxx - minx, maxy - miny) >= threshold:
            if region_sink is not None:
                region_sink.append(panel)
            continue
        kept.append(panel)
    for index, panel in enumerate(kept):
        panel["index"] = index
    return kept


def _assign_classified_holes(
    panels: list[dict[str, Any]],
    hole_candidates: list[dict[str, Any]],
) -> None:
    """Assign geometry-classified holes to the smallest containing panel."""
    if not panels or not hole_candidates:
        return
    assigned_handles = {
        str(handle)
        for panel in panels
        for handle in panel.get("hole_handles", [])
        if handle
    }
    for candidate in hole_candidates:
        hole_polygon = candidate.get("polygon")
        if not isinstance(hole_polygon, Polygon):
            continue
        handle = candidate.get("handle")
        if handle and str(handle) in assigned_handles:
            continue
        containing = []
        for panel in panels:
            outer = panel.get("outer_polygon")
            if not isinstance(outer, Polygon) or outer.area <= hole_polygon.area:
                continue
            try:
                if outer.contains(hole_polygon) or _is_contained_as_hole(
                    outer, hole_polygon
                ):
                    containing.append(panel)
            except Exception:
                continue
        if not containing:
            continue
        panel = min(containing, key=lambda item: float(item["outer_polygon"].area))
        combined = panel["polygon"].difference(hole_polygon)
        if isinstance(combined, MultiPolygon):
            combined = max(combined.geoms, key=lambda geom: geom.area)
        if combined.is_empty or combined.area <= 0.01:
            continue
        panel["polygon"] = combined
        panel["area"] = combined.area
        panel["centroid"] = combined.centroid
        panel["holes"].append(hole_polygon)
        panel["hole_handles"] = panel.get("hole_handles", []) + [handle]
        if handle:
            assigned_handles.add(str(handle))

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
    recognition_memory: dict[str, Any] | None = None,
    recognition_model: dict[str, Any] | None = None,
    classification_sink: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    memory_hints = recognition_memory_hints(recognition_memory)
    allowed_layers = set(profile.number_layers)
    allowed_layers.update(memory_hints.get("number_layers", []))
    excluded_layers = {value for value in (exclude_layers or [])}
    pattern = profile.compiled_label_pattern()
    preferred: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []

    for entity in doc.modelspace():
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers:
            continue
        if getattr(entity.dxf, "handle", None) in profile.exclude_entity_handles:
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
        item = {
            "entity": entity,
            "text": text,
            "layer": layer,
            "point": _entity_coordinates(entity),
            "box": _text_box_polygon(entity),
            "layer_hint": bool(allowed_layers and layer in allowed_layers),
        }
        fallback.append(item)
        if not allowed_layers or layer in allowed_layers:
            preferred.append(item)
    return preferred if preferred else fallback
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


COLUMN_MATERIAL_SPLIT = re.compile(r"[/、,;；]")


def _split_material_codes(value: str) -> list[str]:
    """把 `ST-7/ST-8` 这类写法拆成材料号列表（保持书写顺序，大写化）。"""
    return [
        part.strip().upper()
        for part in COLUMN_MATERIAL_SPLIT.split(value)
        if part.strip()
    ]


def _text_height(entity, default: float) -> float:
    """TEXT 用 `height`、MTEXT 用 `char_height`；都取不到时回退画像默认字高。"""
    for attribute in ("height", "char_height"):
        try:
            value = float(getattr(entity.dxf, attribute, 0.0) or 0.0)
        except Exception:
            value = 0.0
        if value > 0.0:
            return value
    return float(default or 0.0)


def _text_left_edge(entity, box: Polygon | None) -> float:
    """文字视觉左边缘 x，用作列边界锚点。

    citizen 的 `L-XX LEVEL` 是左对齐 TEXT（`halign=0`），此时
    `insert.x` 就是文字左边缘，也正是该列零件的起始 x；用包围盒中心
    当边界会让每列整体右移半个字宽，把边界附近的零件错分到相邻列。
    """
    if box is not None and not box.is_empty:
        try:
            return float(box.bounds[0])
        except Exception:
            pass
    return float(_entity_coordinates(entity)[0])


def collect_column_materials(
    doc: ezdxf.document.Drawing,
    profile: DrawingProfile,
    exclude_layers: list[str] | None = None,
) -> list[dict[str, Any]]:
    """收集列头「材料：ST-x」标签，并按楼层标签（`L-XX LEVEL`）划分列。

    多层平面图约定（citizen.dxf 实测）：每个楼层占 x 方向一列，列头同时给出
    `L-XX LEVEL` 与 `材料：ST-x`；两组标签按 x 顺序一一配对，列的 x 区间为
    `[本层标签 x, 下一层标签 x)`，最后一列向右开放。零件按质心 x 归列。

    返回按 x 升序排列的列表，每项：

    - ``column`` / ``level``：列序号与楼层标签，如 ``L-10``；
    - ``x_min`` / ``x_max``：列区间（最后一列 ``x_max`` 为 ``inf``）；
      锚点是楼层标签的**左边缘**（`_text_left_edge`），与
      `_analysis/material_usage_report.py` 同口径；
    - ``materials``：材料号列表，如 ``["ST-6"]``、``["ST-7", "ST-8"]``；
    - ``material_group``：材料号合并写法，如 ``ST-7/ST-8``（列内共用，未拆分）；
    - ``label`` / ``point`` / ``height``：标签原文、原位坐标（包围盒中心）与字高，
      供原位 DXF 复写；
    - ``warning``：配对异常说明（正常时该键不存在）。
    """
    allowed_layers = set(profile.material_layers or profile.number_layers)
    excluded_layers = {value for value in (exclude_layers or [])}
    material_pattern = re.compile(profile.material_label_pattern)
    level_pattern = re.compile(profile.level_label_pattern)

    level_marks: list[dict[str, Any]] = []
    material_marks: list[dict[str, Any]] = []
    for entity in doc.modelspace():
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if layer in excluded_layers:
            continue
        if allowed_layers and layer not in allowed_layers:
            continue
        if getattr(entity.dxf, "handle", None) in profile.exclude_entity_handles:
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
        box = _text_box_polygon(entity)
        point = (
            (float(box.centroid.x), float(box.centroid.y))
            if box is not None and not box.is_empty
            else _entity_coordinates(entity)
        )
        left_edge = _text_left_edge(entity, box)
        height = _text_height(entity, profile.text_height)

        level = level_pattern.match(text)
        if level:
            level_marks.append(
                {
                    "level": level.group("level").upper(),
                    "text": text,
                    "layer": layer,
                    "point": point,
                    "x_anchor": left_edge,
                    "height": height,
                }
            )
            continue
        material = material_pattern.search(text)
        if not material:
            continue
        materials = _split_material_codes(material.group("materials"))
        if not materials:
            continue
        material_marks.append(
            {
                "materials": materials,
                "text": text,
                "layer": layer,
                "point": point,
                "x_anchor": left_edge,
                "height": height,
            }
        )

    level_marks.sort(key=lambda item: item["x_anchor"])
    material_marks.sort(key=lambda item: item["x_anchor"])

    columns: list[dict[str, Any]] = []
    for index, mark in enumerate(level_marks):
        x_min = float(mark["x_anchor"])
        x_max = (
            float(level_marks[index + 1]["x_anchor"])
            if index + 1 < len(level_marks)
            else float("inf")
        )
        column: dict[str, Any] = {
            "column": index + 1,
            "level": mark["level"],
            "x_min": x_min,
            "x_max": x_max,
            "materials": [],
            "material_group": None,
            "label": None,
            "point": None,
            "height": mark["height"],
        }
        if index < len(material_marks):
            material = material_marks[index]
            material_x = float(material["x_anchor"])
            if not x_min <= material_x < x_max:
                span = (
                    f"[{x_min:.0f}, {x_max:.0f})"
                    if x_max != float("inf")
                    else f"[{x_min:.0f}, inf)"
                )
                column["warning"] = (
                    f"第 {index + 1} 列（{mark['level']}）材料标签 "
                    f"x={material_x:.0f} 不在 {span} 内"
                )
            column.update(
                {
                    "materials": material["materials"],
                    "material_group": "/".join(material["materials"]),
                    "label": material["text"],
                    "point": material["point"],
                    "height": material["height"] or mark["height"],
                }
            )
        else:
            column["warning"] = f"第 {index + 1} 列（{mark['level']}）没有配到材料标签"
        columns.append(column)
    return columns


def column_material_for_x(
    columns: list[dict[str, Any]],
    x: float,
) -> dict[str, Any] | None:
    """按 x 坐标取列定义（与 `_analysis/material_usage_report.py` 同口径）。"""
    for column in columns:
        if column["x_min"] <= x < column["x_max"]:
            return column
    return None


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
                if (
                    panel["polygon"].contains(point)
                    or panel.get("outer_polygon", panel["polygon"]).covers(point)
                ):
                    inside.append(text_index)
            except Exception:
                continue
        if inside:
            if profile.conflict_resolution == "best_overlap":
                best = max(
                    inside,
                    key=lambda index: max(
                        _overlap_ratio(panel["polygon"], texts[index]["box"]),
                        _overlap_ratio(panel.get("outer_polygon", panel["polygon"]), texts[index]["box"]),
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
            ratio = max(
                _overlap_ratio(panel["polygon"], text["box"]),
                _overlap_ratio(panel.get("outer_polygon", panel["polygon"]), text["box"]),
            )
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


def _assign_numbers_by_containing_region(
    panels: list[dict[str, Any]],
    regions: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    profile: DrawingProfile,
) -> dict[int, str]:
    """按"零件落在哪个区域轮廓内"归属编号（房间/户型平面图）。

    区域轮廓（房间、户型）本身不是规格板，但它们的房号标签决定内部零件的
    编号：先用标签插入点（或最近标签）给每个区域定型，再按零件代表点把零件
    归到包含它的最小区域内，最后按区域分组、组内按 (x, y) 排序编号。
    没有落在任何区域内的零件回退为"最近房号标签"，避免整块丢编号。
    """
    region_labels: dict[int, tuple[str, str]] = {}
    for region in regions:
        polygon = region["polygon"]
        best_text = None
        best_dist = profile.room_max_distance
        for text in texts:
            if _room_label_parts(text["text"], profile) is None:
                continue
            tx, ty = text["point"]
            if polygon.contains(Point(tx, ty)):
                best_text = text
                break
            dist = math.hypot(
                float(polygon.centroid.x) - float(tx),
                float(polygon.centroid.y) - float(ty),
            )
            if dist < best_dist:
                best_dist = dist
                best_text = text
        if best_text is None:
            continue
        parts = _room_label_parts(best_text["text"], profile)
        if parts is None:
            continue
        region_labels[region["index"]] = parts

    # 第一步：给每个零件确定归属区域（含/最近标签兜底），不立刻编号，
    # 这样"区域内的零件"和"兜底零件"会在同一个 (unit, room) 组里连续编号。
    group_keys: dict[int, tuple[str, str]] = {}
    for panel in panels:
        polygon = panel["polygon"]
        point = polygon.representative_point()
        owner = None
        for region in regions:
            if region["index"] not in region_labels:
                continue
            if not region["polygon"].contains(point):
                continue
            if owner is None or region["area"] < owner["area"]:
                owner = region
        if owner is not None:
            group_keys[panel["index"]] = region_labels[owner["index"]]
            continue
        centroid = polygon.centroid
        best_text = None
        best_dist = profile.room_max_distance
        for text in texts:
            parts = _room_label_parts(text["text"], profile)
            if parts is None:
                continue
            dist = math.hypot(
                float(centroid.x) - text["point"][0],
                float(centroid.y) - text["point"][1],
            )
            if dist < best_dist:
                best_dist = dist
                best_text = parts
        if best_text is not None:
            group_keys[panel["index"]] = best_text

    groups: dict[tuple[str, str], list[tuple[int, float, float]]] = {}
    for panel in panels:
        key = group_keys.get(panel["index"])
        if key is None:
            continue
        centroid = panel["polygon"].centroid
        groups.setdefault(key, []).append(
            (panel["index"], float(centroid.x), float(centroid.y))
        )

    assignments: dict[int, str] = {}
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
    recognition_memory: dict[str, Any] | None = None,
    recognition_model: dict[str, Any] | None = None,
    classification_sink: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], ezdxf.document.Drawing]:
    """按图纸画像读取 DXF 并返回与 read_dxf 相同结构的 parts_data。

    面板来源优先使用画像中的 HATCH/LINE 规则；编号归属使用
    point_then_bbox：编号插入点优先，其次用文字包围盒与面板的重叠比例补配。
    """
    filepath = Path(filepath)
    doc = ezdxf.readfile(str(filepath))
    texts = _collect_profile_number_texts(
        doc,
        profile,
        exclude_layers=exclude_layers,
        recognition_memory=recognition_memory,
    )
    regions: list[dict[str, Any]] = []
    panels = _extract_profile_panels(
        doc,
        profile,
        panel_layer_override=panel_layers,
        exclude_layers=exclude_layers,
        label_boxes=[text.get("box") for text in texts],
        recognition_memory=recognition_memory,
        recognition_model=recognition_model,
        classification_sink=classification_sink,
        region_sink=regions,
    )
    zone_texts = _collect_zone_texts(
        doc,
        profile,
        exclude_layers=exclude_layers,
    )
    room_assignments = None
    panel_layer_assignments = None
    region_assignments = None
    assignments = None
    if profile.assignment_mode == "nearest_room":
        room_assignments = _assign_room_numbers_to_panels(panels, texts, profile)
    elif profile.assignment_mode == "containing_region":
        region_assignments = _assign_numbers_by_containing_region(
            panels,
            regions,
            texts,
            profile,
        )
    elif profile.assignment_mode == "panel_layer":
        panel_layer_assignments = _assign_numbers_by_panel_layer(
            panels,
            profile,
            zone_texts=zone_texts or None,
        )
    elif profile.network_mode:
        assignments = (
            {} if not profile.network_assign_numbers
            else _assign_texts_to_panels(panels, texts, profile)[0]
        )
    else:
        assignments, _ = _assign_texts_to_panels(panels, texts, profile)

    # 列头材料：零件按质心 x 归属到所在列，材料号写进 material_group，
    # 供原位 DXF 标注与后续"不同材料不共板"的分组使用。
    column_materials: list[dict[str, Any]] = []
    if profile.material_column_enabled and not profile.network_mode:
        column_materials = collect_column_materials(
            doc,
            profile,
            exclude_layers=exclude_layers,
        )

    parts_data: list[dict[str, Any]] = []
    for panel in panels:
        number_layer = None
        if panel.get("original_number") is not None:
            original_number = panel["original_number"]
        elif profile.network_mode and not profile.network_assign_numbers:
            original_number = None
        elif room_assignments is not None:
            original_number = room_assignments.get(panel["index"])
        elif region_assignments is not None:
            original_number = region_assignments.get(panel["index"])
        elif panel_layer_assignments is not None:
            original_number = panel_layer_assignments.get(panel["index"])
        else:
            text_indexes = assignments.get(panel["index"], []) if assignments else []
            original_number = None
            if text_indexes:
                number_text = texts[text_indexes[0]]
                number_layer = number_text.get("layer")
                original_number = _normalize_label(
                    number_text["text"],
                    profile,
                )

        centroid = panel["centroid"]
        column_material = (
            column_material_for_x(column_materials, float(centroid.x))
            if column_materials
            else None
        )
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
                "number_layer": number_layer,
                "confidence": panel.get("confidence", 1.0),
                "source": panel.get("source", "closed_polygon"),
                "closure_gap": float(panel.get("closure_gap", 0.0) or 0.0),
                "classification": panel.get("classification"),
                "material_group": (
                    column_material["material_group"]
                    if column_material is not None
                    else None
                ),
                "material_column": (
                    column_material["level"]
                    if column_material is not None
                    else None
                ),
            }
        )
    # 源实体类型（圆/圆弧）挂到零件上，供原位 DXF 如实复写
    attach_source_curves(parts_data, doc)
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
    if not panels_from_hatch and not profile.grid_mode and not profile.network_mode:
        _add_unclosed_polyline_issues(doc, profile, issues)
        _add_line_chain_issues(doc, profile, issues)
        _add_invalid_geometry_issues(doc, profile, issues)

    regions: list[dict[str, Any]] = []
    panels = _extract_profile_panels(doc, profile, region_sink=regions)
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
    elif profile.assignment_mode == "containing_region":
        region_assignments = _assign_numbers_by_containing_region(
            panels,
            regions,
            texts,
            profile,
        )
        _add_expected_count_issues(
            panels,
            texts,
            {},
            profile,
            issues,
            actual_numbers=set(region_assignments.values()),
        )
        _add_room_assignment_issues(
            panels,
            region_assignments,
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
        if profile.grid_mode:
            actual_numbers = {
                panel["original_number"]
                for panel in panels
                if panel.get("original_number") is not None
            }
            _add_expected_count_issues(
                panels,
                texts,
                {},
                profile,
                issues,
                actual_numbers=actual_numbers,
            )
        elif profile.network_mode and not profile.network_assign_numbers:
            actual_numbers = {
                panel["original_number"]
                for panel in panels
                if panel.get("original_number") is not None
            }
            _add_expected_count_issues(
                panels,
                texts,
                {},
                profile,
                issues,
                actual_numbers=actual_numbers,
            )
        else:
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
