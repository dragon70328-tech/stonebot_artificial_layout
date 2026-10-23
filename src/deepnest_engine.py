"""DeepNest 风格排板后端。

目标：水刀/激光加工时允许任意角度旋转，使用 Bottom-Left Fill + 滑动压实，
快速得到一个密集排板方案。该模块只负责生成 placement，编号、孔洞和 DXF
输出仍由主流程完成。

当前实现是自写简化版：
- arbitrary_rotation=False：使用传入的离散 rotations。
- arbitrary_rotation=True：默认用**边对齐角度**（让零件最长直边与板边平行），
  而不是等步长铺满 360°；等步长网格仅在调用方显式要求时使用。
- 候选位置来自大板边界和已放面板包围盒边缘。
- 每个候选位置做 -x / -y 滑动压实，取最左下位置。

关于放置准则的实测结论（同一 CGR45final 案例，2026-09-23）：

- 打分准则 bottom_left / skyline / column / contact：**都是 54 张**；
- `_MAX_VALID_PER_ANGLE` 取 4 / 8 / 16：**都是 54 张**；
- 候选顺序 legacy / sorted（按准则排序后截断）：**都是 54 张**；
- 追加凹角锚定候选（`use_reflex=True`）：**54 张**，只是更慢（+8s）；
- 多排序 trials（4 轮）：54 张；
- 低填充板重排（`low_fill_rework=True`）：54 张，且更慢。

即：该案例的板数由**贪心的"当前板放不下就换板、永不回头"结构**决定，
不是由上面任何一个参数决定。要再降板数需要改动核心循环（桥切引擎那样
批量构建候选多边形 + 最后统一滑动的实现，或真正的 LNS 跨板重排）。
这些开关保留为显式实验入口，默认使用 legacy 口径。

关于角度的实测结论（CGR45final，203 件，3200x1800，2026-09-23）：

| 角度集 | 板数 | 出材率 | 耗时 |
|---|---:|---:|---:|
| 4 角 (0/90/180/270) | 54 | 67.55% | 8.7s |
| 24 角 (15° 网格) | 61 | 59.80% | 35.4s |
| 72 角 (5° 网格) | 61 | 59.80% | 104.3s |
| 边对齐，最长 8 边（当前默认） | 54 | 67.55% | 11.4s |

即：细化角度不但没有更密，反而更差（贪心近视：更多旋转让它先把零件放歪、
堵住后续空间），5° 相对 15° 更是纯浪费（板数相同、多 69s）。因此默认
改为边对齐角度集；`rotation_step` 只在 `angle_mode="grid"/"hybrid"` 时生效。
"""

import math
import random
import time
from dataclasses import dataclass

import shapely
from shapely import affinity
from shapely.geometry import Point
from shapely.strtree import STRtree

from src.models import Part, Sheet, NestingResult
from src.nesting import _reflex_vertices, validate_nesting

EPS = 1e-6
_OVERLAP_PATTERN = "T********"
_MAX_VALID_PER_ANGLE = 4
_MAX_VALID_PER_ANGLE_NFP = 12
_ARBITRARY_ROTATION_STEP = 5.0
# 参与边对齐计算的直边数量上限（按边长降序取最长边）。
# 实测（CGR45final，203 件）：上限 8 与 2/4 同为 54 张板，上限 16 反而退化到 55 张
# 且更慢——过短的边带来的方向是噪声，会把零件摆歪并堵住后续空间。
_MAX_EDGE_ANGLES = 8
# 曲线件判定：最长若干条边占外轮廓周长的比例低于该值时，认为存在真实圆弧
# （近似圆/样条扁平化），此时追加粗网格兜底——边对齐角度对圆弧没有意义。
# 注意不能用"边方向数"判定：CAD 导出的矩形常带几十个冗余顶点/微段，
# 方向数很多但长边占比接近 1.0，属于多边形而不是曲线件。
_EDGE_COVERAGE_MIN = 0.6
_EDGE_GRID_STEP = 45.0
_DEEPNEST_SORT_MODES = ("area", "short", "long", "jitter")
_DEEPNEST_SCORE_MODES = ("bottom_left", "skyline", "column", "contact")
_DEEPNEST_CANDIDATE_ORDERS = ("legacy", "sorted")
_LOW_FILL_MAX_SUBSET = 6
_LOW_FILL_MAX_ROUNDS = 3
_LOW_FILL_MAX_ATTEMPTS = 96
_LOW_FILL_AREA_CAP = 0.98
_DEEPNEST_CANDIDATE_MODES = ("bbox", "nfp_lite")
_NFP_LITE_MAX_VERTICES = 48
_NFP_LITE_MAX_POSITIONS = 8000


def _edge_segments(poly):
    """外轮廓的直边序列。

    零件外轮廓可能是 MultiPolygon / GeometryCollection（图纸里两个子轮廓被强制
    闭合或被 CAD 合成一个实体时会出现），逐个多边形展开；对空几何返回空列表。
    2026-10-14 修复：东莞奥特莱斯2 水刀 + 共边配对路径曾在此处抛
    ``AttributeError: 'MultiPolygon' object has no attribute 'exterior'``。
    """
    if poly is None:
        return []
    if getattr(poly, "geom_type", "") in ("MultiPolygon", "GeometryCollection"):
        items = list(poly.geoms)
    else:
        items = [poly]
    segments = []
    for item in items:
        exterior = getattr(item, "exterior", None)
        if exterior is None:
            continue
        coords = list(exterior.coords)
        segments.extend(
            (coords[i], coords[i + 1]) for i in range(len(coords) - 1)
        )
    return segments


