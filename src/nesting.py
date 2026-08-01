"""排板算法：多轮贪心 BFD + 多评分模式 + 滑动压实 + 精确校验

核心思路：
1. 候选位置来自"边对齐"：零件左边贴已放零件右边、底边贴已放零件顶边，
   以及凹角（缺口）顶点锚定，保证行/列能凑齐、L 形缺口能被利用。
2. 多种排序键（面积 / 短边 / 长边 / 随机扰动）× 多种评分模式
   （skyline 压低轮廓 / column 收窄横向 / contact 最大化贴合），
   多轮运行取最优，打破单次贪心的排序陷阱。
3. 每次放置后做 -x / -y 方向滑动压实（步长递减），消除浮空缝隙。
4. 每轮结果做精确校验（边界 + 两两重叠），只接受通过的方案。
"""

import random
import time
from dataclasses import dataclass

import shapely
from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.strtree import STRtree

from src.models import Part, Sheet, NestingResult

ALLOWED_ROTATIONS = (0, 90, 180, 270)
EPS = 1e-6
# DE-9IM 模式：两多边形内部相交 = 存在面积重叠；边缘贴合（共边）不算重叠
_OVERLAP_PATTERN = "T********"
# 每个旋转角下，通过碰撞检测的候选点上限（评分再从中择优）
_MAX_VALID_PER_ROT = 8


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------

@dataclass
class _Placement:
    """一次放置记录（引用原始 Part，不做几何修改）"""
    part: Part
    rot: int                 # 旋转角
    x: float                 # 放置后包围盒左下角 x
    y: float                 # 放置后包围盒左下角 y
    poly: Polygon            # 放置后的外轮廓多边形


# ---------------------------------------------------------------------------
# 几何工具
# ---------------------------------------------------------------------------

def _rotation_forms(part: Part, cache: dict) -> list:
    """零件的 4 个旋转形态（绕外轮廓质心旋转），返回 [(rot, polygon, bounds)]"""
    key = id(part)
    forms = cache.get(key)
    if forms is None:
        outer = part.outer_polygon
        origin = (outer.centroid.x, outer.centroid.y)
        forms = []
        for a in ALLOWED_ROTATIONS:
            rp = affinity.rotate(outer, a, origin=origin) if a else outer
            forms.append((a, rp, rp.bounds))
        cache[key] = forms
    return forms


def _reflex_vertices(poly: Polygon) -> list:
    """外轮廓的凹角顶点（内角 > 180°），用于缺口互嵌候选点"""
    coords = list(poly.exterior.coords)
    n = len(coords) - 1
    if n < 4:
        return []
    # 多边形环方向（顺时针时凹角判定取反）
    signed = sum((coords[i + 1][0] - coords[i][0]) *
                 (coords[i + 1][1] + coords[i][1]) for i in range(n))
    sign = 1.0 if signed > 0 else -1.0
    verts = []
    for i in range(n):
        x0, y0 = coords[(i - 1) % n]
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        cross = (x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1)
        if cross * sign > 1e-6:
            verts.append((x1, y1))
    return verts


def _collides(cand: Polygon, tree: STRtree, geoms: list) -> bool:
    """候选多边形与已放零件是否存在面积重叠（共边允许）"""
    if not geoms:
        return False
    for idx in tree.query(cand):
        if shapely.relate_pattern(cand, geoms[idx], _OVERLAP_PATTERN):
            return True
    return False


# ---------------------------------------------------------------------------
# 候选点生成与评分
# ---------------------------------------------------------------------------

