"""逐板最佳填充束搜索（桥切引擎的第二条布局路径）。

与 `_nest_single` 的全局排序贪心不同，这里每开一张新板都问：
"当前库存在这张板上能摆出的最满布局是什么？"——用束搜索在
（空闲矩形 × 零件尺寸档 × 取向）的空间里逐件扩展，保留前 beam_width 个
部分布局，直到放不下更多零件。

为什么更强（CGR45 实测，3200x1800）：
- 多轮贪心 10 配置最优 52 张（填充上限 ~79%，且填充分布均匀无差板，
  回看/LNS 的小邻域重排找不到可减板结构）；
- 逐板束搜索 ~50 张（单板填充 81-95%）。差距不在放置准则，而在
  "哪些零件共享一张板"的分组：束搜索能发现跨尺寸档的互锁配方
  （如 1892x978 横放 + 978x1791 竖放 + 1819x648 横放拼满一张板）。

几何口径：束搜索内部按包围盒矩形摆放（L 形件保守取 bbox），
并通过空闲矩形减法保证互不重叠与 min_gap 间距——因此输出天然合法，
多边形精确校验在引擎主流程里照常进行。孔洞跟随零件刚体变换。
"""
from __future__ import annotations

import time
from collections import deque
import math

import numpy as np
import shapely

from src.nesting import _Placement, _rotation_forms, _translate_coords

EPS = 1e-6
# 尺寸档聚合精度（mm）：同档零件视为可互换
_TIER_DIGITS = 1
# 单张板最多摆放件数（束搜索深度上限）
_MAX_PIECES_PER_SHEET = 12
# 缺口空区下限（mm²）：太小的缺口不值得作为空闲矩形回收
_NOTCH_MIN_AREA = 1e4


def _notch_rects_local(rp, rb) -> list:
    """L 形件的缺口空区（bbox 内、多边形外），以 bbox 左下角为原点。

    只回收"本身就是矩形"的空区（角部缺口），非矩形空区保守跳过。
    返回 [(dx, dy, w, h)]，坐标为相对 bbox 左下角的偏移。
    """
    bw, bh = rb[2] - rb[0], rb[3] - rb[1]
    bbox_area = bw * bh
    if rp.area >= bbox_area * 0.995:
        return []
    diff = shapely.box(rb[0], rb[1], rb[2], rb[3]).difference(rp)
    if diff.is_empty:
        return []
    geoms = [diff] if diff.geom_type == "Polygon" else list(diff.geoms)
    rects = []
    for g in geoms:
        if g.is_empty or g.area < _NOTCH_MIN_AREA:
            continue
        gb = g.bounds
        if g.area >= 0.99 * (gb[2] - gb[0]) * (gb[3] - gb[1]):
            rects.append((gb[0] - rb[0], gb[1] - rb[1],
                          gb[2] - gb[0], gb[3] - gb[1]))
    return rects


def _part_tier_key(part, cache: dict, rotations: tuple):
    """零件的尺寸档键：(短边, 长边, 形状签名)。

    形状签名取自首个旋转形态归一化顶点：bbox 相同但缺口方向不同
    （镜像 L 形）的零件必须分档，否则按代表件算出的缺口空区
    套到镜像件上会造成重叠。
    """
    forms = _rotation_forms(part, cache, rotations)
    rb = forms[0][2]
    w, h = rb[2] - rb[0], rb[3] - rb[1]
    coords = np.asarray(forms[0][3])
    normalized = coords - coords.min(axis=0)
    sig = hash(tuple(map(tuple, np.round(normalized, _TIER_DIGITS))))
    return (round(min(w, h), _TIER_DIGITS), round(max(w, h), _TIER_DIGITS),
            sig)


