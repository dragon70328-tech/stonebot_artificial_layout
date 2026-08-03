"""DXF 文件解析：提取封闭图形，构建规格板（含挖孔）"""

import ezdxf
from shapely.geometry import Polygon, Point, MultiPolygon
from shapely.ops import unary_union
from ezdxf.entities import LWPolyline, Polyline, Circle, Arc, Spline
import numpy as np


def _entity_to_polygon(entity, num_segments: int = 64) -> Polygon | None:
    """将 DXF 实体转换为 Shapely Polygon"""
    try:
        if isinstance(entity, (LWPolyline, Polyline)):
            points = []
            if isinstance(entity, LWPolyline):
                with entity.points() as pts:
                    points = [(p[0], p[1]) for p in pts]
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
                dx = abs(pts[0][0] - pts[-1][0])
                dy = abs(pts[0][1] - pts[-1][1])
                if dx < 0.01 and dy < 0.01:
                    return True
        except Exception:
            pass
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
    num_layers = [
        layer.dxf.name for layer in doc.layers
        if any(ord(c) == 32534 for c in layer.dxf.name)
    ]
    msp = doc.modelspace()
    cx, cy = centroid

    for entity in msp:
        if entity.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        if num_layers and entity.dxf.layer not in num_layers:
            continue
        try:
            if entity.dxftype() == 'TEXT':
                tx = entity.dxf.insert.x if hasattr(entity.dxf, 'insert') else 0
                ty = entity.dxf.insert.y if hasattr(entity.dxf, 'insert') else 0
                text = entity.dxf.text
            else:
                tx = entity.dxf.insert.x if hasattr(entity.dxf, 'insert') else 0
                ty = entity.dxf.insert.y if hasattr(entity.dxf, 'insert') else 0
                text = entity.plain_text() if hasattr(entity, 'plain_text') else entity.dxf.text

            dist = ((tx - cx) ** 2 + (ty - cy) ** 2) ** 0.5
            if dist < search_radius and text and text.strip():
                return text.strip()
        except Exception:
            continue

    return None



def _collect_number_texts(doc,
                         number_layer = None):
    if number_layer is not None:
        num_layers = [number_layer]
    else:
        num_layers = [
            layer.dxf.name for layer in doc.layers
            if any(ord(c) == 32534 for c in layer.dxf.name)
        ]
    texts = []
    msp = doc.modelspace()
    for entity in msp:
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        if num_layers and entity.dxf.layer not in num_layers:
            continue
        try:
            tx = entity.dxf.insert.x
            ty = entity.dxf.insert.y
            if entity.dxftype() == "TEXT":
                t = entity.dxf.text
            else:
                t = entity.plain_text() if hasattr(entity, "plain_text") else entity.dxf.text
            if t and t.strip():
                texts.append((tx, ty, t.strip()))
        except Exception:
            continue
    return texts


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
            number_layer = None):

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
    number_pool = _collect_number_texts(doc, number_layer=number_layer)

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

    # --- Number assignment: containment-first, then distance fallback ---
    assigned_numbers = _assign_numbers_by_containment(parts_data, number_pool)
    for pd in parts_data:
        pd["original_number"] = assigned_numbers.get(pd["index"])

    return parts_data, doc
