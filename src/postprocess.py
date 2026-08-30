# -*- coding: utf-8 -*-
"""
后处理模块：对排板结果做几何变换。

操作：
    slide_to_edge   — 将面板推向大板边缘/角落，空出内部切割通道
    align_edges     — 对齐相邻面板边缘到同一直线
    enforce_min_gap — 强制面板间最小切割间距
"""

import math

import shapely
from shapely import affinity
from shapely.geometry import Point, Polygon

EPS = 1e-6
GAP_TOL = 1e-3
_OVERLAP_PATTERN = "T********"


def _part_straight_edges(part, tol: float = 0.05):
    """Return (vertical, horizontal) straight exterior edges for one part."""
    vertical = []
    horizontal = []
    coords = list(part.outer_polygon.exterior.coords)
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        if abs(x0 - x1) <= tol:
            vertical.append((min(y0, y1), max(y0, y1), x0, part))
        elif abs(y0 - y1) <= tol:
            horizontal.append((min(x0, x1), max(x0, x1), y0, part))
    return vertical, horizontal


def diagnose_postprocess(sheets, slide_expected: bool = True,
                         align_expected: bool = True, gap_mm: float = 0.0,
                         edge_tol: float = 1.0, snap_tol: float = 2.0):
    """Check whether bridge post-processing goals were fully applied.

    Returns a list of structured warnings.  Geometry boundary/overlap errors
    are intentionally not included here; the caller validates those separately
    so this function stays focused on manufacturing checks.
    """
    warnings = []
    for sheet in sheets:
        pp = PostProcessor(sheet.width, sheet.height)
        if slide_expected:
            missed_slide = []
            for part in sheet.parts:
                minx, miny, maxx, maxy = part.outer_polygon.bounds
                edge_distance = min(
                    minx,
                    sheet.width - maxx,
                    miny,
                    sheet.height - maxy,
                )
                if edge_distance <= edge_tol:
                    continue
                others = [
                    p.outer_polygon for p in sheet.parts if p is not part
                ]
                room = pp._slide_potential(
                    part.outer_polygon,
                    others,
                    gap_mm=gap_mm,
                    min_move=edge_tol,
                )
                if room is not None:
                    missed_slide.append({
                        "part": part.number,
                        "direction": room[0],
                        "available_mm": room[1],
                    })
            if missed_slide:
                warnings.append({
                    "type": "part_not_on_edge",
                    "sheet": sheet.index,
                    "parts": [item["part"] for item in missed_slide],
                    "slide_potential": missed_slide,
                })

        if align_expected:
            misaligned = []
            constrained = []
            for orientation in ("vertical", "horizontal"):
                edges = []
                for part in sheet.parts:
                    vertical, horizontal = _part_straight_edges(part)
                    edges.extend(
                        vertical if orientation == "vertical" else horizontal
                    )
                if len(edges) < 2:
                    continue
                edges.sort(key=lambda edge: edge[2])
                for left, right in zip(edges, edges[1:]):
                    if right[2] - left[2] >= snap_tol:
                        continue
                    if right[2] - left[2] <= EPS:
                        continue
                    if id(left[3]) == id(right[3]):
                        continue
                    low = max(left[0], right[0])
                    high = min(left[1], right[1])
                    if high <= low:
                        continue
                    gap = right[2] - left[2]
                    item = {
                        "orientation": orientation,
                        "gap_mm": round(gap, 3),
                        "parts": [left[3].number, right[3].number],
                    }
                    feasible = pp._through_cut_feasible(
                        sheet.parts,
                        orientation,
                        left[3],
                        right[3],
                        gap,
                        gap_mm,
                    )
                    if feasible:
                        misaligned.append(item)
                    else:
                        item["reason"] = "边界或零件间距约束下无法完成共线"
                        constrained.append(item)
            if misaligned:
                warnings.append({
                    "type": "through_cut_not_aligned",
                    "sheet": sheet.index,
                    "edges": misaligned,
                })
            if constrained:
                warnings.append({
                    "type": "through_cut_constrained",
                    "sheet": sheet.index,
                    "edges": constrained,
                })

        if gap_mm > 0:
            gap_pairs = []
            for i, part_a in enumerate(sheet.parts):
                for part_b in sheet.parts[i + 1:]:
                    separation = part_a.outer_polygon.distance(
                        part_b.outer_polygon
                    )
                    overlaps = shapely.relate_pattern(
                        part_a.outer_polygon,
                        part_b.outer_polygon,
                        _OVERLAP_PATTERN,
                    )
                    if not overlaps and separation < gap_mm - GAP_TOL:
                        gap_pairs.append({
                            "parts": [part_a.number, part_b.number],
                            "gap_mm": round(separation, 3),
                        })
            if gap_pairs:
                warnings.append({
                    "type": "min_gap_violation",
                    "sheet": sheet.index,
                    "pairs": gap_pairs,
                })

    return warnings