def _edge_length(edge):
    (x0, y0), (x1, y1) = edge
    return math.hypot(x1 - x0, y1 - y0)


def _angle_allowed(angle, rotations, arbitrary_rotation):
    if arbitrary_rotation:
        return True
    angle %= 360.0
    return any(abs((allowed % 360.0) - angle) <= 1e-6 or
               abs((allowed % 360.0) - angle - 360.0) <= 1e-6
               for allowed in rotations)


def _first_part_left_placement(part, sheet_w, sheet_h,
                               rotations=(0, 90, 180, 270),
                               arbitrary_rotation=True):
    """Place the first part with its longest straight edge on the left sheet edge."""
    outer = part.outer_polygon
    origin = (outer.centroid.x, outer.centroid.y)
    edges = _edge_segments(outer)
    if not edges:
        return None

    max_len = max(_edge_length(edge) for edge in edges)
    long_edges = [edge for edge in edges
                  if abs(_edge_length(edge) - max_len) <= 1e-6]

    best = None
    best_key = (-1.0, float('inf'), float('inf'))

    for edge in long_edges:
        (x0, y0), (x1, y1) = edge
        edge_angle = math.degrees(math.atan2(y1 - y0, x1 - x0))
        for target_angle in (90.0, -90.0):
            angle = target_angle - edge_angle
            if not _angle_allowed(angle, rotations, arbitrary_rotation):
                continue
            rotated = affinity.rotate(outer, angle, origin=origin)
            for candidate_edge in _edge_segments(rotated):
                (cx0, cy0), (cx1, cy1) = candidate_edge
                if abs(cx0 - cx1) > 1e-6:
                    continue
                if abs(_edge_length(candidate_edge) - max_len) > 1e-6:
                    continue
                poly = affinity.translate(
                    rotated,
                    xoff=-cx0,
                    yoff=-rotated.bounds[1],
                )
                poly = shapely.set_precision(poly, 1e-6)
                b = poly.bounds
                if b[0] < -1e-6 or b[1] < -1e-6:
                    continue
                if b[2] > sheet_w + EPS or b[3] > sheet_h + EPS:
                    continue
                key = (-max_len, b[2], b[3])
                if best_key is None or key < best_key:
                    best_key = key
                    best = (angle, poly)

    if best is None:
        return None
    angle, poly = best
    return _Placement(part=part, rot=angle, poly=poly)


@dataclass
class _Placement:
    """一次放置记录：引用原始 Part，poly 为放置后的外轮廓。"""
    part: Part
    rot: float
    poly: shapely.Polygon


def _edge_direction_angles(part, limit: int | None = None) -> list[float]:
    """零件最长若干条直边的方向角（0-180 度，去重）。

    ``limit`` 为 None 时读取模块级 ``_MAX_EDGE_ANGLES``（便于调参与实验）。
    """
    limit = _MAX_EDGE_ANGLES if limit is None else limit
    outer = part.outer_polygon
    segments = _edge_segments(outer)
    ranked = sorted(segments, key=_edge_length, reverse=True)
    angles: list[float] = []
    for edge in ranked[:limit]:
        if _edge_length(edge) <= EPS:
            continue
        (x0, y0), (x1, y1) = edge
        angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
        if all(abs(angle - existing) > 0.01 for existing in angles):
            angles.append(round(angle, 4))
    return angles


def _edge_coverage(part, top: int | None = None) -> float:
    """最长 top 条边占外轮廓周长的比例（1.0 = 周长全部由长边构成）。"""
    top = _MAX_EDGE_ANGLES if top is None else top
    segments = _edge_segments(part.outer_polygon)
    lengths = sorted((_edge_length(edge) for edge in segments), reverse=True)
    total = sum(lengths)
    if total <= EPS:
        return 0.0
    return sum(lengths[:top]) / total


def edge_aligned_angles(part) -> list[float]:
    """让零件最长直边与板边（0°/90°）平行的旋转角。

    零件边方向为 ``d`` 时，旋转 ``-d`` 使该边水平，旋转 ``90-d`` 使其竖直；
    两种情况都取，保证零件可以"正着"或"转 90°"摆放。同时始终包含
    0/90/180/270，兼容以轴对齐矩形为主的图纸。
    """
    angles = {0.0, 90.0, 180.0, 270.0}
    for direction in _edge_direction_angles(part):
        angles.add(round((-direction) % 360.0, 4))
        angles.add(round((90.0 - direction) % 360.0, 4))
    return sorted(angles)


def _grid_angles(step: float) -> list[float]:
    if not step or step <= 0:
        return []
    count = int(round(360.0 / step))
    return [round(i * step, 4) for i in range(count)]


def angle_candidates(rotations: tuple, arbitrary_rotation: bool,
                     step: float = _ARBITRARY_ROTATION_STEP,
                     part=None, angle_mode: str = "edge") -> list:
    """返回待尝试的旋转角度。

    ``angle_mode``：

    - ``edge``（默认）：边对齐角度 + 0/90/180/270；零件直边方向过多
      （近似圆、样条扁平化）时追加一层 45° 粗网格兜底；
    - ``grid``：旧的等步长枚举（``step`` 度），保留给需要全角度扫描的场景；
    - ``hybrid``：边对齐角度 ∪ ``step`` 网格。

    未传 ``part`` 时保持旧行为（``grid``），避免破坏既有调用方与测试。
    """
    if not arbitrary_rotation:
        return list(rotations)

    if part is None:
        return _grid_angles(step)

    if angle_mode == "grid":
        return _grid_angles(step)
    if angle_mode == "hybrid":
        return sorted({angle % 360.0 for angle in
                       edge_aligned_angles(part) + _grid_angles(step)})

    angles = set(edge_aligned_angles(part))
    if _edge_coverage(part) < _EDGE_COVERAGE_MIN:
        angles.update(_grid_angles(_EDGE_GRID_STEP))
    angles.update(_grid_angles(step) if step and step >= _EDGE_GRID_STEP else [])
    return sorted(angle % 360.0 for angle in angles)


