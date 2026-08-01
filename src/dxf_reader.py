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


def extract_closed_polygons(doc: ezdxf.document.Drawing) -> list[Polygon]:
    """从 DXF 文档中提取所有封闭图形"""
    polygons = []
    msp = doc.modelspace()

    for entity in msp:
        if not _is_closed(entity):
            continue
        poly = _entity_to_polygon(entity)
        if poly is not None and not poly.is_empty and poly.area > 0.01:
            polygons.append(poly)

    return polygons


def _build_part_hierarchy(polygons: list[Polygon]) -> list[dict]:
    """
    判断内外包含关系，构建规格板层级结构。
    返回：每个大封闭图形及其内部挖孔
    """
    n = len(polygons)
    # 按面积降序排列
    sorted_indices = sorted(range(n), key=lambda i: polygons[i].area, reverse=True)
    sorted_polys = [polygons[i] for i in sorted_indices]

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
        }
        for i in range(n)
    ]


def find_number_labels(doc: ezdxf.document.Drawing,
                       centroid: tuple[float, float],
                       search_radius: float = 50.0) -> str | None:
    """
    在指定几何中心附近查找 TEXT 或 MTEXT 编号。
    search_radius：搜索半径（单位与 DXF 一致）
    """
    msp = doc.modelspace()
    cx, cy = centroid

    for entity in msp:
        if entity.dxftype() not in ('TEXT', 'MTEXT'):
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


def read_dxf(filepath: str) -> tuple[list[dict], ezdxf.document.Drawing]:
    """
    读取 DXF 文件，提取所有规格板信息。

    返回：
    - parts_data：规格板数据列表，每个元素包含 polygon、holes、number 等
    - doc：ezdxf 文档对象（用于后续写回）
    """
    doc = ezdxf.readfile(filepath)
    polygons = extract_closed_polygons(doc)

    if not polygons:
        return [], doc

    hierarchy = _build_part_hierarchy(polygons)

    parts_data = []
    part_index = 0

    for item in hierarchy:
        if item["parent"] != -1:
            # 这是一个挖孔，跳过（它将在父图形中处理）
            continue

        outer_poly = item["poly"]
        orig_idx = item["orig_index"]
        holes = [hierarchy[h]["poly"] for h in item["children"]]

        # 构建带洞多边形
        if holes:
            combined_poly = outer_poly.difference(unary_union(holes))
            if isinstance(combined_poly, MultiPolygon):
                # 取面积最大的部分
                combined_poly = max(combined_poly.geoms, key=lambda g: g.area)
        else:
            combined_poly = outer_poly

        if combined_poly.is_empty or combined_poly.area < 0.01:
            continue

        centroid = combined_poly.centroid
        number = find_number_labels(doc, (centroid.x, centroid.y))

        part_data = {
            "polygon": combined_poly,
            "outer_polygon": outer_poly,
            "holes": holes,
            "centroid": (centroid.x, centroid.y),
            "area": combined_poly.area,
            "original_number": number,
            "index": part_index,
        }
        parts_data.append(part_data)
        part_index += 1

    return parts_data, doc