def diagnose_waterjet(sheets, first_part_left_edge: bool = False,
                      arbitrary_rotation: bool = False,
                      rotations: tuple = (0,),
                      edge_tol: float = 1.0, snap_tol: float = 1.0):
    """DeepNest/waterjet-specific manufacturability checks."""
    warnings = []
    metrics = {
        "first_part_left_edge_checked": 0,
        "first_part_left_edge_failed": 0,
        "collinear_edge_pairs": 0,
    }

    for sheet in sheets:
        if first_part_left_edge and sheet.parts:
            metrics["first_part_left_edge_checked"] += 1
            part = sheet.parts[0]
            longest = _longest_straight_edge(part)
            can_rotate_to_vertical = arbitrary_rotation or any(
                (angle % 180) == 90 for angle in rotations
            )
            has_vertical_longest = _has_vertical_longest_edge(
                part, longest, edge_tol
            )
            if (
                longest > 0
                and (can_rotate_to_vertical or has_vertical_longest)
                and not _has_left_vertical_edge(part, longest, edge_tol)
            ):
                metrics["first_part_left_edge_failed"] += 1
                warnings.append({
                    "type": "first_part_not_on_left_edge",
                    "sheet": sheet.index,
                    "part": part.number,
                    "longest_straight_edge_mm": round(longest, 1),
                })
        metrics["collinear_edge_pairs"] += _collinear_edge_pairs(
            sheet.parts, snap_tol
        )

    return warnings, metrics


def _longest_straight_edge(part, tol: float = 0.05) -> float:
    coords = list(part.outer_polygon.exterior.coords)
    longest = 0.0
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        if abs(x0 - x1) <= tol or abs(y0 - y1) <= tol:
            longest = max(longest, math.hypot(x1 - x0, y1 - y0))
    return longest


def _has_left_vertical_edge(part, longest, edge_tol) -> bool:
    coords = list(part.outer_polygon.exterior.coords)
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        if abs(x0 - x1) > 0.05:
            continue
        length = abs(y1 - y0)
        if x0 <= edge_tol and length >= longest - edge_tol:
            return True
    return False


def _has_vertical_longest_edge(part, longest, edge_tol) -> bool:
    coords = list(part.outer_polygon.exterior.coords)
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        if abs(x0 - x1) > 0.05:
            continue
        if abs(y1 - y0) >= longest - edge_tol:
            return True
    return False


def _collinear_edge_pairs(parts, snap_tol) -> int:
    total = 0
    for orientation in ("vertical", "horizontal"):
        edges = []
        for part in parts:
            vertical, horizontal = _part_straight_edges(part)
            edges.extend(vertical if orientation == "vertical" else horizontal)
        if len(edges) < 2:
            continue
        edges.sort(key=lambda edge: edge[2])
        for left, right in zip(edges, edges[1:]):
            if right[2] - left[2] > snap_tol:
                continue
            if id(left[3]) == id(right[3]):
                continue
            if min(left[1], right[1]) > max(left[0], right[0]):
                total += 1
    return total