def _rotation_forms(part: Part, angles: list, cache: dict) -> list:
    """缓存零件绕质心旋转后的外轮廓和包围盒。"""
    key = id(part)
    forms = cache.get(key)
    if forms is None:
        outer = part.outer_polygon
        origin = (outer.centroid.x, outer.centroid.y)
        forms = []
        for angle in angles:
            rotated = (affinity.rotate(outer, angle, origin=origin)
                       if angle else outer)
            forms.append((angle, rotated, rotated.bounds))
        cache[key] = forms
    return forms


def _collides(cand, tree: STRtree | None, geoms: list) -> bool:
    """判断候选外轮廓是否与已放面板存在面积重叠；共边不算重叠。"""
    if tree is None or not geoms:
        return False
    for idx in tree.query(cand):
        if shapely.relate_pattern(cand, geoms[idx], _OVERLAP_PATTERN):
            return True
    return False


def _contact_len(cb: tuple, boxes: list, sheet_w: float, sheet_h: float,
                 tol: float = 1.0) -> float:
    """候选包围盒与板边/已放零件包围盒的贴合边总长（越大越紧凑）。

    与桥切引擎 ``nesting._contact_len`` 同一语义，便于两种工艺共用一套
    "贴紧度"直觉。
    """
    x0, y0, x1, y1 = cb
    total = 0.0
    if x0 <= tol:
        total += y1 - y0
    if y0 <= tol:
        total += x1 - x0
    if sheet_w - x1 <= tol:
        total += y1 - y0
    if sheet_h - y1 <= tol:
        total += x1 - x0
    for (px0, py0, px1, py1) in boxes:
        if abs(py1 - y0) <= tol:
            total += max(0.0, min(x1, px1) - max(x0, px0))
        if abs(py0 - y1) <= tol:
            total += max(0.0, min(x1, px1) - max(x0, px0))
        if abs(px1 - x0) <= tol:
            total += max(0.0, min(y1, py1) - max(y0, py0))
        if abs(px0 - x1) <= tol:
            total += max(0.0, min(y1, py1) - max(y0, py0))
    return total


def _placement_score(mode: str, bounds: tuple, boxes: list,
                     placed_boxes: list, sheet_w: float,
                     sheet_h: float) -> tuple:
    """候选放置的评分（越小越优）。

    - ``bottom_left``（旧行为）：直接取放置后的左下角，容易把零件塞进角落却
      留下无法利用的碎片；
    - ``skyline``：先压低"放置后该处轮廓高度"（顶边），再取左边——标准
      Bottom-Left 的 skyline 版本；
    - ``contact``：先最大化贴合边长度（与板边、已放零件的共边），再压低顶边。
      桥切引擎已验证该准则更利于形成可切割的直线共边。
    """
    x0, y0, x1, y1 = bounds
    if mode == "skyline":
        return (y1, x0)
    if mode == "column":
        # 与桥切引擎 "col" 配置同构：优先让零件右边靠左，形成竖向列，
        # 再压低底边。竖向列在长条/窄件为主的图纸上能显著减少板数。
        return (x1, y0)
    if mode == "contact":
        return (
            -_contact_len(bounds, boxes, sheet_w, sheet_h),
            y1,
            x0,
        )
    return (y0, x0)


def _bbox_overlaps_any(cb: tuple, boxes: list) -> bool:
    """候选包围盒是否与任一已放零件包围盒存在正面积重叠。"""
    x0, y0, x1, y1 = cb
    for (px0, py0, px1, py1) in boxes:
        if x1 <= px0 + EPS or x0 >= px1 - EPS:
            continue
        if y1 <= py0 + EPS or y0 >= py1 - EPS:
            continue
        return True
    return False


def _compact_boundary_vertices(geom, limit: int = _NFP_LITE_MAX_VERTICES):
    """返回用于 NFP-lite 接触的轮廓顶点，去掉重复点和共线冗余点。"""
    if geom is None:
        return []
    geometry_type = getattr(geom, "geom_type", "")
    if geometry_type in ("MultiPolygon", "GeometryCollection"):
        vertices = []
        for item in geom.geoms:
            vertices.extend(_compact_boundary_vertices(item, limit))
        if len(vertices) <= limit:
            return vertices
        stride = max(1, math.ceil(len(vertices) / limit))
        return vertices[::stride][:limit]

    exterior = getattr(geom, "exterior", None)
    if exterior is None:
        return []
    coords = list(exterior.coords)
    if len(coords) > 1 and coords[0] == coords[-1]:
        coords.pop()

    compact = []
    for point in coords:
        rounded = (round(float(point[0]), 7), round(float(point[1]), 7))
        if not compact or rounded != compact[-1]:
            compact.append(rounded)
    if len(compact) > 2 and compact[0] == compact[-1]:
        compact.pop()

    changed = True
    while changed and len(compact) > 3:
        changed = False
        filtered = []
        count = len(compact)
        for index, current in enumerate(compact):
            previous = compact[index - 1]
            following = compact[(index + 1) % count]
            ax = current[0] - previous[0]
            ay = current[1] - previous[1]
            bx = following[0] - current[0]
            by = following[1] - current[1]
            cross = ax * by - ay * bx
            scale = max(1.0, math.hypot(ax, ay) * math.hypot(bx, by))
            if abs(cross) <= 1e-10 * scale:
                changed = True
                continue
            filtered.append(current)
        if len(filtered) < 3:
            break
        compact = filtered

    if len(compact) <= limit:
        return compact
    stride = max(1, math.ceil(len(compact) / limit))
    sampled = compact[::stride][:limit]
    return sampled