def _candidate_points(boxes: list, reflex: list,
                      sheet_w: float, sheet_h: float,
                      pw: float, ph: float) -> list:
    """生成候选左下角位置（已按包围盒可行性过滤）"""
    xs = {0.0, sheet_w - pw}
    ys = {0.0, sheet_h - ph}
    for (x0, y0, x1, y1) in boxes:
        xs.add(x1)          # 候选左边贴已放零件右边
        xs.add(x0 - pw)     # 候选右边贴已放零件左边
        ys.add(y1)          # 候选底边贴已放零件顶边
        ys.add(y0 - ph)     # 候选顶边贴已放零件底边
    rights = sorted({b[2] for b in boxes})
    tops = sorted({b[3] for b in boxes})

    pts = set()
    for x in xs:
        pts.add((x, 0.0))
    for y in ys:
        pts.add((0.0, y))
    # 右边 × 顶边 网格：行/列对齐的主力候选
    for x in rights:
        for y in tops:
            pts.add((x, y))
    # 凹角锚定：候选包围盒四角对准已放零件的凹角，利用 L 形缺口
    for (vx, vy) in reflex:
        pts.add((vx, vy))
        pts.add((vx - pw, vy))
        pts.add((vx, vy - ph))
        pts.add((vx - pw, vy - ph))

    out = []
    for (x, y) in pts:
        if x < -EPS or y < -EPS:
            continue
        if x + pw > sheet_w + EPS or y + ph > sheet_h + EPS:
            continue
        out.append((x, y))
    return out


def _primary_key(mode: str, x: float, y: float, pw: float, ph: float):
    """候选点粗排序键（决定碰撞检测的遍历顺序，先检最优方向）"""
    if mode == "col":
        return (x + pw, y)
    return (y + ph, x)      # skyline / contact 统一先压低轮廓


def _contact_len(cb: tuple, boxes: list, sheet_w: float, sheet_h: float,
                 tol: float = 1.0) -> float:
    """候选包围盒与板边 / 已放零件包围盒的贴合边总长度（近似贴紧度）"""
    x0, y0, x1, y1 = cb
    c = 0.0
    if x0 <= tol:
        c += y1 - y0
    if y0 <= tol:
        c += x1 - x0
    if sheet_w - x1 <= tol:
        c += y1 - y0
    if sheet_h - y1 <= tol:
        c += x1 - x0
    for (px0, py0, px1, py1) in boxes:
        if abs(py1 - y0) <= tol:      # 底边贴对方顶边
            c += max(0.0, min(x1, px1) - max(x0, px0))
        if abs(py0 - y1) <= tol:      # 顶边贴对方底边
            c += max(0.0, min(x1, px1) - max(x0, px0))
        if abs(px1 - x0) <= tol:      # 左边贴对方右边
            c += max(0.0, min(y1, py1) - max(y0, py0))
        if abs(px0 - x1) <= tol:      # 右边贴对方左边
            c += max(0.0, min(y1, py1) - max(y0, py0))
    return c


def _full_score(mode: str, cb: tuple, boxes: list,
                sheet_w: float, sheet_h: float) -> tuple:
    """候选最终评分（越小越优）"""
    x0, y0, x1, y1 = cb
    if mode == "col":
        return (x1, y0)
    if mode == "contact":
        return (-_contact_len(cb, boxes, sheet_w, sheet_h), y1, x0)
    return (y1, x0)  # skyline


# ---------------------------------------------------------------------------
# 放置搜索
# ---------------------------------------------------------------------------

def _slide(rp: Polygon, rb: tuple, x: float, y: float,
           tree: STRtree, geoms: list,
           sheet_w: float, sheet_h: float) -> tuple:
    """滑动压实：交替向 -x / -y 方向以递减步长移动，直到贴紧"""
    pw, ph = rb[2] - rb[0], rb[3] - rb[1]
    ox, oy = x - rb[0], y - rb[1]
    for _ in range(2):
        for dx, dy in ((-1.0, 0.0), (0.0, -1.0)):
            step = 1024.0
            while step > 0.25:
                nx, ny = x + dx * step, y + dy * step
                if nx < -EPS or ny < -EPS:
                    step *= 0.5
                    continue
                cand = affinity.translate(rp, xoff=nx - rb[0], yoff=ny - rb[1])
                if _collides(cand, tree, geoms):
                    step *= 0.5
                else:
                    x, y = nx, ny
                    ox, oy = nx - rb[0], ny - rb[1]
    return x, y, affinity.translate(rp, xoff=ox, yoff=oy)