class PostProcessor:
    """排板结果后处理器。"""

    def __init__(self, sheet_width: float, sheet_height: float):
        self.w = sheet_width
        self.h = sheet_height

    def run(self, sheets, slide=True, align=True, gap_mm=0.0):
        if slide:
            self.maximize_edge_clearance(sheets, gap_mm=gap_mm)
        elif gap_mm > 0:
            self.enforce_min_gap(sheets, gap_mm)
        if align:
            self.align_edges(sheets, gap_mm=gap_mm)
            self.through_cut(sheets, gap_mm=gap_mm)
        if slide:
            for sheet in sheets:
                self._slide_sheet(sheet, gap_mm)
        if gap_mm > 0:
            self.enforce_min_gap(sheets, gap_mm)

    # ── slide_to_edge ────────────────────────────────────────

    def slide_to_edge(self, sheets):
        """兼容入口：将面板推向最近板边。"""
        self.maximize_edge_clearance(sheets, gap_mm=0.0)

    def maximize_edge_clearance(self, sheets, gap_mm=0.0):
        """在不改变角度和大板归属的前提下，将每张大板的面板重排到四边。

        目标是最小化面板到板边的最小距离，从而尽可能空出中央区域。
        若某张大板无法在不重叠的情况下完成重排，则保留该板原始排板结果。
        """
        for sheet in sheets:
            if not self._pack_sheet_to_edges(sheet, gap_mm):
                self._slide_sheet(sheet, gap_mm)
            else:
                self._slide_sheet(sheet, gap_mm)

    def _pack_sheet_to_edges(self, sheet, gap_mm):
        parts = sheet.parts
        if not parts:
            return True

        if len(parts) == 1:
            p = parts[0]
            b = p.outer_polygon.bounds
            w = b[2] - b[0]
            h = b[3] - b[1]
            tx, ty = self._target_corner(b[0], b[1], w, h)
            self._shift(p, tx - b[0], ty - b[1])
            return True

        refs = []
        for p in parts:
            refs.append((p, p.outer_polygon, p.polygon, list(p.holes),
                         p.label_position))

        order = sorted(refs, key=lambda item: item[1].area, reverse=True)
        placed = []
        assignment = {}

        for ref in order:
            p, ref_outer, _, _, _ = ref
            minx, miny, maxx, maxy = ref_outer.bounds
            w = maxx - minx
            h = maxy - miny
            candidates = self._edge_candidates(minx, miny, w, h, placed)
            target_corner = self._target_corner(minx, miny, w, h)
            best = None
            best_score = None

            for cx, cy in candidates:
                target = affinity.translate(ref_outer, cx - minx, cy - miny)
                tb = target.bounds
                if tb[0] < -EPS or tb[1] < -EPS or \
                   tb[2] > self.w + EPS or tb[3] > self.h + EPS:
                    continue
                if any(self._too_close(target, o, gap_mm) for o in placed):
                    continue

                edge_dist = min(tb[0], self.w - tb[2], tb[1], self.h - tb[3])
                corner_penalty = (abs(tb[0] - target_corner[0]) +
                                  abs(tb[1] - target_corner[1]))
                score = (edge_dist, corner_penalty)
                if best_score is None or score < best_score:
                    best_score = score
                    best = (cx, cy)

            if best is None:
                return False

            assignment[id(p)] = best
            placed.append(affinity.translate(ref_outer,
                                             best[0] - minx,
                                             best[1] - miny))

        for ref in refs:
            p, ref_outer, ref_poly, ref_holes, ref_label = ref
            minx, miny, _, _ = ref_outer.bounds
            cx, cy = assignment[id(p)]
            dx = cx - minx
            dy = cy - miny
            p.outer_polygon = affinity.translate(ref_outer, dx, dy)
            p.polygon = affinity.translate(ref_poly, dx, dy)
            p.holes = [affinity.translate(h, dx, dy) for h in ref_holes]
            p.label_position = (ref_label[0] + dx, ref_label[1] + dy)
        return True

    def _target_corner(self, minx, miny, w, h):
        cx = minx + w / 2.0
        cy = miny + h / 2.0
        tx = 0.0 if cx < self.w / 2.0 else self.w - w
        ty = 0.0 if cy < self.h / 2.0 else self.h - h
        return tx, ty

    def _edge_candidates(self, minx, miny, w, h, placed):
        xs = {0.0, self.w - w}
        ys = {0.0, self.h - h}
        for o in placed:
            x0, y0, x1, y1 = o.bounds
            xs.add(x1)
            xs.add(x0 - w)
            ys.add(y1)
            ys.add(y0 - h)

        # 增加 50mm 间隔的周缘候选点，提高 L 型/异形板的贴边成功率。
        step = 50.0
        xmax = self.w - w
        ymax = self.h - h
        if xmax > 0:
            for k in range(int(xmax // step) + 2):
                x = k * step
                if x <= xmax + EPS:
                    xs.add(x)
        if ymax > 0:
            for k in range(int(ymax // step) + 2):
                y = k * step
                if y <= ymax + EPS:
                    ys.add(y)

        pts = set()
        pts.add((0.0, 0.0))
        pts.add((self.w - w, 0.0))
        pts.add((0.0, self.h - h))
        pts.add((self.w - w, self.h - h))

        for x in xs:
            if -EPS <= x <= self.w - w + EPS:
                pts.add((x, 0.0))
                pts.add((x, self.h - h))
        for y in ys:
            if -EPS <= y <= self.h - h + EPS:
                pts.add((0.0, y))
                pts.add((self.w - w, y))

        for x in xs:
            if -EPS <= x <= self.w - w + EPS:
                for y in ys:
                    if -EPS <= y <= self.h - h + EPS:
                        pts.add((x, y))
        return sorted(pts, key=lambda p: (p[0], p[1]))

    def _too_close(self, a, b, gap_mm):
        if gap_mm <= 0:
            return shapely.relate_pattern(a, b, _OVERLAP_PATTERN)
        return a.distance(b) < gap_mm - GAP_TOL

    def _slide_sheet(self, sheet, gap_mm=0.0):
        # 标记已滑过的面板（靠边后不再滑第二次）
        done = set()
        for _pass in range(20):
            moved = False
            for part in sorted(sheet.parts, key=lambda p: p.outer_polygon.area):
                if id(part) in done:
                    continue
                b = part.outer_polygon.bounds
                dl, db = b[0], b[1]
                dr, dt = self.w - b[2], self.h - b[3]
                # 如果已靠至少一边，标记完成
                if dl < 1 or dr < 1 or db < 1 or dt < 1:
                    done.add(id(part))
                    continue
                if self._slide_one(part, sheet, gap_mm):
                    moved = True
                    # 滑完后检查是否靠边
                    b2 = part.outer_polygon.bounds
                    if b2[0] < 1 or self.w-b2[2] < 1 or b2[1] < 1 or self.h-b2[3] < 1:
                        done.add(id(part))
            if not moved:
                break


    def _slide_one(self, part, sheet, gap_mm=0.0):
        """优先滑向最近边；成功靠边后，下一轮可滑向次近边。"""
        others = [p.outer_polygon for p in sheet.parts if p is not part]
        b = part.outer_polygon.bounds
        dl, db = b[0], b[1]
        dr, dt = self.w - b[2], self.h - b[3]

        # 靠边状态
        tL = dl < 1; tR = dr < 1; tB = db < 1; tT = dt < 1

        # 单边候选：最近边优先
        raw = [(dl, "L", -dl, 0), (dr, "R", dr, 0),
               (db, "B", 0, -db), (dt, "T", 0, dt)]

        # 只保留还没靠的边
        cand = []
        if not tL: cand.append((-dl, 0, dl, "L"))
        if not tR: cand.append((dr, 0, dr, "R"))
        if not tB: cand.append((0, -db, db, "B"))
        if not tT: cand.append((0, dt, dt, "T"))

        # 已靠 2 边以上 → 停
        if not cand:
            return False

        # 按距离排序
        cand.sort(key=lambda x: x[2])

        for dx, dy, _, edge_name in cand:
            if abs(dx) < 0.5 and abs(dy) < 0.5:
                continue
            d = self._max_slide(part.outer_polygon, dx, dy, others, gap_mm)
            if d > 1.0:
                mag = (dx**2 + dy**2)**0.5
                self._shift(part, dx / mag * d, dy / mag * d)
                return True
        return False

    def _fill_empty_edge(self, sheet):
        """找到空边，整体平移填满它。"""
        touch = {"left": 0, "right": 0, "bottom": 0, "top": 0}
        for p in sheet.parts:
            b = p.outer_polygon.bounds
            if b[0] < 1: touch["left"] += 1
            if self.w - b[2] < 1: touch["right"] += 1
            if b[1] < 1: touch["bottom"] += 1
            if self.h - b[3] < 1: touch["top"] += 1

        min_e = min(touch, key=touch.get)
        if touch[min_e] > 0:
            return False

        if min_e == "left":
            d = min(p.outer_polygon.bounds[0] for p in sheet.parts)
            ux, uy = -1.0, 0.0
        elif min_e == "right":
            d = min(self.w - p.outer_polygon.bounds[2] for p in sheet.parts)
            ux, uy = 1.0, 0.0
        elif min_e == "bottom":
            d = min(p.outer_polygon.bounds[1] for p in sheet.parts)
            ux, uy = 0.0, -1.0
        else:
            d = min(self.h - p.outer_polygon.bounds[3] for p in sheet.parts)
            ux, uy = 0.0, 1.0

        if d < 1.0:
            return False

        # 二分搜索整体平移距离
        lo, hi = 0.0, d * 2
        for _ in range(40):
            mid = (lo + hi) / 2
            ok = True
            tps = [affinity.translate(p.outer_polygon, ux*mid, uy*mid) for p in sheet.parts]
            for i, tp in enumerate(tps):
                tb = tp.bounds
                if tb[0] < -EPS or tb[1] < -EPS or tb[2] > self.w+EPS or tb[3] > self.h+EPS:
                    ok = False; break
                for j in range(i+1, len(tps)):
                    if shapely.relate_pattern(tp, tps[j], _OVERLAP_PATTERN):
                        ok = False; break
                if not ok: break
            if ok:
                lo = mid
            else:
                hi = mid

        if lo > 1.0:
            for p in sheet.parts:
                self._shift(p, ux*lo, uy*lo)
            return True
        return False

    def _max_slide(self, orig_poly, dir_x, dir_y, others, gap_mm=0.0):
        """二分搜索沿方向的最大安全滑动距离。"""
        if abs(dir_x) < EPS and abs(dir_y) < EPS:
            return 0.0
        mag = (dir_x**2 + dir_y**2)**0.5
        ux, uy = dir_x / mag, dir_y / mag

        hi = 1.0
        for _ in range(30):
            test = affinity.translate(orig_poly, ux*hi, uy*hi)
            tb = test.bounds
            if tb[0] < -EPS or tb[1] < -EPS or tb[2] > self.w+EPS or tb[3] > self.h+EPS:
                break
            if any(self._too_close(test, o, gap_mm) for o in others):
                break
            hi *= 2
            if hi > 100000:
                break

        lo = 0.0
        for _ in range(50):
            mid = (lo + hi) / 2
            test = affinity.translate(orig_poly, ux*mid, uy*mid)
            tb = test.bounds
            if tb[0] < -EPS or tb[1] < -EPS or tb[2] > self.w+EPS or tb[3] > self.h+EPS:
                hi = mid; continue
            if any(self._too_close(test, o, gap_mm) for o in others):
                hi = mid; continue
            lo = mid
        return lo

    def _slide_potential(self, orig_poly, others, gap_mm=0.0, min_move=1.0):
        """Return the first direction in which the part can reach an edge."""
        minx, miny, maxx, maxy = orig_poly.bounds
        directions = (
            (-1.0, 0.0, "left", minx),
            (1.0, 0.0, "right", self.w - maxx),
            (0.0, -1.0, "bottom", miny),
            (0.0, 1.0, "top", self.h - maxy),
        )
        for dir_x, dir_y, name, required in directions:
            if required <= min_move:
                continue
            distance = self._max_slide(orig_poly, dir_x, dir_y, others, gap_mm)
            if distance >= required - min_move:
                return name, round(distance, 1)
        return None

    def _shift(self, part, dx, dy):
        part.outer_polygon = affinity.translate(part.outer_polygon, dx, dy)
        part.polygon = affinity.translate(part.polygon, dx, dy)
        part.holes = [affinity.translate(h, dx, dy) for h in part.holes]
        lp = part.label_position
        part.label_position = (lp[0]+dx, lp[1]+dy)

    # ── align_edges ──────────────────────────────────────────

    def align_edges(self, sheets, snap_tol=2.0, gap_mm=0.0):
        for sheet in sheets:
            if len(sheet.parts) <= 1: continue
            self._align_sheet(sheet, snap_tol, gap_mm)

    def _align_sheet(self, sheet, snap_tol, gap_mm=0.0):
        for _ in range(5):
            changed = False
            for edge_type in ("bottom", "top", "left", "right"):
                if self._snap_axis(sheet, edge_type, snap_tol, gap_mm):
                    changed = True
            if not changed:
                break

    def _snap_axis(self, sheet, edge_type, snap_tol, gap_mm=0.0):
        parts = sheet.parts
        edges = []
        for p in parts:
            b = p.outer_polygon.bounds
            v = b[1] if edge_type == "bottom" else (b[3] if edge_type == "top" else (b[0] if edge_type == "left" else b[2]))
            edges.append((p, v))
        edges.sort(key=lambda e: e[1])

        clusters = []; current = [edges[0]]
        for e in edges[1:]:
            if e[1] - current[-1][1] < snap_tol:
                current.append(e)
            else:
                if len(current) >= 2: clusters.append(current)
                current = [e]
        if len(current) >= 2: clusters.append(current)

        is_vert = edge_type in ("left", "right")
        part_deltas: dict[int, float] = {}
        for cluster in clusters:
            target = sum(e[1] for e in cluster) / len(cluster)
            for part, _ in cluster:
                b = part.outer_polygon.bounds
                cur = b[1] if edge_type == "bottom" else (b[3] if edge_type == "top" else (b[0] if edge_type == "left" else b[2]))
                part_deltas[id(part)] = target - cur

        moving_parts = [
            (part, delta)
            for part in parts
            if (delta := part_deltas.get(id(part))) is not None
        ]
        others = [
            part.outer_polygon
            for part in parts
            if id(part) not in part_deltas
        ]
        moved_polys = []
        moves = []
        for part, delta in moving_parts:
            if abs(delta) < EPS:
                moved_polys.append(part.outer_polygon)
                moves.append((part, 0.0, 0.0))
                continue
            dx, dy = (delta, 0.0) if is_vert else (0.0, delta)
            test = affinity.translate(part.outer_polygon, dx, dy)
            tb = test.bounds
            if tb[0] < -EPS or tb[1] < -EPS or tb[2] > self.w+EPS or tb[3] > self.h+EPS:
                return False
            if any(self._too_close(test, other, gap_mm) for other in others + moved_polys):
                return False
            moved_polys.append(test)
            moves.append((part, dx, dy))

        changed = False
        for part, dx, dy in moves:
            if dx or dy:
                self._shift(part, dx, dy)
                changed = True
        return changed

    # ── through_cut ─────────────────────────────────────────

    def through_cut(self, sheets, snap_tol=2.0, gap_mm=0.0):
        """对已排板结果做“一刀通切”整理。

        只做刚体平移，不改变角度和板归属；优先把同一张板内近乎共线的
        竖直/水平外轮廓边吸附到同一条切割线，减少桥切机抬刀和小台阶。
        """
        for sheet in sheets:
            if len(sheet.parts) <= 1:
                continue
            snapshot = self._snapshot_parts(sheet.parts)
            self._through_cut_sheet(sheet, snap_tol, gap_mm)
            if self._has_overlap(sheet.parts) or (
                gap_mm > 0 and self._has_gap_violation(sheet.parts, gap_mm)
            ):
                self._restore_parts(sheet.parts, snapshot)

    def _snapshot_parts(self, parts):
        return [
            (p.outer_polygon, p.polygon, list(p.holes), p.label_position)
            for p in parts
        ]

    def _restore_parts(self, parts, snapshot):
        for p, (outer, poly, holes, label) in zip(parts, snapshot):
            p.outer_polygon = outer
            p.polygon = poly
            p.holes = holes
            p.label_position = label

    def _has_overlap(self, parts):
        for i, p1 in enumerate(parts):
            for p2 in parts[i + 1:]:
                if shapely.relate_pattern(
                    p1.outer_polygon,
                    p2.outer_polygon,
                    _OVERLAP_PATTERN,
                ):
                    return True
        return False

    def _has_gap_violation(self, parts, gap_mm):
        for i, p1 in enumerate(parts):
            for p2 in parts[i + 1:]:
                if not shapely.relate_pattern(
                    p1.outer_polygon,
                    p2.outer_polygon,
                    _OVERLAP_PATTERN,
                ) and p1.outer_polygon.distance(p2.outer_polygon) < gap_mm - GAP_TOL:
                    return True
        return False

    def _through_cut_sheet(self, sheet, snap_tol, gap_mm=0.0):
        for _ in range(20):
            changed = False
            for orientation in ("vertical", "horizontal"):
                axis_changed = self._snap_straight_axis(
                    sheet, orientation, snap_tol, gap_mm
                )
                pair_changed = self._snap_straight_pairs(
                    sheet, orientation, snap_tol, gap_mm
                )
                if axis_changed or pair_changed:
                    changed = True
            if not changed:
                break

    def _snap_straight_axis(self, sheet, orientation, snap_tol, gap_mm=0.0):
        parts = sheet.parts
        edges = []
        for part in parts:
            vertical, horizontal = self._straight_edges(part)
            edges.extend(vertical if orientation == "vertical" else horizontal)
        if len(edges) < 2:
            return False

        edges.sort(key=lambda edge: edge[2])
        clusters = []
        current = [edges[0]]
        for edge in edges[1:]:
            if edge[2] - current[-1][2] < snap_tol:
                current.append(edge)
            else:
                if len(current) >= 2:
                    clusters.append(current)
                current = [edge]
        if len(current) >= 2:
            clusters.append(current)

        if not clusters:
            return False

        cluster = clusters[0]
        is_vertical = orientation == "vertical"
        target = sum(edge[2] for edge in cluster) / len(cluster)
        part_deltas: dict[int, list[float]] = {}
        for _, _, coord, part in cluster:
            part_deltas.setdefault(id(part), []).append(target - coord)

        moving_parts = [
            (part, sum(deltas) / len(deltas))
            for part in parts
            if (deltas := part_deltas.get(id(part)))
        ]
        moves = []
        for part, delta in moving_parts:
            dx, dy = (delta, 0.0) if is_vertical else (0.0, delta)
            moves.append((part, dx, dy))

        # 优先只移动一侧，避免把已贴边零件拉离板边。
        if len(cluster) == 2:
            (_, _, coord_a, part_a), (_, _, coord_b, part_b) = cluster
            if part_a is not part_b:
                for part, delta in (
                    (part_a, coord_b - coord_a),
                    (part_b, coord_a - coord_b),
                ):
                    if abs(delta) < EPS:
                        continue
                    dx, dy = (delta, 0.0) if is_vertical else (0.0, delta)
                    if self._try_axis_moves(parts, [(part, dx, dy)], gap_mm):
                        return True

        return self._try_axis_moves(parts, moves, gap_mm)

    def _axis_moves_valid(self, parts, moves, gap_mm):
        """Test a set of axis translations without mutating parts."""
        candidate = {id(part): part.outer_polygon for part in parts}
        for part, dx, dy in moves:
            test = affinity.translate(part.outer_polygon, dx, dy)
            tb = test.bounds
            if tb[0] < -EPS or tb[1] < -EPS or \
               tb[2] > self.w + EPS or tb[3] > self.h + EPS:
                return False
            candidate[id(part)] = test

        for i, part_a in enumerate(parts):
            for part_b in parts[i + 1:]:
                if self._too_close(candidate[id(part_a)], candidate[id(part_b)], gap_mm):
                    return False
        return True

    def _try_axis_moves(self, parts, moves, gap_mm):
        """Check and apply a set of axis translations as one atomic move."""
        if not self._axis_moves_valid(parts, moves, gap_mm):
            return False
        for part, dx, dy in moves:
            if dx or dy:
                self._shift(part, dx, dy)
        return True

    def _through_cut_feasible(self, parts, orientation, part_a, part_b,
                              delta, gap_mm):
        """Return True if two near-collinear edges can be aligned safely."""
        return any(
            self._axis_moves_valid(parts, moves, gap_mm)
            for moves in self._through_cut_moves(part_a, part_b, orientation, delta)
        )

    def _through_cut_moves(self, part_a, part_b, orientation, delta):
        """Return candidate move sets for aligning two straight edges."""
        is_vertical = orientation == "vertical"
        half = delta / 2.0
        move_a = (half, 0.0) if is_vertical else (0.0, half)
        move_b = (-half, 0.0) if is_vertical else (0.0, -half)
        return (
            [(part_a, delta if is_vertical else 0.0,
              0.0 if is_vertical else delta)],
            [(part_b, -delta if is_vertical else 0.0,
              0.0 if is_vertical else -delta)],
            [(part_a, *move_a), (part_b, *move_b)],
        )

    def _snap_straight_pairs(self, sheet, orientation, snap_tol, gap_mm):
        """Pairwise fallback for through-cut alignment."""
        parts = sheet.parts
        changed = False
        for _ in range(6):
            edges = []
            for part in parts:
                vertical, horizontal = self._straight_edges(part)
                edges.extend(vertical if orientation == "vertical" else horizontal)
            if len(edges) < 2:
                break
            edges.sort(key=lambda edge: edge[2])
            pairs = []
            for left, right in zip(edges, edges[1:]):
                if right[2] - left[2] >= snap_tol:
                    continue
                if right[2] - left[2] <= EPS:
                    continue
                if id(left[3]) == id(right[3]):
                    continue
                if min(left[1], right[1]) <= max(left[0], right[0]):
                    continue
                pairs.append((left, right))
            if not pairs:
                break

            moved = False
            for left, right in pairs:
                delta = right[2] - left[2]
                for moves in self._through_cut_moves(
                    left[3], right[3], orientation, delta
                ):
                    if self._try_axis_moves(parts, moves, gap_mm):
                        moved = True
                        break
                if moved:
                    break
            if not moved:
                break
            changed = True
        return changed

    def _straight_edges(self, part, tol=0.05):
        """提取外轮廓中近似竖直和水平的直线边。"""
        vertical = []
        horizontal = []
        coords = list(part.outer_polygon.exterior.coords)
        for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
            if abs(x0 - x1) <= tol:
                vertical.append((min(y0, y1), max(y0, y1), x0, part))
            elif abs(y0 - y1) <= tol:
                horizontal.append((min(x0, x1), max(x0, x1), y0, part))
        return vertical, horizontal

    # ── manufacturability metrics ───────────────────────────

    def measure(self, sheets):
        """返回靠边总长和通切对齐总长，单位与 DXF 一致。"""
        edge_total = 0.0
        through_total = 0.0
        for sheet in sheets:
            edge_total += self._edge_contact_score(sheet)
            through_total += self._through_cut_score(sheet)
        return {
            "edge_contact_mm": edge_total,
            "through_cut_mm": through_total,
        }

    def _edge_contact_score(self, sheet, tol=1.0):
        total = 0.0
        for part in sheet.parts:
            coords = list(part.outer_polygon.exterior.coords)
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                if abs(y0 - y1) <= tol:
                    length = abs(x1 - x0)
                    y = (y0 + y1) / 2.0
                    if y <= tol or abs(y - self.h) <= tol:
                        total += length
                elif abs(x0 - x1) <= tol:
                    length = abs(y1 - y0)
                    x = (x0 + x1) / 2.0
                    if x <= tol or abs(x - self.w) <= tol:
                        total += length
        return total

    def _through_cut_score(self, sheet, snap_tol=2.0):
        total = 0.0
        for orientation in ("vertical", "horizontal"):
            edges = []
            for part in sheet.parts:
                vertical, horizontal = self._straight_edges(part)
                edges.extend(vertical if orientation == "vertical" else horizontal)
            if len(edges) < 2:
                continue
            edges.sort(key=lambda edge: edge[2])
            clusters = []
            current = [edges[0]]
            for edge in edges[1:]:
                if edge[2] - current[-1][2] < snap_tol:
                    current.append(edge)
                else:
                    if len(current) >= 2:
                        clusters.append(current)
                    current = [edge]
            if len(current) >= 2:
                clusters.append(current)
            for cluster in clusters:
                owners = {id(edge[3]) for edge in cluster}
                if len(owners) < 2:
                    continue
                total += sum(edge[1] - edge[0] for edge in cluster)
        return total

    # ── enforce_min_gap ─────────────────────────────────────

    def enforce_min_gap(self, sheets, gap_mm):
        for sheet in sheets:
            if len(sheet.parts) <= 1: continue
            self._gap_sheet(sheet, gap_mm)

    def _gap_sheet(self, sheet, gap_mm):
        for _ in range(10):
            resolved = True
            for i, p1 in enumerate(sheet.parts):
                for j in range(i+1, len(sheet.parts)):
                    p2 = sheet.parts[j]
                    sep = p1.outer_polygon.distance(p2.outer_polygon)
                    if sep >= gap_mm or shapely.relate_pattern(p1.outer_polygon, p2.outer_polygon, _OVERLAP_PATTERN):
                        continue
                    needed = gap_mm - sep + 0.1
                    ca, cb = p1.outer_polygon.centroid, p2.outer_polygon.centroid
                    vx, vy = ca.x-cb.x, ca.y-cb.y
                    mag = (vx**2+vy**2)**0.5
                    if mag < EPS: continue
                    dx, dy = vx/mag*needed, vy/mag*needed
                    others = [p.outer_polygon for p in sheet.parts if p not in (p1,p2)]
                    if self._push(p1, dx, dy, others+[p2.outer_polygon]):
                        resolved = False
                    elif self._push(p2, -dx, -dy, others+[p1.outer_polygon]):
                        resolved = False
                    elif self._push(p1, dx/2, dy/2, others+[p2.outer_polygon]):
                        p1p = p1.outer_polygon
                        others2 = [p.outer_polygon for p in sheet.parts if p not in (p1,p2)]
                        self._push(p2, -dx/2, -dy/2, others2+[p1p])
                        resolved = False
            if resolved:
                break

    def _push(self, part, dx, dy, others):
        if abs(dx) < EPS and abs(dy) < EPS: return False
        test = affinity.translate(part.outer_polygon, dx, dy)
        tb = test.bounds
        if tb[0] < -EPS or tb[1] < -EPS or tb[2] > self.w+EPS or tb[3] > self.h+EPS: return False
        if any(shapely.relate_pattern(test, o, _OVERLAP_PATTERN) for o in others): return False
        self._shift(part, dx, dy)
        return True