def _build_tiers(parts: list, cache: dict, rotations: tuple) -> list:
    """把零件聚成尺寸档：[(key, [(rot, w, h, rb, coords)], deque(part_indices))]。

    取向变体来自 `_rotation_forms`（已对中心对称形状去重）；
    每个变体用 (w, h) 参与空闲矩形匹配，摆放时取对应 rot 的几何。
    """
    tier_map: dict = {}
    for idx, part in enumerate(parts):
        forms = _rotation_forms(part, cache, rotations)
        if not forms:
            continue
        key = _part_tier_key(part, cache, rotations)
        entry = tier_map.get(key)
        if entry is None:
            variants = []
            for rot, rp, rb, coords in forms:
                variants.append((rot, rb[2] - rb[0], rb[3] - rb[1], rb,
                                 coords, _notch_rects_local(rp, rb)))
            entry = [key, variants, deque(), False]
            tier_map[key] = entry
        entry[2].append(idx)
    tiers = sorted(tier_map.values(),
                   key=lambda e: -(e[0][0] * e[0][1]))
    return tiers


def _subtract_free(free: list, x: float, y: float, w: float, h: float,
                   sheet_w: float, sheet_h: float) -> list:
    """从空闲矩形列表挖去占用区 (x,y,w,h)，返回互不重叠的新分区。

    每个与占用区相交的空闲矩形切出左/右/下/上四块；再滤掉被包含的碎片。
    """
    out = []
    for (fx, fy, fw, fh) in free:
        if x >= fx + fw - EPS or x + w <= fx + EPS \
                or y >= fy + fh - EPS or y + h <= fy + EPS:
            out.append((fx, fy, fw, fh))
            continue
        if x > fx + EPS:
            out.append((fx, fy, x - fx, fh))
        if x + w < fx + fw - EPS:
            out.append((x + w, fy, fx + fw - (x + w), fh))
        if y > fy + EPS:
            out.append((fx, fy, fw, y - fy))
        if y + h < fy + fh - EPS:
            out.append((fx, y + h, fw, fy + fh - (y + h)))
    res = []
    for i, a in enumerate(out):
        if a[2] <= EPS or a[3] <= EPS:
            continue
        contained = False
        for j, b in enumerate(out):
            if i != j and a[0] >= b[0] - EPS and a[1] >= b[1] - EPS \
                    and a[0] + a[2] <= b[0] + b[2] + EPS \
                    and a[1] + a[3] <= b[1] + b[3] + EPS \
                    and a[2] * a[3] < b[2] * b[3] - EPS:
                contained = True
                break
        if not contained:
            res.append(a)
    return res


def _beam_pack_one_sheet(counts: tuple, tiers: list,
                         sheet_w: float, sheet_h: float,
                         beam_width: int, max_pieces: int,
                         min_gap: float,
                         hard_bonus: float = 0.0):
    """单张板的束搜索最佳填充。

    counts: 各尺寸档剩余数量（元组，与 tiers 对齐）。
    hard_bonus: >0 时每放一件"难排档"（tiers[i][3] 为真）额外加分，
    让难排件尽早与最优搭档同板，避免留到库存末尾互相卡死。
    返回 (placed, next_counts)：placed 为
    [(tier_idx, rot, x, y, w, h)]，按放置顺序。
    """
    start = (counts, (), ((0.0, 0.0, sheet_w, sheet_h),), 0.0)
    states = [start]
    best = start
    for _depth in range(max_pieces):
        nxt = []
        for cnt, placed, free, used in states:
            for (fx, fy, fw, fh) in free:
                for ti, (_key, variants, _dq, _hard) in enumerate(tiers):
                    if cnt[ti] <= 0:
                        continue
                    for rot, w, h, _rb, _coords, notches in variants:
                        if w <= fw + EPS and h <= fh + EPS:
                            nc = list(cnt)
                            nc[ti] -= 1
                            if min_gap > EPS:
                                # 占用区外扩 gap（裁到板边）挖空，
                                # 保证后续放置与本件间距 >= min_gap
                                nfree = _subtract_free_gap(
                                    free, fx, fy, w, h, min_gap,
                                    sheet_w, sheet_h)
                            else:
                                nfree = _subtract_free(
                                    free, fx, fy, w, h, sheet_w, sheet_h)
                            if notches:
                                nfree = nfree + _global_notches(
                                    notches, fx, fy, min_gap)
                            nxt.append((tuple(nc),
                                        placed + ((ti, rot, fx, fy, w, h),),
                                        nfree, used + w * h))
        if not nxt:
            break
        seen = set()
        uniq = []
        for st in nxt:
            key = (st[0], tuple(sorted((p[2], p[3], p[0]) for p in st[1])))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(st)
        if hard_bonus > 0:
            uniq.sort(
                key=lambda st: (
                    st[3] + hard_bonus
                    * sum(1 for p in st[1] if tiers[p[0]][3]),
                    -len(st[2]),
                ),
                reverse=True)
        else:
            uniq.sort(key=lambda st: (st[3], -len(st[2])), reverse=True)
        states = uniq[:beam_width]
        if states and states[0][3] > best[3]:
            best = states[0]
    return best[1], best[0]


