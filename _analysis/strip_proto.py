"""矩形条带排样原型：验证 CGR45 用列式精确填充能到多少张。
零件按包围盒矩形处理（L 形件保守），旋转自选。
列 = 竖条（宽 w，高 1800），列内堆叠到接近 1800；板 = 若干列并排 BFD。
"""
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHEET_W, SHEET_H = 3200.0, 1800.0


def load_rects():
    with (ROOT / "_analysis" / "cgr45_parts.pkl").open("rb") as fh:
        parts = pickle.load(fh)
    rects = []
    for p in parts:
        b = p.outer_polygon.bounds
        w, h = b[2] - b[0], b[3] - b[1]
        rects.append({"num": p.number, "w": w, "h": h, "area": w * h})
    return rects


def best_gap_fill(cands, gap_h, col_w, orient_fn):
    """从候选中挑一个放进 (w<=col_w, h<=gap_h) 的件，
    优先高度贴满、其次宽度贴满列宽。返回 (index, w, h) 或 None。"""
    best = None
    for i, r in enumerate(cands):
        for w, h in orient_fn(r):
            if w <= col_w + 1e-6 and h <= gap_h + 1e-6:
                score = (h / gap_h, w / col_w)
                if best is None or score > best[0]:
                    best = (score, i, w, h)
    if best is None:
        return None
    _score, i, w, h = best
    return i, w, h


def column_pack(rects, anchor_policy="area"):
    """构建列：每次取锚件，尽量把列填到 1800。"""
    remaining = list(rects)
    columns = []

    def orients(r):
        outs = []
        if r["h"] <= SHEET_H + 1e-6 and r["w"] <= SHEET_W + 1e-6:
            outs.append((r["w"], r["h"]))
        if r["w"] <= SHEET_H + 1e-6 and r["h"] <= SHEET_W + 1e-6 \
                and abs(r["w"] - r["h"]) > 1e-6:
            outs.append((r["h"], r["w"]))
        return outs

    while remaining:
        if anchor_policy == "area":
            anchor = max(remaining, key=lambda r: r["area"])
        else:
            anchor = max(remaining, key=lambda r: max(r["w"], r["h"]))
        ao = orients(anchor)
        ao.sort(key=lambda wh: (wh[1] / max(wh[0], 1e-9), wh[1]), reverse=True)
        aw, ah = ao[0]
        remaining.remove(anchor)
        col = [(anchor, aw, ah)]
        col_w = aw
        used_h = ah
        while True:
            gap = SHEET_H - used_h
            pick = best_gap_fill(remaining, gap, col_w, orients)
            if pick is None:
                break
            i, w, h = pick
            col.append((remaining[i], w, h))
            remaining.pop(i)
            used_h += h
        columns.append({"w": col_w, "used_h": used_h, "items": col})
    return columns


def sheets_from_columns(columns):
    cols = sorted(columns, key=lambda c: -c["w"])
    sheets = []
    for c in cols:
        best = None
        for si, (uw, members) in enumerate(sheets):
            if uw + c["w"] <= SHEET_W + 1e-6:
                slack = SHEET_W - uw - c["w"]
                if best is None or slack < best[0]:
                    best = (slack, si)
        if best is None:
            sheets.append((c["w"], [c]))
        else:
            uw, members = sheets[best[1]]
            members.append(c)
            sheets[best[1]] = (uw + c["w"], members)
    return sheets


def report(rects, sheets, label):
    bbox_total = sum(r["area"] for r in rects)
    sheet_area = SHEET_W * SHEET_H
    print(f"[{label}] {len(sheets)} 张, bbox 利用率 "
          f"{bbox_total / len(sheets) / sheet_area:.1%}")
    col_h = sorted(c["used_h"] / SHEET_H for _uw, mem in sheets for c in mem)
    print(f"  列数 {len(col_h)}, 列高利用率 min={col_h[0]:.0%} "
          f"med={col_h[len(col_h) // 2]:.0%} max={col_h[-1]:.0%}")
    col_waste = sum((SHEET_H - c["used_h"]) * c["w"]
                    for _uw, mem in sheets for c in mem)
    print(f"  列内纵向浪费 {col_waste / 1e6:.1f} m², "
          f"板内横向剩余 {sum(SHEET_W - uw for uw, _m in sheets) / 1e3:.0f} m")
    return len(sheets)


def main():
    rects = load_rects()
    print(f"零件 {len(rects)}, bbox 总面积 "
          f"{sum(r['area'] for r in rects) / 1e6:.1f} m²")
    for policy in ("area", "long"):
        columns = column_pack(rects, anchor_policy=policy)
        sheets = sheets_from_columns(columns)
        report(rects, sheets, f"列式 anchor={policy}")


if __name__ == "__main__":
    main()