def _nfp_lite_positions(rotated, rb: tuple, geoms: list,
                        sheet_w: float, sheet_h: float) -> list:
    """顶点接触候选（NFP-lite）。

    对每个已放零件顶点 q 和待放零件顶点 p，计算使 p 与 q 重合的平移量。
    这些候选覆盖了大量 NFP 边界顶点，能进入凹多边形包围盒内部的凹槽；
    最终仍由精确 DE-9IM 碰撞检测筛选，不会把接触当作重叠。
    """
    if rotated is None or not geoms:
        return []
    moving_vertices = _compact_boundary_vertices(rotated)
    static_vertices = []
    for geom in geoms:
        static_vertices.extend(_compact_boundary_vertices(geom))
    if not moving_vertices or not static_vertices:
        return []

    points = set()
    for qx, qy in static_vertices:
        for px, py in moving_vertices:
            x = rb[0] + qx - px
            y = rb[1] + qy - py
            if x < -EPS or y < -EPS:
                continue
            if (
                x + (rb[2] - rb[0]) > sheet_w + EPS
                or y + (rb[3] - rb[1]) > sheet_h + EPS
            ):
                continue
            points.add((round(x, 7), round(y, 7)))
    ordered = sorted(points, key=lambda point: (point[1], point[0]))
    return ordered[:_NFP_LITE_MAX_POSITIONS]


def _candidate_axes(geoms: list, part_w: float, part_h: float,
                    sheet_w: float, sheet_h: float) -> tuple[list, list]:
    """生成候选左下角 x/y 坐标，覆盖板边和已放面板包围盒边缘。"""
    xs = {0.0, sheet_w - part_w}
    ys = {0.0, sheet_h - part_h}
    for geom in geoms:
        x0, y0, x1, y1 = geom.bounds
        xs.add(x1)
        xs.add(x0 - part_w)
        ys.add(y1)
        ys.add(y0 - part_h)

    xs = sorted(x for x in xs if x >= -EPS and x + part_w <= sheet_w + EPS)
    ys = sorted(y for y in ys if y >= -EPS and y + part_h <= sheet_h + EPS)
    return xs, ys


def _candidate_order_key(mode: str, x: float, y: float,
                         part_w: float, part_h: float) -> tuple:
    """候选遍历顺序的粗排序键（与桥切引擎 ``_primary_key`` 同构）。

    先按该键排序再截断 ``_MAX_VALID_PER_ANGLE``，否则截断只会保留"最左下"
    的几个候选，真正能贴紧的位置根本不会被考察。
    """
    if mode == "column":
        return (x + part_w, y)
    return (y + part_h, x)


def _candidate_positions(geoms: list, sheet_w: float, sheet_h: float,
                         part_w: float, part_h: float,
                         *, order: str = "legacy",
                         score_mode: str = "bottom_left",
                         use_reflex: bool = False,
                         rotated=None,
                         candidate_mode: str = "bbox") -> list:
    """候选左下角位置。

    ``order="legacy"`` 保持旧行为（y 升序 × x 升序）；``"sorted"`` 先按放置
    准则排序再截断，与桥切引擎一致。``use_reflex`` 追加"候选包围盒四角对准
    已放零件凹角"的锚点，用于嵌入 L 形缺口。
    """
    xs, ys = _candidate_axes(geoms, part_w, part_h, sheet_w, sheet_h)
    points = {(x, y) for y in ys for x in xs}

    if use_reflex:
        for geom in geoms:
            for (vx, vy) in _reflex_vertices(geom):
                points.add((vx, vy))
                points.add((vx - part_w, vy))
                points.add((vx, vy - part_h))
                points.add((vx - part_w, vy - part_h))

    contact_points = set()
    if candidate_mode == "nfp_lite" and rotated is not None:
        contact_points = set(_nfp_lite_positions(
            rotated, rotated.bounds, geoms, sheet_w, sheet_h
        ))
        points.update(contact_points)

    inside = [
        (x, y)
        for (x, y) in points
        if x >= -EPS and y >= -EPS
        and x + part_w <= sheet_w + EPS
        and y + part_h <= sheet_h + EPS
    ]
    def sort_positions(items):
        if order == "sorted":
            return sorted(
                items,
                key=lambda pt: _candidate_order_key(
                    score_mode, pt[0], pt[1], part_w, part_h
                ),
            )
        return sorted(items, key=lambda pt: (pt[1], pt[0]))

    if contact_points:
        contact_inside = [pt for pt in inside if pt in contact_points]
        other_inside = [pt for pt in inside if pt not in contact_points]
        inside = sort_positions(contact_inside) + sort_positions(other_inside)
    else:
        inside = sort_positions(inside)
    return inside


def _slide_down_left(rotated, rb, x: float, y: float,
                     tree: STRtree | None, geoms: list,
                     sheet_w: float, sheet_h: float):
    """交替向 -x / -y 方向滑动，直到贴住板边或已放面板。"""
    ox = x - rb[0]
    oy = y - rb[1]

    for _ in range(2):
        for dx, dy in ((-1.0, 0.0), (0.0, -1.0)):
            step = 1024.0
            while step > 0.25:
                nx = x + dx * step
                ny = y + dy * step
                if nx < -EPS or ny < -EPS:
                    step *= 0.5
                    continue
                cand = affinity.translate(rotated,
                                          xoff=nx - rb[0],
                                          yoff=ny - rb[1])
                if _collides(cand, tree, geoms):
                    step *= 0.5
                else:
                    x, y = nx, ny
                    ox, oy = nx - rb[0], ny - rb[1]

    return affinity.translate(rotated, xoff=ox, yoff=oy)


