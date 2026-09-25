"""逐板"最佳填充"束搜索原型：每块板都问"当前库存能摆出的最满布局"，
而不是按全局排序放下一个零件。矩形（包围盒）精确摆放。

状态 = (各尺寸档剩余数量, 已放 [(x,y,w,h,tier)], 空闲矩形列表)
扩展 = 在每个空闲矩形的左下角放任一可行尺寸档（两种取向）
评分 = 已用 bbox 面积；beam 保留前 K 个
"""
import pickle
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHEET_W, SHEET_H = 3200.0, 1800.0
BEAM = 300
MAX_PIECES = 12


def load_tiers():
    with (ROOT / "_analysis" / "cgr45_parts.pkl").open("rb") as fh:
        parts = pickle.load(fh)
    tiers = {}
    for p in parts:
        b = p.outer_polygon.bounds
        w, h = round(b[2] - b[0], 1), round(b[3] - b[1], 1)
        key = (min(w, h), max(w, h))
        tiers.setdefault(key, []).append(p.number)
    return tiers  # {(short, long): [numbers]}


def orientations(tier):
    s, l = tier
    outs = []
    if l <= SHEET_W and s <= SHEET_H:
        outs.append((l, s))   # 横放：宽=l 高=s
    if s <= SHEET_W and l <= SHEET_H:
        outs.append((s, l))   # 竖放：宽=s 高=l
    return outs


def subtract(free, x, y, w, h):
    """从空闲矩形列表中挖去 (x,y,w,h)，返回新的互不重叠分区。
    对每个与占用区相交的空闲矩形，切出上/下/左/右四块。"""
    out = []
    for (fx, fy, fw, fh) in free:
        if x >= fx + fw or x + w <= fx or y >= fy + fh or y + h <= fy:
            out.append((fx, fy, fw, fh))
            continue
        # 左
        if x > fx:
            out.append((fx, fy, x - fx, fh))
        # 右
        if x + w < fx + fw:
            out.append((x + w, fy, fx + fw - (x + w), fh))
        # 下
        if y > fy:
            out.append((fx, fy, fw, y - fy))
        # 上
        if y + h < fy + fh:
            out.append((fx, y + h, fw, fy + fh - (y + h)))
    # 去掉被包含的碎片（简单 O(n²) 过滤）
    res = []
    for i, a in enumerate(out):
        contained = False
        for j, b in enumerate(out):
            if i != j and a[0] >= b[0] - 1e-6 and a[1] >= b[1] - 1e-6 \
                    and a[0] + a[2] <= b[0] + b[2] + 1e-6 \
                    and a[1] + a[3] <= b[1] + b[3] + 1e-6 \
                    and (a[2] * a[3]) < (b[2] * b[3]) - 1e-6:
                contained = True
                break
        if not contained and a[2] > 1e-6 and a[3] > 1e-6:
            res.append(a)
    return res


def pack_one_sheet(inv, tier_list, beam=BEAM, cap=None, rng=None,
                   rank_mode="fill"):
    """inv: Counter {tier: count}。返回 (used_area, placed)。
    cap: 本板 bbox 填充上限（面积）；达到或超过后停止扩展，
    把大件留给后面的板，避免尾部饿死。cap 不可达时退化为最大填充。"""
    start = (tuple(inv[t] for t in tier_list), (), ((0.0, 0.0, SHEET_W, SHEET_H),), 0.0)
    states = [start]
    best = states[0]
    best_under = None
    for _depth in range(MAX_PIECES):
        nxt = []
        for counts, placed, free, used in states:
            if cap is not None and used >= cap:
                continue
            for fi, (fx, fy, fw, fh) in enumerate(free):
                for ti, tier in enumerate(tier_list):
                    if counts[ti] <= 0:
                        continue
                    for w, h in orientations(tier):
                        if w <= fw + 1e-6 and h <= fh + 1e-6:
                            nc = list(counts)
                            nc[ti] -= 1
                            nfree = subtract(free, fx, fy, w, h)
                            nplaced = placed + ((fx, fy, w, h, tier),)
                            nused = used + w * h
                            nxt.append((tuple(nc), nplaced, nfree, nused))
        if not nxt:
            break
        # 去重：同一 (counts, placed 的规范化) 只留一个
        seen = set()
        uniq = []
        for st in nxt:
            key = (st[0], tuple(sorted((p[0], p[1], p[4]) for p in st[1])))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(st)
        if rank_mode != "fill":
            # 可用空间评分：空闲矩形中能装下剩余最小档之一的面积，
            # 惩罚谁也装不下的死缝。usable 只对前 2*beam 名计算，控制耗时。
            lam = 1.0 if rank_mode == "fill_usable" else 0.5

            small_cache = {}

            def small_tiers(counts):
                small = small_cache.get(counts)
                if small is None:
                    rem = [tier_list[ti] for ti, c in enumerate(counts) if c > 0]
                    small = sorted(rem, key=lambda t: t[0] * t[1])[:10]
                    small_cache[counts] = small
                return small

            def usable(st):
                u = 0.0
                for (fx, fy, fw, fh) in st[2]:
                    for t in small_tiers(st[0]):
                        if (t[0] <= fw and t[1] <= fh) or \
                                (t[1] <= fw and t[0] <= fh):
                            u += fw * fh
                            break
                return u

            head = uniq[:beam * 2]
            head.sort(key=lambda st: (st[3] + lam * usable(st),
                                      -len(st[2])), reverse=True)
            uniq = head + uniq[beam * 2:]
        elif rng is None:
            uniq.sort(key=lambda st: (st[3], -len(st[2])), reverse=True)
        else:
            uniq.sort(key=lambda st: (st[3] * rng.uniform(0.97, 1.03),
                                      -len(st[2])), reverse=True)
        states = uniq[:beam]
        if states and states[0][3] > best[3]:
            best = states[0]
        for st in states:
            if st[1] and (cap is None or st[3] <= cap):
                if best_under is None or st[3] > best_under[3]:
                    best_under = st
        if cap is not None and best_under is not None \
                and best_under[3] >= cap * 0.995:
            break
    if cap is not None and best_under is not None:
        return best_under
    return best