def _subtract_free_gap(free: list, x: float, y: float, w: float, h: float,
                       gap: float, sheet_w: float, sheet_h: float) -> list:
    """min_gap > 0 时，把占用区外扩 gap 后再挖一遍（外扩部分裁到板边）。"""
    gx = max(0.0, x - gap)
    gy = max(0.0, y - gap)
    gw = min(sheet_w, x + w + gap) - gx
    gh = min(sheet_h, y + h + gap) - gy
    return _subtract_free(free, gx, gy, gw, gh, sheet_w, sheet_h)


def _global_notches(notches: list, fx: float, fy: float,
                    min_gap: float) -> list:
    """缺口空区平移到放置位置；min_gap 时四边内缩，保证与 L 形件的间距。"""
    out = []
    for (dx, dy, w, h) in notches:
        if min_gap > EPS:
            dx += min_gap
            dy += min_gap
            w -= 2 * min_gap
            h -= 2 * min_gap
        if w > EPS and h > EPS:
            out.append((fx + dx, fy + dy, w, h))
    return out


def _beam_pack_multi(counts: tuple, tiers: list, n_sheets: int,
                     sheet_w: float, sheet_h: float,
                     beam_width: int, min_gap: float,
                     deadline: float | None = None):
    """多板耦合束搜索：把剩余库存**同时**排进 n_sheets 张板。

    状态 = (各档剩余, 每板空闲矩形, 每板已放, 已用 bbox 面积)。
    全部放完即成功（返回每板 placed 列表），状态耗尽返回 None。
    用于尾部精确求解：逐板贪心把大板用完后，剩下的零散件往往
    彼此凑不齐一张板；耦合搜索在同一状态空间里协调多张板的分组，
    不存在"留到最后"的问题。
    """
    full = ((0.0, 0.0, sheet_w, sheet_h),)
    start = (counts, tuple(full for _ in range(n_sheets)),
             tuple(() for _ in range(n_sheets)), 0.0)
    states = [start]
    while states:
        if deadline is not None and time.time() > deadline:
            return None
        # 已完成？
        done = [st for st in states if sum(st[0]) == 0]
        if done:
            done.sort(key=lambda st: -st[3])
            return [list(p) for p in done[0][2]]
        nxt = []
        for cnt, frees, placeds, used in states:
            if sum(cnt) == 0:
                continue
            for si in range(n_sheets):
                for (fx, fy, fw, fh) in frees[si]:
                    for ti, (_key, variants, _dq, _hard) in enumerate(tiers):
                        if cnt[ti] <= 0:
                            continue
                        for rot, w, h, _rb, _coords, notches in variants:
                            if w <= fw + EPS and h <= fh + EPS:
                                nc = list(cnt)
                                nc[ti] -= 1
                                if min_gap > EPS:
                                    nf = _subtract_free_gap(
                                        frees[si], fx, fy, w, h, min_gap,
                                        sheet_w, sheet_h)
                                else:
                                    nf = _subtract_free(
                                        frees[si], fx, fy, w, h,
                                        sheet_w, sheet_h)
                                if notches:
                                    nf = nf + _global_notches(
                                        notches, fx, fy, min_gap)
                                nfrees = list(frees)
                                nfrees[si] = nf
                                npl = list(placeds)
                                npl[si] = npl[si] + ((ti, rot, fx, fy, w, h),)
                                nxt.append((tuple(nc), tuple(nfrees),
                                            tuple(npl), used + w * h))
        if not nxt:
            return None
        seen = set()
        uniq = []
        for st in nxt:
            sig = tuple(sorted(
                tuple(sorted((p[2], p[3], p[0]) for p in sheet))
                for sheet in st[2]))
            key = (st[0], sig)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(st)
        uniq.sort(key=lambda st: (st[3], -sum(len(f) for f in st[1])),
                  reverse=True)
        states = uniq[:beam_width]
    return None