def _find_placement(part: Part, geoms: list, tree: STRtree | None,
                    sheet_w: float, sheet_h: float,
                    cache: dict, rotations: tuple,
                    arbitrary_rotation: bool,
                    rotation_step: float = _ARBITRARY_ROTATION_STEP,
                    angle_mode: str = "edge",
                    score_mode: str = "bottom_left",
                    candidate_order: str = "legacy",
                    use_reflex: bool = False,
                    candidate_mode: str = "bbox") -> _Placement | None:
    """为单个零件找一个最左下的可行放置。"""
    angles = angle_candidates(
        rotations, arbitrary_rotation, rotation_step,
        part=part, angle_mode=angle_mode,
    )
    boxes = [g.bounds for g in geoms]
    placed_boxes = boxes
    best = None

    for angle, rotated, rb in _rotation_forms(part, angles, cache):
        part_w = rb[2] - rb[0]
        part_h = rb[3] - rb[1]
        if part_w > sheet_w + EPS or part_h > sheet_h + EPS:
            continue

        positions = _candidate_positions(
            geoms, sheet_w, sheet_h, part_w, part_h,
            order=candidate_order, score_mode=score_mode,
            use_reflex=use_reflex,
            rotated=rotated,
            candidate_mode=candidate_mode,
        )
        valid_count = 0

        for (x, y) in positions:
            cb = (x, y, x + part_w, y + part_h)
            if _bbox_overlaps_any(cb, boxes):
                cand = affinity.translate(rotated,
                                          xoff=x - rb[0],
                                          yoff=y - rb[1])
                if _collides(cand, tree, geoms):
                    continue

            placed = _slide_down_left(rotated, rb, x, y, tree, geoms,
                                      sheet_w, sheet_h)
            b = placed.bounds
            score = _placement_score(
                score_mode, b, boxes, placed_boxes, sheet_w, sheet_h
            )
            if best is None or score < best[0]:
                best = (score, angle, placed)

            valid_count += 1
            valid_limit = (
                _MAX_VALID_PER_ANGLE_NFP
                if candidate_mode == "nfp_lite"
                else _MAX_VALID_PER_ANGLE
            )
            if valid_count >= valid_limit:
                break

        # 已找到贴 (0,0) 的放置就不必再试更多角度（评分准则不同，判断放在
        # 实际包围盒上，避免把新评分元组当成坐标比较）。
        if best is not None:
            bb = best[2].bounds
            if bb[0] <= EPS and bb[1] <= EPS:
                break

    if best is None:
        return None
    return _Placement(part=part, rot=best[1], poly=best[2])


def _dims(part: Part) -> tuple:
    b = part.outer_polygon.bounds
    return b[2] - b[0], b[3] - b[1]


def _make_sort_key(name: str, seed: int = 0):
    if name == "short":
        return lambda p: (-min(_dims(p)), -max(_dims(p)))
    if name == "long":
        return lambda p: (-max(_dims(p)), -min(_dims(p)))
    if name == "jitter":
        rng = random.Random(seed)
        return lambda p: -p.area * rng.uniform(0.9, 1.1)
    return lambda p: -p.area


def _normalize_score_mode(mode: str) -> str:
    aliases = {"col": "column", "cols": "column"}
    normalized = aliases.get(str(mode).casefold(), str(mode).casefold())
    if normalized not in _DEEPNEST_SCORE_MODES:
        raise ValueError(
            f"未知的水刀/激光放置准则 {mode!r}；"
            f"可选值：{', '.join(_DEEPNEST_SCORE_MODES)}"
        )
    return normalized


def _normalize_candidate_mode(mode: str) -> str:
    normalized = str(mode or "bbox").casefold()
    if normalized not in _DEEPNEST_CANDIDATE_MODES:
        raise ValueError(
            f"未知的候选位置模式 {mode!r}；"
            f"可选值：{', '.join(_DEEPNEST_CANDIDATE_MODES)}"
        )
    return normalized


def _expand_search_configs(
    configs: list | None,
    *,
    trials: int,
    seed: int,
    default_score_mode: str,
    default_candidate_order: str,
    use_reflex: bool,
) -> list[tuple[str, str, int, str, bool]]:
    """展开水刀/激光搜索配置。

    与桥切引擎兼容的二元/三元组格式为
    ``(sort_name, score_mode[, seed])``；额外两项可显式指定
    ``candidate_order`` 与 ``use_reflex``。也接受同样字段的 dict。
    ``configs`` 优先于 ``trials``，避免 quick 配置被 trials 静默忽略。
    """
    normalized: list[tuple[str, str, int, str, bool]] = []
    if configs:
        raw_configs = list(configs)
    else:
        raw_configs = [
            (
                _DEEPNEST_SORT_MODES[i % len(_DEEPNEST_SORT_MODES)],
                default_score_mode,
                seed + i,
            )
            for i in range(max(1, trials))
        ]

    for index, raw in enumerate(raw_configs):
        if isinstance(raw, dict):
            sort_name = raw.get("sort", "area")
            score_mode = raw.get("score_mode", default_score_mode)
            config_seed = raw.get("seed", seed + index)
            candidate_order = raw.get("candidate_order",
                                      default_candidate_order)
            reflex = bool(raw.get("use_reflex", use_reflex))
        else:
            values = tuple(raw) if isinstance(raw, (tuple, list)) else (raw,)
            sort_name = values[0] if len(values) > 0 and values[0] else "area"
            score_mode = (
                values[1]
                if len(values) > 1 and values[1] is not None
                else default_score_mode
            )
            config_seed = (
                values[2]
                if len(values) > 2 and values[2] is not None
                else seed + index
            )
            candidate_order = (
                values[3]
                if len(values) > 3 and values[3] is not None
                else default_candidate_order
            )
            reflex = bool(values[4]) if len(values) > 4 else use_reflex

        sort_name = str(sort_name).casefold()
        if sort_name not in _DEEPNEST_SORT_MODES:
            raise ValueError(
                f"未知的水刀/激光排序 {sort_name!r}；"
                f"可选值：{', '.join(_DEEPNEST_SORT_MODES)}"
            )
        score_mode = _normalize_score_mode(score_mode)
        candidate_order = str(candidate_order).casefold()
        if candidate_order not in _DEEPNEST_CANDIDATE_ORDERS:
            raise ValueError(
                f"未知的候选坐标顺序 {candidate_order!r}；"
                f"可选值：{', '.join(_DEEPNEST_CANDIDATE_ORDERS)}"
            )
        config = (
            sort_name,
            score_mode,
            int(config_seed),
            candidate_order,
            reflex,
        )
        if config not in normalized:
            normalized.append(config)

    return normalized