def inv_from_layout(layout):
    inv = Counter()
    for sheet in layout:
        for _x, _y, _w, _h, tier in sheet:
            inv[tier] += 1
    return inv


def compress(layout, tier_list, beam=BEAM, rounds=8):
    """LNS 压缩：反复取填充最低的 k 张板，尝试用 beam 塞进 k-1 张。"""
    sheet_area = SHEET_W * SHEET_H
    for _ in range(rounds):
        fills = sorted(range(len(layout)),
                       key=lambda i: sum(p[2] * p[3] for p in layout[i]))
        improved = False
        for k in (2, 3, 4, 5):
            sel = fills[:k]
            sub = Counter()
            for i in sel:
                for _x, _y, _w, _h, tier in layout[i]:
                    sub[tier] += 1
            total_area = sum(t[0] * t[1] * n for t, n in sub.items())
            if total_area > (k - 1) * sheet_area:
                continue
            new_sheets = []
            ok = True
            tmp = Counter(sub)
            while sum(tmp.values()) > 0:
                if len(new_sheets) >= k - 1:
                    ok = False
                    break
                counts, placed, free, used = pack_one_sheet(tmp, tier_list,
                                                          beam=beam)
                if not placed:
                    ok = False
                    break
                for _x, _y, _w, _h, tier in placed:
                    tmp[tier] -= 1
                new_sheets.append(placed)
            if ok:
                layout = [s for i, s in enumerate(layout) if i not in sel] \
                    + new_sheets
                improved = True
                break
        if not improved:
            break
    return layout


def main():
    tiers = load_tiers()
    tier_list = sorted(tiers.keys(), key=lambda t: -(t[0] * t[1]))
    total_inv = Counter({t: len(nums) for t, nums in tiers.items()})
    sheet_area = SHEET_W * SHEET_H
    bbox_total = sum(t[0] * t[1] * n for t, n in total_inv.items())
    print(f"尺寸档 {len(tier_list)}, 零件 {sum(total_inv.values())}, "
          f"bbox 总面积 {bbox_total / 1e6:.1f} m² "
          f"(下界 {bbox_total / sheet_area:.1f} 张)")

    import time
    for label, beam, rank_mode in (("b1000 fill", 1000, "fill"),
                                   ("b1000 fill+usable", 1000, "fill_usable"),
                                   ("b1000 fill+0.5usable", 1000, "fill_usable_half"),
                                   ("b1500 fill+usable", 1500, "fill_usable")):
        t0 = time.time()
        inv = Counter(total_inv)
        layout = []
        while sum(inv.values()) > 0:
            _c, placed, _f, _u = pack_one_sheet(inv, tier_list,
                                                beam=beam,
                                                rank_mode=rank_mode)
            if not placed:
                break
            for _x, _y, _w, _h, tier in placed:
                inv[tier] -= 1
            layout.append(placed)
        fills = sorted(sum(p[2] * p[3] for p in s) / sheet_area
                       for s in layout)
        print(f"[{label}] {len(layout)} 张, 尾板 {fills[0]:.0%}, "
              f"次低 {fills[1]:.0%}, med={fills[len(fills) // 2]:.0%}, "
              f"{time.time() - t0:.0f}s", flush=True)
    return

    for target in (48, 49, 50, 51):
        inv = Counter(total_inv)
        layout = []
        while sum(inv.values()) > 0 and len(layout) < target:
            remaining_area = sum(t[0] * t[1] * n for t, n in inv.items())
            slots = target - len(layout)
            cap = remaining_area / slots
            if cap > sheet_area:
                cap = None  # 面积都装不下，全力填
            _c, placed, _f, _u = pack_one_sheet(inv, tier_list,
                                                cap=cap)
            if not placed:
                break
            for _x, _y, _w, _h, tier in placed:
                inv[tier] -= 1
            layout.append(placed)
        leftover = sum(inv.values())
        ok = leftover == 0 and len(layout) <= target
        fills = sorted(sum(p[2] * p[3] for p in s) / sheet_area
                       for s in layout)
        tail = f"尾板 {fills[0]:.0%}" if ok else f"剩 {leftover} 件未排"
        print(f"目标 {target}: {'可行' if ok else '不可行'} "
              f"用 {len(layout)} 张, {tail}, "
              f"min={fills[0]:.0%} med={fills[len(fills) // 2]:.0%} "
              f"max={fills[-1]:.0%}", flush=True)


if __name__ == "__main__":
    main()
