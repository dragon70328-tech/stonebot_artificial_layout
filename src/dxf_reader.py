"""DXF 文件解析：提取封闭图形，构建规格板（含挖孔）"""

import ezdxf
import math
from shapely.geometry import Polygon, Point, MultiPolygon
from shapely.ops import unary_union
from ezdxf.entities import LWPolyline, Polyline, Circle, Arc, Spline
from ezdxf.math import bulge_to_arc
import numpy as np
import re


def _bulge_arc_points(start_point, end_point, bulge: float,
                      max_segments: int = 64) -> list:
    """将 LWPOLYLINE 的 bulge 弧段采样为折线点。

    bulge > 0 表示从 start_point 到 end_point 逆时针走弧；
    bulge < 0 表示顺时针走弧。返回包含首尾点的点列。
    """
    center, start_angle, end_angle, radius = bulge_to_arc(
        start_point, end_point, bulge
    )
    if radius <= 0:
        return [tuple(start_point), tuple(end_point)]

    sweep = (end_angle - start_angle) % (2 * math.pi)
    if sweep < 1e-12:
        sweep = 2 * math.pi
    segment_count = max(4, min(max_segments,
                               int(math.ceil(math.degrees(sweep) / 5.0))))

    points = []
    for i in range(segment_count + 1):
        angle = start_angle + sweep * i / segment_count
        points.append((
            center[0] + radius * math.cos(angle),
            center[1] + radius * math.sin(angle),
        ))

    if bulge < 0:
        points.reverse()
    return points


def _lwpolyline_points(entity: LWPolyline) -> list:
    """读取 LWPOLYLINE 顶点，并将 bulge 弧段展开为采样折线点。"""
    raw = entity.get_points("xyseb")
    if len(raw) < 2:
        return []

    vertices = [(p[0], p[1], p[4] if len(p) > 4 else 0.0)
                for p in raw]

    # 首尾重复时去掉最后一个重复点，后续按需闭合。
    if vertices and len(vertices) > 1:
        first = vertices[0]
        last = vertices[-1]
        if abs(first[0] - last[0]) < 1e-9 and abs(first[1] - last[1]) < 1e-9:
            vertices = vertices[:-1]

    if len(vertices) < 2:
        return []

    points = [(vertices[0][0], vertices[0][1])]
    for i in range(len(vertices) - 1):
        start = (vertices[i][0], vertices[i][1])
        end = (vertices[i + 1][0], vertices[i + 1][1])
        bulge = vertices[i][2]
        if abs(bulge) > 1e-9:
            arc_points = _bulge_arc_points(start, end, bulge)
            points.extend(arc_points[1:])
        else:
            points.append(end)

    if entity.closed:
        start = (vertices[-1][0], vertices[-1][1])
        end = (vertices[0][0], vertices[0][1])
        bulge = vertices[-1][2]
        if abs(bulge) > 1e-9:
            arc_points = _bulge_arc_points(start, end, bulge)
            points.extend(arc_points[1:-1])
        # 闭合边最后一个点就是起点，不重复追加。

    return points


