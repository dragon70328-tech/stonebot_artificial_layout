"""桥切 LNS 攻坚的共享实验台。

- get_parts(): 读取 CGR45 fixture 的零件（带 pickle 缓存）
- get_baseline_layout(): 复现 nest_parts 的贪心阶段（默认 10 配置），
  返回内部 _Placement 布局（带 pickle 缓存）
- fill_report(): 板填充率分布
- template_layout(): 加载 50 张已验证模板布局（NestingResult）
- reviewed_layout(): 加载 51 张回看后布局（NestingResult）
"""
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "dxf" / "CGR45final.dxf"
TEMPLATE_DXF = ROOT / "drawing_profiles" / "validated_templates" / "cgr45_3200x1800_v3.dxf"
REVIEWED_DXF = ROOT / "output" / "20260923_220414_CGR45final" / "CGR45final_nested_3200x1800.dxf"
PARTS_PKL = ROOT / "_analysis" / "cgr45_parts.pkl"
LAYOUT_PKL = ROOT / "_analysis" / "cgr45_layout52.pkl"

SHEET_W, SHEET_H = 3200.0, 1800.0
ROT = (0, 90, 180, 270)


def get_parts():
    if PARTS_PKL.exists():
        with PARTS_PKL.open("rb") as fh:
            return pickle.load(fh)
    from src.workflow import prepare_drawing
    prepared = prepare_drawing(str(FIXTURE), use_case_library=False)
    parts = prepared.parts
    with PARTS_PKL.open("wb") as fh:
        pickle.dump(parts, fh)
    return parts


def get_baseline_layout():
    """复现 nest_parts 贪心阶段（budget=0），返回 (sheets_pl, cache)。"""
    if LAYOUT_PKL.exists():
        with LAYOUT_PKL.open("rb") as fh:
            return pickle.load(fh)
    from src.nesting import (
        _nest_single, _make_sort_key, _default_configs,
        _placement_manufacturability_score,
    )
    parts = get_parts()
    cache: dict = {}
    best_pl, best_key = None, None
    for sort_name, mode, cfg_seed in _default_configs(0):
        sort_key = _make_sort_key(sort_name, cfg_seed)
        t0 = time.time()
        sheets_pl = _nest_single(parts, SHEET_W, SHEET_H, sort_key, mode,
                                 cache, rotations=ROT, min_gap=0.0)
        compact = sum(max((pl.poly.bounds[3] for pl in pls), default=0.0)
                      for pls in sheets_pl)
        manu = _placement_manufacturability_score(sheets_pl, SHEET_W, SHEET_H)
        key = (len(sheets_pl), -manu, compact)
        print(f"  cfg {sort_name}/{mode}: {len(sheets_pl)} 张 "
              f"({time.time() - t0:.1f}s)", flush=True)
        if best_key is None or key < best_key:
            best_key, best_pl = key, sheets_pl
    with LAYOUT_PKL.open("wb") as fh:
        pickle.dump((best_pl, cache), fh)
    return best_pl, cache


def fill_report(sheets_pl, label=""):
    """sheets_pl: list[list[_Placement]] 或 NestingResult。"""
    from src.nesting import _sheet_used_area
    sheet_area = SHEET_W * SHEET_H
    if hasattr(sheets_pl, "sheets"):  # NestingResult
        fills = sorted(
            sum(p.area for p in s.parts) / sheet_area
            for s in sheets_pl.sheets
        )
        n = len(sheets_pl.sheets)
    else:
        fills = sorted(_sheet_used_area(s) / sheet_area for s in sheets_pl)
        n = len(sheets_pl)
    buckets = {"<50%": 0, "50-60%": 0, "60-70%": 0, "70-80%": 0, ">=80%": 0}
    for f in fills:
        if f < 0.5:
            buckets["<50%"] += 1
        elif f < 0.6:
            buckets["50-60%"] += 1
        elif f < 0.7:
            buckets["60-70%"] += 1
        elif f < 0.8:
            buckets["70-80%"] += 1
        else:
            buckets[">=80%"] += 1
    print(f"[{label}] {n} 张, 填充率 min={fills[0]:.1%} "
          f"med={fills[n // 2]:.1%} max={fills[-1]:.1%} "
          f"mean={sum(fills) / n:.1%}")
    print(f"  分布: {buckets}")
    print(f"  最低 10 张: {[f'{f:.0%}' for f in fills[:10]]}")
    return fills


def template_layout():
    from src.nesting_reviewer import load_nested_dxf
    return load_nested_dxf(str(TEMPLATE_DXF))


def reviewed_layout():
    from src.nesting_reviewer import load_nested_dxf
    return load_nested_dxf(str(REVIEWED_DXF))


if __name__ == "__main__":
    t0 = time.time()
    parts = get_parts()
    print(f"parts: {len(parts)}, 总净面积 {sum(p.area for p in parts) / 1e6:.1f} m², "
          f"面积下界 {sum(p.area for p in parts) / (SHEET_W * SHEET_H):.1f} 张 "
          f"({time.time() - t0:.1f}s)", flush=True)

    best_pl, _cache = get_baseline_layout()
    print(f"贪心基线: {len(best_pl)} 张", flush=True)
    fill_report(best_pl, "算法贪心 52")

    fill_report(reviewed_layout(), "回看后 51")
    fill_report(template_layout(), "已验证模板 50")
