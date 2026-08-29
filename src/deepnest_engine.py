"""DeepNest 风格排板后端。

目标：水刀/激光加工时允许任意角度旋转，使用 Bottom-Left Fill + 滑动压实，
快速得到一个密集排板方案。该模块只负责生成 placement，编号、孔洞和 DXF
输出仍由主流程完成。

当前实现是自写简化版：
- arbitrary_rotation=False：使用传入的离散 rotations。
- arbitrary_rotation=True：按 1 度步长枚举 0~359 度。
- 候选位置来自大板边界和已放面板包围盒边缘。
- 每个候选位置做 -x / -y 滑动压实，取最左下位置。
"""

import math
import random
from dataclasses import dataclass

import shapely
from shapely import affinity
from shapely.geometry import Point
from shapely.strtree import STRtree

from src.models import Part, Sheet, NestingResult
from src.nesting import validate_nesting

EPS = 1e-6
_OVERLAP_PATTERN = "T********"
_MAX_VALID_PER_ANGLE = 4
_ARBITRARY_ROTATION_STEP = 5.0


def _edge_segments(poly):
    coords = list(poly.exterior.coords)
    return [(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]


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


def angle_candidates(rotations: tuple, arbitrary_rotation: bool,
                     step: float = _ARBITRARY_ROTATION_STEP) -> list:
    """返回待尝试的旋转角度。"""
    if not arbitrary_rotation:
        return list(rotations)
    count = int(round(360.0 / step))
    return [round(i * step, 4) for i in range(count)]


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
                    rotation_step: float = _ARBITRARY_ROTATION_STEP) -> _Placement | None:
    """为单个零件找一个最左下的可行放置。"""
    angles = angle_candidates(rotations, arbitrary_rotation, rotation_step)
    boxes = [g.bounds for g in geoms]
    best = None

    for angle, rotated, rb in _rotation_forms(part, angles, cache):
        part_w = rb[2] - rb[0]
        part_h = rb[3] - rb[1]
        if part_w > sheet_w + EPS or part_h > sheet_h + EPS:
            continue

        xs, ys = _candidate_axes(geoms, part_w, part_h, sheet_w, sheet_h)
        valid_count = 0

        for y in ys:
            for x in xs:
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
                score = (b[1], b[0])
                if best is None or score < best[0]:
                    best = (score, angle, placed)

                valid_count += 1
                if valid_count >= _MAX_VALID_PER_ANGLE:
                    break
            if valid_count >= _MAX_VALID_PER_ANGLE:
                break

        if best is not None and best[0][0] <= EPS and best[0][1] <= EPS:
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


def _greedy_pass(parts: list, sheet_w: float, sheet_h: float,
                 cache: dict, rotations: tuple,
                 arbitrary_rotation: bool, sort_key,
                 first_part_left_edge: bool = False,
                 rotation_step: float = _ARBITRARY_ROTATION_STEP) -> list:
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
                    placement = _find_placement(part, geoms, tree, sheet_w, sheet_h,
                                                cache, rotations, arbitrary_rotation,
                                                rotation_step)
            else:
                placement = _find_placement(part, geoms, tree, sheet_w, sheet_h,
                                            cache, rotations, arbitrary_rotation,
                                            rotation_step)
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
                        progress=None) -> NestingResult:
    """DeepNest 风格排板入口。"""
    if not parts:
        raise ValueError("DeepNest 排板需要至少一个零件")

    cache: dict = {}
    best_pl = None
    best_key = None
    order_names = ["area", "short", "long", "jitter"]

    for trial in range(max(1, trials)):
        name = order_names[trial % len(order_names)]
        sort_key = _make_sort_key(name, seed + trial)
        sheets_pl = _greedy_pass(parts, sheet_width, sheet_height, cache,
                                 rotations, arbitrary_rotation, sort_key,
                                 first_part_left_edge=first_part_left_edge,
                                 rotation_step=rotation_step)
        key = (len(sheets_pl), _compactness(sheets_pl))
        if best_key is None or key < best_key:
            best_key = key
            best_pl = sheets_pl

    result = _materialize(best_pl, sheet_width, sheet_height,
                          sheet_thickness, unit, len(parts))
    errors = validate_nesting(result, sheet_width, sheet_height)
    if errors:
        raise RuntimeError("DeepNest 排板校验失败：" + "; ".join(errors))
    return result