def _beam_pass(parts, tiers, counts, sheet_w, sheet_h, cache,
               beam_width, rotations, max_pieces, min_gap,
               hard_bonus, time_budget_s, greedy_fallback, progress,
               multi_tail: bool = True, multi_beam_width: int = 500):
    """单遍逐板束搜索：从给定库存排完全部零件。"""
    sheets = []
    t0 = time.time()
    sheet_area = sheet_w * sheet_h
    multi_tried = False

    def build_sheet(placed):
        sheet = []
        for ti, rot, x, y, w, h in placed:
            idx = tiers[ti][2].popleft()
            part = parts[idx]
            form = _match_form(part, cache, rotations, rot, w, h)
            _r, rb, coords = form
            poly = _translate_coords(coords, x - rb[0], y - rb[1])
            sheet.append(_Placement(part=part, rot=rot, x=x, y=y, poly=poly))
        return sheet

    while sum(counts) > 0:
        remaining = sum(counts)
        if multi_tail and not multi_tried and 1 < remaining <= 24:
            rem_area = sum(
                tiers[ti][0][0] * tiers[ti][0][1] * c
                for ti, c in enumerate(counts))
            if rem_area <= 3.2 * sheet_area:
                multi_tried = True
                k = max(1, math.ceil(rem_area / sheet_area))
                sub_tiers = [[k2, v, deque(dq), h]
                             for k2, v, dq, h in tiers]
                deadline = time.time() + 20.0
                got = _beam_pack_multi(counts, sub_tiers, k, sheet_w,
                                       sheet_h, multi_beam_width, min_gap,
                                       deadline=deadline)
                if got is not None:
                    for placed_j in got:
                        if not placed_j:
                            continue
                        sheets.append(build_sheet(placed_j))
                    counts = tuple(0 for _ in counts)
                    break
        placed, counts = _beam_pack_one_sheet(
            counts, tiers, sheet_w, sheet_h, beam_width, max_pieces,
            min_gap, hard_bonus=hard_bonus)
        if not placed:
            numbers = [parts[idx].number
                       for _k2, _v, dq, _h in tiers for idx in dq]
            raise ValueError(
                f"以下零件无法放入 {sheet_w:.0f}x{sheet_h:.0f} 大板：{numbers}")
        sheets.append(build_sheet(placed))
        if progress:
            progress(len(sheets), sum(counts))
        if time_budget_s is not None and sum(counts) > 0 \
                and time.time() - t0 > time_budget_s:
            if greedy_fallback is None:
                break
            remaining = [parts[idx]
                         for _k, _v, dq, _h in tiers for idx in dq]
            rest = greedy_fallback(remaining)
            sheets.extend(rest)
            break
    return sheets