def _greedy_pass(parts: list, sheet_w: float, sheet_h: float,
                 cache: dict, rotations: tuple,
                 arbitrary_rotation: bool, sort_key,
                 first_part_left_edge: bool = False,
                 rotation_step: float = _ARBITRARY_ROTATION_STEP,
                 angle_mode: str = "edge",
                 score_mode: str = "bottom_left",
                 candidate_order: str = "legacy",
                 use_reflex: bool = False,
                 candidate_mode: str = "bbox") -> list:
    """一轮 Bottom-Left Fill：逐张开板，无法放入当前板的进入下一张。"""
    remaining = sorted(parts, key=sort_key)
    sheets_pl = []

    while remaining:
        cur: list[_Placement] = []
        geoms: list = []
        tree: STRtree | None = None
        nxt: list = []

        for part in remaining:
            if not cur and first_part_left_edge:
                placement = _first_part_left_placement(
                    part, sheet_w, sheet_h, rotations, arbitrary_rotation
                )
                if placement is None:
                    placement = _find_placement(
                        part, geoms, tree, sheet_w, sheet_h,
                        cache, rotations, arbitrary_rotation,
                        rotation_step, angle_mode, score_mode,
                        candidate_order, use_reflex, candidate_mode,
                    )
            else:
                placement = _find_placement(
                    part, geoms, tree, sheet_w, sheet_h,
                    cache, rotations, arbitrary_rotation,
                    rotation_step, angle_mode, score_mode,
                    candidate_order, use_reflex, candidate_mode,
                )
            if placement is None:
                nxt.append(part)
                continue
            cur.append(placement)
            geoms.append(placement.poly)
            tree = STRtree(geoms)

        if not cur:
            numbers = [p.number for p in remaining]
            raise ValueError(
                f"以下零件无法放入 {sheet_w:.0f}x{sheet_h:.0f} 大板：{numbers}"
            )
        sheets_pl.append(cur)
        remaining = nxt

    return sheets_pl


def _sheet_used_area(placements: list) -> float:
    return sum(pl.part.area for pl in placements)


def _sheet_outer_area(placements: list) -> float:
    """用于可行性判断的外包络面积（不能用扣孔后的净面积高估容量）。"""
    return sum(pl.poly.area for pl in placements)