def _entity_to_polygon(entity, num_segments: int = 64) -> Polygon | None:
    """将 DXF 实体转换为 Shapely Polygon"""
    try:
        if isinstance(entity, (LWPolyline, Polyline)):
            if isinstance(entity, LWPolyline):
                points = _lwpolyline_points(entity)
            else:
                points = [(v.dxf.location.x, v.dxf.location.y)
                          for v in entity.vertices]

            if len(points) < 3:
                return None
            # 确保闭合
            if points[0] != points[-1]:
                points.append(points[0])
            poly = Polygon(points)
            if not poly.is_valid:
                poly = poly.buffer(0)
            return poly

        elif isinstance(entity, Circle):
            cx, cy = entity.dxf.center.x, entity.dxf.center.y
            r = entity.dxf.radius
            angles = np.linspace(0, 2 * np.pi, num_segments)
            points = [(cx + r * np.cos(a), cy + r * np.sin(a)) for a in angles]
            return Polygon(points)

        elif isinstance(entity, Arc):
            cx, cy = entity.dxf.center.x, entity.dxf.center.y
            r = entity.dxf.radius
            sa = np.radians(entity.dxf.start_angle)
            ea = np.radians(entity.dxf.end_angle)
            # 圆弧的弦
            angles = np.linspace(sa, ea, max(8, num_segments // 2))
            points = [(cx + r * np.cos(a), cy + r * np.sin(a)) for a in angles]
            # 圆弧不能单独构成封闭图形，仅返回其弦线段
            if len(points) >= 3:
                poly = Polygon(points)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                return poly
            return None

        elif isinstance(entity, Spline):
            try:
                # 用控制点拟合多边形
                points = list(entity.flattening(0.1))
                pts = [(p.x, p.y) for p in points]
                if len(pts) < 3:
                    return None
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                return poly
            except Exception:
                return None
    except Exception:
        return None

    return None


def _is_closed(entity) -> bool:
    """判断 DXF 实体是否闭合"""
    if isinstance(entity, LWPolyline):
        if entity.closed:
            return True
        try:
            pts = list(entity.get_points())
            if len(pts) >= 3:
                return True
        except Exception:
            return False
        return False
    if isinstance(entity, Polyline):
        return entity.is_closed
    if isinstance(entity, (Circle, Spline)):
        return True
    if isinstance(entity, Arc):
        return False
    return False


# ============================================================
# Line type filtering helpers
# ============================================================
DEFAULT_EXCLUDE_LINETYPES = {"ZIGZAG", "DASHED", "MOBIAN", "DASHDOT", "DASHDOT2",
                              "DIVIDE", "CENTER", "CENTER2", "BORDER", "HIDDEN", "PHANTOM"}


def _linetype_name(entity, doc):
    lt = getattr(entity.dxf, "linetype", "BYLAYER") or "BYLAYER"
    lt_upper = lt.upper()
    if lt_upper == "BYLAYER":
        layer_name = getattr(entity.dxf, "layer", "0") or "0"
        try:
            layer = doc.layers.get(layer_name)
            if layer:
                ll = getattr(layer.dxf, "linetype", "CONTINUOUS") or "CONTINUOUS"
                return ll.upper()
        except Exception:
            pass
        return "CONTINUOUS"
    return lt_upper




def extract_closed_polygons(doc,
                             panel_layers = None,
                             exclude_layers = None,
                             exclude_linetypes = None):
    if exclude_linetypes is None:
        exclude_linetypes = DEFAULT_EXCLUDE_LINETYPES
    results = []
    msp = doc.modelspace()

    for entity in msp:
        layer = getattr(entity.dxf, "layer", "0") or "0"

        if panel_layers is not None and layer not in panel_layers:
            continue
        if exclude_layers is not None and layer in exclude_layers:
            continue

        lt = _linetype_name(entity, doc)
        if lt in exclude_linetypes:
            continue

        if not _is_closed(entity):
            continue
        poly = _entity_to_polygon(entity)
        if poly is not None and not poly.is_empty and poly.area > 0.01:
            handle = entity.dxf.handle
            results.append((poly, handle))

    return results

def _build_part_hierarchy(polygons: list) -> list[dict]:
    """
    判断内外包含关系，构建规格板层级结构。
    返回：每个大封闭图形及其内部挖孔
    """
    n = len(polygons)
    # polygons is now list of (Polygon, handle) tuples
    polys_only = [p for p, h in polygons]
    handles = [h for p, h in polygons]
    # 按面积降序排列
    sorted_indices = sorted(range(n), key=lambda i: polys_only[i].area, reverse=True)
    sorted_polys = [polys_only[i] for i in sorted_indices]

    children = [[] for _ in range(n)]  # children[i] = 直接包含的挖孔索引
    parent = [-1] * n                    # parent[i] = 直接包含 i 的父图形索引

    for i in range(n):
        for j in range(i + 1, n):
            # i 面积更大，检查 j 是否被 i 包含
            try:
                if sorted_polys[i].contains(sorted_polys[j]):
                    # 检查是否有更近的父级
                    is_direct_child = True
                    for k in children[i]:
                        if sorted_polys[k].contains(sorted_polys[j]):
                            is_direct_child = False
                            break
                    if is_direct_child:
                        children[i].append(j)
                        parent[j] = i
            except Exception:
                continue

    return [
        {
            "poly": sorted_polys[i],
            "orig_index": sorted_indices[i],
            "children": children[i],
            "parent": parent[i],
            "handle": handles[sorted_indices[i]],
        }
        for i in range(n)
    ]


def find_number_labels(doc: ezdxf.document.Drawing,
                       centroid: tuple[float, float],
                       search_radius: float = 5000.0) -> str | None:
    """
    在指定几何中心附近查找 TEXT 或 MTEXT 编号。
    search_radius：搜索半径（单位与 DXF 一致）
    """
    cx, cy = centroid
    number_pool = _collect_number_texts(doc)
    best_dist = float("inf")
    best_text = None
    for tx, ty, text in number_pool:
        dist = ((tx - cx) ** 2 + (ty - cy) ** 2) ** 0.5
        if dist < search_radius and dist < best_dist:
            best_dist = dist
            best_text = text
    return best_text



def _looks_like_number(text: str) -> bool:
    """Return True for common part-number shapes without forcing a single layer."""
    value = " ".join(text.strip().split())
    if not value or len(value) > 15:
        return False
    markers = (
        "\u6237\u578b", "\u5957", "\u8bf4\u660e", "\u6750\u6599",
        "\u77f3\u6750", "\u7f16\u53f7", "\u7bb1\u53f7", "\u6587\u5b57",
        "\u9762\u79ef", "\u5355\u4f4d", "\u6bd4\u4f8b", "\u8bbe\u8ba1",
        "\u5ba1\u6838", "\u65e5\u671f", "\u5907\u6ce8", "\u56fe\u540d",
    )
    if any(marker in value for marker in markers):
        return False
    if re.search(r"\d$", value):
        return True
    if re.fullmatch(r"\d+[-_/][A-Za-z]+", value):
        return True
    if re.fullmatch(r"[A-Za-z]+[-_/]\d+", value):
        return True
    return False


def _collect_number_texts(doc,
                          number_layer = None,
                          number_layers = None,
                          label_pattern = None):
    """Collect candidate part-number texts from TEXT/MTEXT entities.

    Layer selection priority:
    1. explicit number_layers
    2. legacy number_layer argument
    3. layers whose name contains the Chinese character 32534
    4. all TEXT/MTEXT entities, filtered by common number shape
    """
    explicit_layers = None
    if number_layers is not None:
        explicit_layers = set(number_layers) if number_layers else None
    elif number_layer is not None:
        explicit_layers = {number_layer}
    elif any(
        any(ord(c) == 32534 for c in (layer.dxf.name or ""))
        for layer in doc.layers
    ):
        explicit_layers = {
            layer.dxf.name for layer in doc.layers
            if any(ord(c) == 32534 for c in (layer.dxf.name or ""))
        }

    pattern = re.compile(label_pattern) if label_pattern else None
    texts = []
    seen = set()
    msp = doc.modelspace()
    for entity in msp:
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if explicit_layers is not None and layer not in explicit_layers:
            continue
        try:
            tx = float(entity.dxf.insert.x)
            ty = float(entity.dxf.insert.y)
            if entity.dxftype() == "TEXT":
                raw_text = entity.dxf.text
            else:
                raw_text = entity.plain_text() if hasattr(entity, "plain_text") else entity.dxf.text
        except Exception:
            continue

        text = " ".join(raw_text.strip().split()) if raw_text else ""
        if not text:
            continue
        if pattern is not None and not pattern.match(text):
            continue
        if explicit_layers is None and pattern is None and not _looks_like_number(text):
            continue

        key = (round(tx, 6), round(ty, 6), text)
        if key in seen:
            continue
        seen.add(key)
        texts.append((tx, ty, text))
    return texts


def _collect_room_texts(msp):
    """Fallback: collect room-type labels from MTEXT entities containing 户型.
    Returns list of (x, y, normalized_label). Labels are deduplicated by position."""
    candidates = {}
    for entity in msp:
        if entity.dxftype() != "MTEXT":
            continue
        try:
            t = entity.plain_text() if hasattr(entity, "plain_text") else entity.dxf.text
        except Exception:
            continue
        if not t or "户型" not in t:
            continue
        if "套" in t:
            continue
        t = re.sub(r"\{[^}]*;", "", t).replace("{", "").replace("}", "").strip()
        t = t.replace("户型：", "").replace("户型:", "").strip()
        t = re.sub(r"\s+", "", t)
        t = re.sub(r"^B7a", "B7-a", t)
        x = entity.dxf.insert.x
        y = entity.dxf.insert.y
        key = (round(x / 100), round(y / 100))
        if key not in candidates:
            candidates[key] = (x, y, t)
    return list(candidates.values())


def _assign_numbers_by_nearest_room(parts_data, room_labels):
    from collections import defaultdict
    panel_labels = []
    for pd in parts_data:
        cx, cy = pd["centroid"]
        best_dist = float("inf")
        best_label = None
        for lx, ly, lt in room_labels:
            d = ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_label = lt
        panel_labels.append((pd["index"], best_label, cx, cy))
    label_groups = defaultdict(list)
    for idx, label, cx, cy in panel_labels:
        label_groups[label].append((idx, cx, cy))
    assignments = {}
    for label in sorted(label_groups.keys()):
        items = label_groups[label]
        items.sort(key=lambda x: (-round(x[2] / 500), round(x[1] / 500)))
        for seq, (idx, cx, cy) in enumerate(items, 1):
            assignments[idx] = f"{label}-{seq:02d}"
    return assignments


def _assign_unique_number(
    doc,
    centroid,
    used_numbers,
    number_pool,
    search_radius = 20000.0,
):
    cx, cy = centroid
    best_dist = float("inf")
    best_idx = -1

    for i, (tx, ty, text) in enumerate(number_pool):
        if text in used_numbers:
            continue
        d = ((tx - cx) ** 2 + (ty - cy) ** 2) ** 0.5
        if d < search_radius and d < best_dist:
            best_dist = d
            best_idx = i

    if best_idx >= 0:
        _, _, text = number_pool[best_idx]
        used_numbers.add(text)
        return text
    return None

def _assign_numbers_by_containment(parts_data: list[dict],
                                    number_pool: list) -> dict:
    """Assign numbers using point-in-polygon containment as primary method.

    1. For each number text, check which panel's polygon contains it.
    2. If a text is inside exactly one panel, assign it (highest confidence).
    3. For remaining unmatched, fall back to nearest-centroid distance.
    
    Returns dict: part_index -> number
    """
    from shapely.geometry import Point

    used_numbers = set()
    assignments = {}

    if not number_pool:
        return assignments

    # Step 1: Containment-based matching
    for tx, ty, text in number_pool:
        pt = Point(tx, ty)
        containing = []
        for pd in parts_data:
            try:
                if pd["polygon"].contains(pt):
                    containing.append(pd)
            except Exception:
                continue
        if len(containing) == 1:
            pd = containing[0]
            ai = pd["index"]
            if ai not in assignments:
                assignments[ai] = text
                used_numbers.add(text)

    # Step 2: Distance-based fallback for unmatched parts
    for pd in parts_data:
        ai = pd["index"]
        if ai in assignments:
            continue
        cx, cy = pd.get("centroid", (0, 0))
        best_dist = float("inf")
        best_text = None
        for tx, ty, text in number_pool:
            if text in used_numbers:
                continue
            d = ((tx - cx) ** 2 + (ty - cy) ** 2) ** 0.5
            if d < 20000.0 and d < best_dist:
                best_dist = d
                best_text = text
        if best_text is not None:
            assignments[ai] = best_text
            used_numbers.add(best_text)

    return assignments


def read_dxf(filepath: str,
            panel_layers = None,
            exclude_layers = None,
            exclude_linetypes = None,
            number_layer = None,
            number_layers = None,
            label_pattern = None):

    """
    读取 DXF 文件，提取所有规格板信息。

    返回：
    - parts_data：规格板数据列表，每个元素包含 polygon、holes、number 等
    - doc：ezdxf 文档对象（用于后续写回）
    """
    doc = ezdxf.readfile(filepath)
    polygons = extract_closed_polygons(doc,
                                        panel_layers=panel_layers,
                                        exclude_layers=exclude_layers,
                                        exclude_linetypes=exclude_linetypes)

    if not polygons:
        return [], doc

    hierarchy = _build_part_hierarchy(polygons)

    # Pre-collect numbering layer texts for 1:1 matching
    number_pool = _collect_number_texts(
        doc,
        number_layer=number_layer,
        number_layers=number_layers,
        label_pattern=label_pattern,
    )

    room_labels = _collect_room_texts(doc.modelspace())

    # --- Build parts_data list first (before numbering) ---
    parts_data = []
    part_index = 0

    for item in hierarchy:
        if item["parent"] != -1:
            continue

        outer_poly = item["poly"]
        orig_idx = item["orig_index"]
        outer_handle = item.get("handle")
        holes = [hierarchy[h]["poly"] for h in item["children"]]
        hole_handles = [hierarchy[h].get("handle") for h in item["children"]]

        if holes:
            combined_poly = outer_poly.difference(unary_union(holes))
            if isinstance(combined_poly, MultiPolygon):
                combined_poly = max(combined_poly.geoms, key=lambda g: g.area)
        else:
            combined_poly = outer_poly

        if combined_poly.is_empty or combined_poly.area < 0.01:
            continue

        centroid = combined_poly.centroid
        part_data = {
            "polygon": combined_poly,
            "outer_polygon": outer_poly,
            "holes": holes,
            "centroid": (centroid.x, centroid.y),
            "area": combined_poly.area,
            "original_number": None,  # assigned later by containment
            "index": part_index,
            "outer_handle": outer_handle,
            "hole_handles": hole_handles,
        }
        parts_data.append(part_data)
        part_index += 1

    # --- Number assignment: containment-first, then room-label fallback ---
    assigned_numbers = _assign_numbers_by_containment(parts_data, number_pool)
    if room_labels and not assigned_numbers:
        assigned_numbers = _assign_numbers_by_nearest_room(parts_data, room_labels)
    for pd in parts_data:
        pd["original_number"] = assigned_numbers.get(pd["index"])

    return parts_data, doc