def _find_placement(part: Part, geoms: list, boxes: list, reflex: list,
                    sheet_w: float, sheet_h: float,
                    mode: str, cache: dict) -> _Placement | None:
    """为单个零件搜索最优放置位置，找不到返回 None"""
    tree = STRtree(geoms) if geoms else None
    best = None
    best_score = None

    for rot, rp, rb in _rotation_forms(part, cache):
        pw, ph = rb[2] - rb[0], rb[3] - rb[1]
        if pw > sheet_w + EPS or ph > sheet_h + EPS:
            continue
        pts = _candidate_points(boxes, reflex, sheet_w, sheet_h, pw, ph)
        pts.sort(key=lambda pt: _primary_key(mode, pt[0], pt[1], pw, ph))

        valid = []
        for (x, y) in pts:
            cand = affinity.translate(rp, xoff=x - rb[0], yoff=y - rb[1])
            if tree is not None and _collides(cand, tree, geoms):
                continue
            valid.append((x, y, cand))
            if len(valid) >= _MAX_VALID_PER_ROT:
                break
        if not valid:
            continue

        for (x, y, cand) in valid:
            cb = (x, y, x + pw, y + ph)
            score = _full_score(mode, cb, boxes, sheet_w, sheet_h)
            if best_score is None or score < best_score:
                best_score = score
                best = (rot, x, y, cand, rp, rb)

    if best is None:
        return None
    rot, x, y, _cand, rp, rb = best
    # 滑动压实（只向 -x/-y 移动，不会变差）
    x, y, cand = _slide(rp, rb, x, y, tree, geoms, sheet_w, sheet_h)
    return _Placement(part=part, rot=rot, x=x, y=y, poly=cand)


# ---------------------------------------------------------------------------
# 单轮贪心排板
# ---------------------------------------------------------------------------

def _nest_single(parts: list, sheet_w: float, sheet_h: float,
                 sort_key, mode: str, cache: dict,
                 max_sheets: int | None = None) -> list | None:
    """单轮贪心：按 sort_key 排序，逐张填满大板。

    max_sheets：大板数量上限，超过则放弃本轮（返回 None）。
    """
    remaining = sorted(parts, key=sort_key)
    sheets = []  # list[list[_Placement]]

    while remaining:
        if max_sheets is not None and len(sheets) >= max_sheets:
            return None
        cur: list[_Placement] = []
        geoms: list = []
        boxes: list = []
        reflex: list = []
        nxt = []

        for part in remaining:
            pl = _find_placement(part, geoms, boxes, reflex,
                                 sheet_w, sheet_h, mode, cache)
            if pl is None:
                nxt.append(part)
                continue
            cur.append(pl)
            geoms.append(pl.poly)
            boxes.append(pl.poly.bounds)
            reflex.extend(_reflex_vertices(pl.poly))

        if not cur:
            numbers = [p.number for p in remaining]
            raise ValueError(
                f"以下零件无法放入 {sheet_w:.0f}x{sheet_h:.0f} 大板：{numbers}")
        sheets.append(cur)
        remaining = nxt

    return sheets


# ---------------------------------------------------------------------------
# 排序键与多轮配置
# ---------------------------------------------------------------------------

def _dims(part: Part) -> tuple:
    b = part.outer_polygon.bounds
    return b[2] - b[0], b[3] - b[1]


def _make_sort_key(name: str, seed: int):
    if name == "area":
        return lambda p: -p.area
    if name == "short":   # 短边降序：同高度档相邻，自然成行
        return lambda p: (-min(_dims(p)), -max(_dims(p)))
    if name == "long":    # 长边降序：先放超长件
        return lambda p: (-max(_dims(p)), -min(_dims(p)))
    if name == "jitter":  # 面积降序 + 随机扰动，打破排序僵局
        rng = random.Random(seed)
        jitter = {}

        def key(p):
            k = id(p)
            if k not in jitter:
                jitter[k] = rng.uniform(0.9, 1.1)
            return -p.area * jitter[k]
        return key
    raise ValueError(f"未知排序键: {name}")