def _improve_low_fill_sheets(
    sheets_pl: list,
    sheet_w: float,
    sheet_h: float,
    cache: dict,
    rotations: tuple,
    arbitrary_rotation: bool,
    sort_key,
    *,
    first_part_left_edge: bool = False,
    rotation_step: float = _ARBITRARY_ROTATION_STEP,
    angle_mode: str = "edge",
    score_mode: str = "bottom_left",
    candidate_order: str = "legacy",
    use_reflex: bool = False,
    candidate_mode: str = "bbox",
    threshold: float = 0.6,
    seed: int = 0,
    configs: list | None = None,
    improve_budget: float = 0.0,
    max_attempts: int = _LOW_FILL_MAX_ATTEMPTS,
    max_subset: int = _LOW_FILL_MAX_SUBSET,
    max_rounds: int = _LOW_FILL_MAX_ROUNDS,
    stats: dict | None = None,
) -> list:
    """有界低填充板重排（改进计划 Step 2）。

    纯贪心"当前板放不下就开新板、永不回头"，会留下 sheet 7 那种"2 件占一板"
    的碎片板。这里先尝试所有低填充板，再尝试"最弱板 + 若干 donor 板"的小子集；
    每个子集用多套排序/放置准则重排，只有板数严格减少时才接受。

    ``max_attempts`` 是确定性护栏，``improve_budget``（>0）是可选墙钟护栏。
    任何护栏都只停止后续尝试，不会丢掉当前完整布局，因此结果始终包含全部
    零件且不会比输入布局更差。使用 ``sort_key`` 仅用于兼容旧调用；新搜索优先
    使用 ``configs``，其中的候选顺序与凹角开关也会实际生效。
    """
    sheet_area = sheet_w * sheet_h
    if sheet_area <= 0 or len(sheets_pl) < 2:
        return sheets_pl

    # 重排是一个独立的小子问题：即使基础搜索只给了一套配置，也应换用不同
    # 排序和放置准则，否则重排会机械复现原布局。
    rework_configs = list(configs or [])
    # 先沿用历史上表现最稳定的桥切 quick 组合；DeepNest 的放置评分接受
    # skyline/column 语义，配置只影响候选顺序，不改变工艺合法性。
    rework_configs.extend([
        ("short", "skyline", seed, candidate_order, use_reflex),
        ("short", "column", seed, candidate_order, use_reflex),
        ("area", "skyline", seed, candidate_order, use_reflex),
        ("long", "column", seed, candidate_order, use_reflex),
    ])
    rework_configs.extend(
        (sort_name, mode, seed, candidate_order, use_reflex)
        for sort_name in _DEEPNEST_SORT_MODES
        for mode in ("bottom_left", "skyline", "column", "contact")
    )
    search_configs = _expand_search_configs(
        rework_configs,
        trials=1,
        seed=seed,
        default_score_mode=score_mode,
        default_candidate_order=candidate_order,
        use_reflex=use_reflex,
    )
    deadline = (
        time.monotonic() + improve_budget
        if improve_budget and improve_budget > 0
        else None
    )
    attempts = 0
    current = sheets_pl
    accepted_rounds = 0
    budget_exhausted = False
    attempt_limit_hit = False

    def _selection_candidates(layout: list) -> list[list[int]]:
        n = len(layout)
        fill_order = sorted(
            range(n), key=lambda i: (_sheet_outer_area(layout[i]), i)
        )
        weakest = fill_order[0]
        low = [
            i for i in fill_order
            if _sheet_used_area(layout[i]) < threshold * sheet_area
        ]
        selections: list[list[int]] = []
        seen: set[tuple[int, ...]] = set()

        def add(indices: list[int]) -> None:
            key = tuple(sorted(indices))
            if len(key) < 2 or key in seen:
                return
            seen.add(key)
            selections.append(list(key))

        # 小池子先整体重排通常最划算；大池子放最后，避免一次尝试吃掉预算。
        if 2 <= len(low) <= max_subset:
            add(low)
        by_parts = sorted(range(n), key=lambda i: (-len(layout[i]), i))
        rng = random.Random(seed * 1009 + n)
        for k in range(2, min(max_subset, n) + 1):
            add(fill_order[:k])
            add([weakest] + [
                i for i in by_parts if i != weakest
            ][:k - 1])
            pool = [i for i in fill_order if i != weakest]
            if len(pool) >= k - 1:
                add([weakest] + rng.sample(pool, k - 1))
            if k == 2:
                # 最弱板与每个 donor 单独试一次，覆盖"两张并一张"的最常见场景。
                for donor in fill_order[1:min(len(fill_order), 13)]:
                    add([weakest, donor])
        if len(low) >= 2:
            add(low)
        return selections

    for _round in range(max(1, max_rounds)):
        before_count = len(current)
        best_layout = None
        best_key = None
        stopped = False

        for selection in _selection_candidates(current):
            if stopped:
                break
            selected_area = sum(
                _sheet_outer_area(current[i]) for i in selection
            )
            target_sheets = len(selection) - 1
            if selected_area > target_sheets * sheet_area * _LOW_FILL_AREA_CAP:
                continue

            selected_set = set(selection)
            kept = [
                placements
                for index, placements in enumerate(current)
                if index not in selected_set
            ]
            pool = [
                placement.part
                for index in selection
                for placement in current[index]
            ]
            for config in search_configs:
                if attempts >= max(1, max_attempts):
                    stopped = True
                    attempt_limit_hit = True
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    stopped = True
                    budget_exhausted = True
                    break
                attempts += 1
                sort_name, config_mode, config_seed, order, reflex = config
                retry = _greedy_pass(
                    pool, sheet_w, sheet_h, cache, rotations,
                    arbitrary_rotation, _make_sort_key(sort_name, config_seed),
                    first_part_left_edge=first_part_left_edge,
                    rotation_step=rotation_step,
                    angle_mode=angle_mode,
                    score_mode=config_mode,
                    candidate_order=order,
                    use_reflex=reflex,
                    candidate_mode=candidate_mode,
                )
                if len(retry) > target_sheets:
                    continue
                candidate_layout = kept + retry
                key = (len(candidate_layout), _compactness(candidate_layout))
                if best_key is None or key < best_key:
                    best_key = key
                    best_layout = candidate_layout

        if stopped and best_layout is None:
            break
        if best_layout is None or len(best_layout) >= before_count:
            break
        current = best_layout
        accepted_rounds += 1

    if stats is not None:
        stats.update({
            "deepnest_low_fill_attempts": attempts,
            "deepnest_low_fill_accepted_rounds": accepted_rounds,
            "deepnest_low_fill_budget_exhausted": budget_exhausted,
            "deepnest_low_fill_attempt_limit_hit": attempt_limit_hit,
        })
    return current


def _compactness(sheets_pl: list) -> float:
    """同板数时选择更紧凑的方案。"""
    total = 0.0
    for sheet in sheets_pl:
        top = max((pl.poly.bounds[3] for pl in sheet), default=0.0)
        total += top
    return total