def _reset_tiers(tiers, parts, cache, rotations, hard_tier_idxs=frozenset()):
    """重建尺寸档库存（每个 part 各一件），并打难排标记。"""
    fresh = []
    for ti, (key, variants, _dq, _hard) in enumerate(tiers):
        fresh.append([key, variants, deque(), ti in hard_tier_idxs])
    for idx, part in enumerate(parts):
        key = _part_tier_key(part, cache, rotations)
        for entry in fresh:
            if entry[0] == key:
                entry[2].append(idx)
                break
    return fresh


def beam_layout(parts: list, sheet_w: float, sheet_h: float,
                cache: dict, *, beam_width: int = 1000,
                rotations: tuple = (0, 90, 180, 270),
                min_gap: float = 0.0,
                max_pieces: int = _MAX_PIECES_PER_SHEET,
                time_budget_s: float | None = None,
                greedy_fallback=None,
                refine_tail: bool = True,
                progress=None) -> list:
    """逐板束搜索排板，返回与 `_nest_single` 相同的 list[list[_Placement]]。

    beam_width：每层保留的部分布局数（越大越优越慢，CGR45 上 1000 约 40s）；
    time_budget_s：可选总时间预算；超出后剩余零件交给 greedy_fallback
    （一个 `_nest_single` 兼容函数）收尾，保证结果完整；
    refine_tail：两遍法——第一遍找出尾板（低填充板上的难排件），
    第二遍给难排件加权，让它们尽早在满库存里找到最优搭档，
    避免尾板互相卡死；两遍取较优者，不会比单遍差；
    其余参数与 `_nest_single` 同义。
    """
    tiers = _build_tiers(parts, cache, rotations)
    if not tiers:
        raise ValueError("没有可排板的零件")
    counts = tuple(len(dq) for _k, _v, dq, _h in tiers)

    sheets = _beam_pass(parts, tiers, counts, sheet_w, sheet_h, cache,
                        beam_width, rotations, max_pieces, min_gap,
                        0.0, time_budget_s, greedy_fallback, progress)
    if not refine_tail or len(sheets) < 3:
        return sheets

    # 难排档 = 低填充尾板（最差 3 张及所有低于 65% 的板）上的零件档
    sheet_area = sheet_w * sheet_h
    used_areas = [
        sum((pl.poly.bounds[2] - pl.poly.bounds[0])
            * (pl.poly.bounds[3] - pl.poly.bounds[1]) for pl in pls)
        for pls in sheets
    ]
    fills = sorted(range(len(sheets)), key=lambda i: used_areas[i])
    tail = set(fills[:3])
    for i, used in enumerate(used_areas):
        if used < 0.65 * sheet_area:
            tail.add(i)
    hard_tiers = set()
    for i in tail:
        for pl in sheets[i]:
            key = _part_tier_key(pl.part, cache, rotations)
            for ti, (k, _v, _dq, _h) in enumerate(tiers):
                if k == key:
                    hard_tiers.add(ti)
                    break
    if not hard_tiers:
        return sheets

    tiers2 = _reset_tiers(tiers, parts, cache, rotations, hard_tiers)
    counts2 = tuple(len(dq) for _k, _v, dq, _h in tiers2)
    sheets2 = _beam_pass(parts, tiers2, counts2, sheet_w, sheet_h, cache,
                         beam_width, rotations, max_pieces, min_gap,
                         sheet_area, time_budget_s, greedy_fallback, progress)
    return sheets2 if len(sheets2) <= len(sheets) else sheets


def _match_form(part, cache, rotations, rot, w, h):
    """取零件与目标 rot 一致的旋转形态（形状必须与束搜索规划所用的一致）。"""
    for r, _rp, rb, coords in _rotation_forms(part, cache, rotations):
        if abs(r - rot) <= EPS:
            return r, rb, coords
    raise ValueError(f"零件 {part.number} 没有匹配取向 rot={rot} {w}x{h}")