def _default_configs() -> list:
    """多轮启动配置：(排序键, 评分模式, 随机种子)"""
    return [
        ("short", "skyline", 0),
        ("short", "col", 0),
        ("area", "skyline", 0),
        ("area", "contact", 0),
        ("long", "col", 0),
        ("long", "skyline", 0),
        ("jitter", "skyline", 1),
        ("jitter", "col", 2),
        ("jitter", "contact", 3),
        ("short", "contact", 0),
    ]


def _sheet_used_area(pls: list) -> float:
    return sum(pl.part.area for pl in pls)


def _concentration(sheets_pl: list) -> float:
    """填充集中度：各板已用面积平方和。越大说明填充越向少数板集中，
    空板/半空板越多，越容易整板释放。"""
    return sum(_sheet_used_area(s) ** 2 for s in sheets_pl)


def _lns_improve(sheets_pl: list, sheet_w: float, sheet_h: float,
                 cache: dict, budget_s: float = 40.0,
                 seed: int = 0, progress=None) -> list:
    """大邻域搜索：反复拆除利用率偏低的若干张板并重排，
    同数重排提升填充集中度（中性移动），偶尔直接压缩板数。"""
    rng = random.Random(seed)
    sheet_area = sheet_w * sheet_h

    cur = sheets_pl
    cur_key = (len(cur), -_concentration(cur))
    best, best_key = cur, cur_key
    t0 = time.time()
    it = 0

    while time.time() - t0 < budget_s:
        it += 1
        n = len(cur)
        k = min(rng.choice((2, 3, 3, 4, 4, 5, 6)), n)
        # 偏向选取利用率低的板：最差板 + 从较差的一半中随机
        order = sorted(range(n), key=lambda i: _sheet_used_area(cur[i]))
        pool = order[:max(k * 2, 6)]
        sel = sorted(rng.sample(pool, min(k, len(pool))))
        sel_set = set(sel)
        sub = [pl.part for i in sel for pl in cur[i]]
        sub_area = sum(p.area for p in sub)

        sort_name = rng.choice(("area", "short", "long", "jitter"))
        mode = rng.choice(("skyline", "col", "contact"))
        sort_key = _make_sort_key(sort_name, rng.randrange(1000))

        # 10% 概率尝试压缩板数（面积可行时）
        res = None
        if rng.random() < 0.1 and sub_area <= (k - 1) * sheet_area:
            res = _nest_single(sub, sheet_w, sheet_h, sort_key, mode,
                               cache, max_sheets=k - 1)
        if res is None:
            res = _nest_single(sub, sheet_w, sheet_h, sort_key, mode,
                               cache, max_sheets=k)
        if res is None:
            continue

        new = [s for i, s in enumerate(cur) if i not in sel_set] + res
        new_key = (len(new), -_concentration(new))
        if new_key < cur_key:
            cur, cur_key = new, new_key
            if new_key < best_key:
                best, best_key = new, new_key
        if progress and it % 20 == 0:
            progress(it, len(best))

    return best


# ---------------------------------------------------------------------------
# 结果组装与校验
# ---------------------------------------------------------------------------

def _materialize(sheets_pl: list, sheet_w: float, sheet_h: float,
                 thickness: float, unit: str, total_parts: int) -> NestingResult:
    """将内部放置记录转换为 NestingResult（Part 副本带最终几何）"""
    sheets = []
    for i, pls in enumerate(sheets_pl, start=1):
        sheet = Sheet(index=i, width=sheet_w, height=sheet_h,
                      thickness=thickness)
        for pl in pls:
            part = pl.part
            outer = part.outer_polygon
            origin = (outer.centroid.x, outer.centroid.y)
            # 与搜索一致的刚体变换：先绕质心旋转，再平移到目标包围盒
            if pl.rot:
                r_outer = affinity.rotate(outer, pl.rot, origin=origin)
            else:
                r_outer = outer
            ox = pl.x - r_outer.bounds[0]
            oy = pl.y - r_outer.bounds[1]

            def tf(g):
                g2 = affinity.rotate(g, pl.rot, origin=origin) if pl.rot else g
                return affinity.translate(g2, xoff=ox, yoff=oy)

            label_pt = tf(Point(part.label_position))
            placed = Part(
                id=part.id,
                number=part.number,
                polygon=tf(part.polygon),
                outer_polygon=affinity.translate(r_outer, xoff=ox, yoff=oy),
                holes=[tf(h) for h in part.holes],
                original_number=part.original_number,
                area=part.area,
                label_position=(label_pt.x, label_pt.y),
            )
            sheet.parts.append(placed)
        sheets.append(sheet)

    tpa = sum(pl.part.area for pls in sheets_pl for pl in pls)
    tsa = sum(s.total_area for s in sheets)
    return NestingResult(sheets=sheets, unit=unit, total_parts=total_parts,
                         total_sheets=len(sheets), total_part_area=tpa,
                         total_sheet_area=tsa)