def _materialize(sheets_pl: list, sheet_w: float, sheet_h: float,
                 thickness: float, unit: str, total_parts: int) -> NestingResult:
    """把内部 placement 转换为 NestingResult，并同步变换孔洞/编号标签。"""
    sheets = []
    for sheet_index, placements in enumerate(sheets_pl, start=1):
        sheet = Sheet(index=sheet_index, width=sheet_w, height=sheet_h,
                      thickness=thickness)
        for pl in placements:
            part = pl.part
            outer = part.outer_polygon
            origin = (outer.centroid.x, outer.centroid.y)
            rotated_outer = (affinity.rotate(outer, pl.rot, origin=origin)
                             if pl.rot else outer)
            ox = pl.poly.bounds[0] - rotated_outer.bounds[0]
            oy = pl.poly.bounds[1] - rotated_outer.bounds[1]

            def transform(geom):
                rotated = (affinity.rotate(geom, pl.rot, origin=origin)
                           if pl.rot else geom)
                return affinity.translate(rotated, xoff=ox, yoff=oy)

            label = transform(Point(part.label_position))
            placed_part = Part(
                id=part.id,
                number=part.number,
                polygon=transform(part.polygon),
                outer_polygon=pl.poly,
                holes=[transform(h) for h in part.holes],
                original_number=part.original_number,
                material_group=part.material_group,
                group_id=part.group_id,
                area=part.area,
                label_position=(label.x, label.y),
            )
            sheet.parts.append(placed_part)
        sheets.append(sheet)

    total_part_area = sum(p.area for pls in sheets_pl for pl in pls for p in [pl.part])
    total_sheet_area = sum(s.total_area for s in sheets)
    return NestingResult(
        sheets=sheets,
        unit=unit,
        total_parts=total_parts,
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


def nest_parts_deepnest(parts: list, sheet_width: float, sheet_height: float,
                        sheet_thickness: float, unit: str = "metric",
                        configs: list | None = None,
                        improve_budget: float = 0.0,
                        trials: int = 1,
                        seed: int = 0,
                        rotations: tuple = (0, 90, 180, 270),
                        arbitrary_rotation: bool = False,
                        first_part_left_edge: bool = False,
                        rotation_step: float = _ARBITRARY_ROTATION_STEP,
                        angle_mode: str = "edge",
                        score_mode: str = "bottom_left",
                        candidate_order: str = "legacy",
                        use_reflex: bool = False,
                        candidate_mode: str = "bbox",
                        low_fill_rework: bool = False,
                        low_fill_threshold: float = 0.6,
                        stats: dict | None = None,
                        progress=None) -> NestingResult:
    """DeepNest 风格排板入口。

    ``configs`` 现在会实际驱动排序、放置准则、候选顺序和凹角候选搜索；
    与桥切兼容的配置至少包含前三个元素 ``(sort, score_mode, seed)``。
    ``configs`` 优先于 ``trials``。``improve_budget`` 是可选墙钟护栏：
    只在开始下一套完整配置前检查，不会截断一套配置而产生丢件。

    ``low_fill_rework`` 开启后，在全部基础配置完成后对低填充板和小子集做
    有界联合重排；只接受板数严格减少的完整结果。
    """
    if not parts:
        raise ValueError("DeepNest 排板需要至少一个零件")

    candidate_mode = _normalize_candidate_mode(candidate_mode)
    search_configs = _expand_search_configs(
        configs,
        trials=trials,
        seed=seed,
        default_score_mode=score_mode,
        default_candidate_order=candidate_order,
        use_reflex=use_reflex,
    )
    if stats is not None:
        stats.update({
            "deepnest_search_configs": len(search_configs),
            "deepnest_low_fill_rework": bool(low_fill_rework),
            "deepnest_low_fill_threshold": float(low_fill_threshold),
            "deepnest_candidate_mode": candidate_mode,
        })
    cache: dict = {}
    best_pl = None
    best_key = None
    started = time.monotonic()

    for config in search_configs:
        if (
            best_pl is not None
            and improve_budget
            and improve_budget > 0
            and time.monotonic() - started >= improve_budget
        ):
            break
        name, config_mode, config_seed, order, reflex = config
        sort_key = _make_sort_key(name, config_seed)
        sheets_pl = _greedy_pass(parts, sheet_width, sheet_height, cache,
                                 rotations, arbitrary_rotation, sort_key,
                                 first_part_left_edge=first_part_left_edge,
                                 rotation_step=rotation_step,
                                 angle_mode=angle_mode,
                                 score_mode=config_mode,
                                 candidate_order=order,
                                 use_reflex=reflex,
                                 candidate_mode=candidate_mode)
        key = (len(sheets_pl), _compactness(sheets_pl))
        if best_key is None or key < best_key:
            best_key = key
            best_pl = sheets_pl

    if low_fill_rework and best_pl is not None:
        elapsed = time.monotonic() - started
        remaining_budget = (
            max(0.0, improve_budget - elapsed)
            if improve_budget and improve_budget > 0
            else 0.0
        )
        if not (improve_budget and improve_budget > 0
                and remaining_budget <= 0):
            best_pl = _improve_low_fill_sheets(
                best_pl, sheet_width, sheet_height, cache,
                rotations, arbitrary_rotation,
                _make_sort_key("area", seed),
                first_part_left_edge=first_part_left_edge,
                rotation_step=rotation_step,
                angle_mode=angle_mode,
                score_mode=score_mode,
                candidate_order=candidate_order,
                use_reflex=use_reflex,
            candidate_mode=candidate_mode,
                threshold=low_fill_threshold,
                seed=seed,
                configs=search_configs,
                improve_budget=remaining_budget,
                stats=stats,
            )

    placed_count = sum(len(sheet) for sheet in (best_pl or []))
    if placed_count != len(parts):
        raise RuntimeError(
            f"DeepNest 排板结果不完整：输入 {len(parts)} 件，"
            f"输出 {placed_count} 件"
        )
    result = _materialize(best_pl, sheet_width, sheet_height,
                          sheet_thickness, unit, len(parts))
    errors = validate_nesting(result, sheet_width, sheet_height)
    if errors:
        raise RuntimeError("DeepNest 排板校验失败：" + "; ".join(errors))
    return result