def validate_nesting(result: NestingResult, sheet_w: float, sheet_h: float,
                     tol: float = 0.01) -> list:
    """精确校验排板结果：边界包含 + 两两面积重叠。返回违规信息列表（空 = 通过）"""
    errors = []
    for sheet in result.sheets:
        polys = [p.outer_polygon for p in sheet.parts]
        nums = [p.number for p in sheet.parts]
        for num, poly in zip(nums, polys):
            b = poly.bounds
            if b[0] < -tol or b[1] < -tol or \
               b[2] > sheet_w + tol or b[3] > sheet_h + tol:
                errors.append(f"Sheet {sheet.index}: {num} 超出大板边界")
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                if shapely.relate_pattern(polys[i], polys[j], _OVERLAP_PATTERN):
                    area = polys[i].intersection(polys[j]).area
                    errors.append(
                        f"Sheet {sheet.index}: {nums[i]} 与 {nums[j]} "
                        f"重叠 {area:.1f} mm²")
    return errors


def _compactness(result: NestingResult) -> float:
    """紧凑度：各板最高占用点之和，越小越好（用于同板数时择优）"""
    total = 0.0
    for sheet in result.sheets:
        top = max((p.outer_polygon.bounds[3] for p in sheet.parts),
                  default=0.0)
        total += top
    return total


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def nest_parts(parts: list, sheet_width: float, sheet_height: float,
               sheet_thickness: float, unit: str = "metric",
               configs: list | None = None,
               improve_budget: float = 40.0,
               progress=None) -> NestingResult:
    """排板主入口：多轮贪心取最优。

    configs：可选，自定义 [(排序键, 评分模式, 种子)] 列表；
    improve_budget：大邻域搜索的时间预算（秒），设 0 可关闭；
    progress：可选回调 fn(当前轮次, 总轮次, 当前最优板数)。
    """
    if configs is None:
        configs = _default_configs()

    cache: dict = {}
    best_pl = None
    best_key = None

    for i, (sort_name, mode, seed) in enumerate(configs, start=1):
        sort_key = _make_sort_key(sort_name, seed)
        sheets_pl = _nest_single(parts, sheet_width, sheet_height,
                                 sort_key, mode, cache)
        # 择优键：先比板数，再比紧凑度（各板最高占用点之和）
        compact = sum(max((pl.poly.bounds[3] for pl in pls), default=0.0)
                      for pls in sheets_pl)
        key = (len(sheets_pl), compact)
        if best_key is None or key < best_key:
            best_key = key
            best_pl = sheets_pl
        if progress:
            progress(i, len(configs), best_key[0] if best_key else None)

    # 大邻域搜索：持续提升填充集中度，争取整板释放
    if improve_budget > 0:
        best_pl = _lns_improve(best_pl, sheet_width, sheet_height, cache,
                               budget_s=improve_budget)

    result = _materialize(best_pl, sheet_width, sheet_height,
                          sheet_thickness, unit, len(parts))
    errors = validate_nesting(result, sheet_width, sheet_height)
    if errors:
        raise RuntimeError("排板结果校验失败：" + "; ".join(errors))
    return result
