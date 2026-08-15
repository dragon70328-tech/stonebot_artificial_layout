#!python
# -*- coding: utf-8 -*-
"""DXF 自动排板系统 - 主入口（支持 CLI 参数 + 交互模式）"""

import argparse
import json
import math
import os
import sys
import time
import itertools
from datetime import datetime
from pathlib import Path
from shapely import affinity
from shapely.geometry import Point
import shapely

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROJECT_ROOT = Path(__file__).resolve().parent

from src.units import UnitSystem, UNIT_LABELS, convert_to_mm
from src.dxf_reader import read_dxf
from src.numbering import assign_numbers
from src.nesting import nest_parts, validate_nesting
from src.dxf_writer import write_nested_dxf, write_numbered_parts_dxf
from src.models import NestingResult, Part, Sheet
from src.constraints import (
    NestingProfile, PROFILES, PROFILE_HELP,
    PROFILE_MIN_SHEETS,
    STANDARD_SHEET_SIZES, STANDARD_THICKNESSES,
    get_sheet_size, get_sheet_size_by_index,
)
from src.postprocess import PostProcessor
# ═══════════════════════════════════════════════════════════════
#  CLI 参数
# ═══════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="DXF 自动排板系统")

    # 必选 / 定位参数
    p.add_argument("dxf", nargs="?", help="规格板 DXF 文件路径")
    p.add_argument("width", nargs="?", type=float, help="大板长度 X (mm)")
    p.add_argument("height", nargs="?", type=float, help="大板宽度 Y (mm)")
    p.add_argument("thickness", nargs="?", type=float, help="大板厚度 (mm)")

    # 排板参数
    p.add_argument("--trials", type=int, default=1,
                   help="独立试验次数（不同种子），默认 1")
    p.add_argument("--seed", type=int, default=0,
                   help="随机种子基数，默认 0")
    p.add_argument("--budget", type=float, default=180.0,
                   help="每次试验 LNS 搜索时间（秒），默认 180")
    p.add_argument("--quick", action="store_true",
                   help="快速模式：使用 4 组贪心配置，适合先看板数")
    p.add_argument("--imperial", action="store_true",
                   help="英制模式（默认公制）")

    # 约束模板
    p.add_argument("--profile", type=str, default=None,
                   choices=list(PROFILES.keys()),
                   help="约束模板：" + " / ".join(
                       f"{k}={v}" for k, v in PROFILE_HELP.items()))
    p.add_argument("--list-profiles", action="store_true",
                   help="列出可用约束模板后退出")

    # 约束覆盖（可与 --profile 组合，也可独立使用）
    p.add_argument("--rotation", type=str, default=None,
                   help="允许的旋转角度，逗号分隔，如 0,90")
    p.add_argument("--min-gap", type=float, default=None,
                   help="最小切割间距 mm")
    p.add_argument("--group", type=str, default=None,
                   choices=[None, "one_set_per_sheet"],
                   help="分组模式")
    p.add_argument("--no-slide", action="store_true",
                   help="关闭后处理推边")
    p.add_argument("--no-align", action="store_true",
                   help="关闭边缘对齐")
    p.add_argument("--no-confirm", action="store_true",
                   help="排板完成后不询问板数，直接开始后处理")
    p.add_argument("--report-only", action="store_true",
                   help="仅完成排板并报告大板使用量，不执行后处理和输出文件")
    p.add_argument("--special-size", type=str, default=None,
                   help="特殊面板排板尺寸，如 3225x1625")

    # 标准规格快捷选择
    p.add_argument("--list-sizes", action="store_true",
                   help="列出标准大板尺寸后退出")
    p.add_argument("--size", type=str, default=None,
                   help="选择标准大板尺寸，如 3200x1800 或序号 1")

    # DXF 读取
    p.add_argument("--include-unnumbered", action="store_true",
                   help="包含无编号封闭图形（默认跳过）")
    p.add_argument("--layers", type=str, default=None,
                   help="指定读取图层，逗号分隔，默认自动检测")
    p.add_argument("--exclude-layers", type=str, default=None,
                   help="排除图层，逗号分隔")

    return p.parse_args()


# ═══════════════════════════════════════════════════════════════
#  辅助
# ═══════════════════════════════════════════════════════════════

def make_output_dir(dxf_path: str) -> Path:
    """按 时间戳_文件名 创建输出子目录"""
    stem = Path(dxf_path).stem
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = PROJECT_ROOT / "output" / f"{ts}_{stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def resolve_sheet_size(args) -> tuple[float, float]:
    """从 --size / 定位参数 解析大板尺寸"""
    if args.size:
        # 尝试按标签匹配
        result = get_sheet_size(args.size)
        if result:
            return result
        # 尝试按序号匹配
        try:
            idx = int(args.size)
            item = get_sheet_size_by_index(idx)
            if item:
                return (item[0], item[1])
        except ValueError:
            pass
        print(f"错误：无法识别尺寸 '{args.size}'，"
              f"请用 --list-sizes 查看可用规格")
        sys.exit(1)
    return (args.width, args.height)


def print_sizes():
    """打印标准大板尺寸列表"""
    print(f"{'#':>3}  {'尺寸':>14}  {'面积 (m²)':>10}")
    print("-" * 32)
    for i, (w, h, label) in enumerate(STANDARD_SHEET_SIZES, 1):
        area = w * h / 1e6
        print(f"{i:>3}  {label:>14}  {area:>10.2f}")


def print_thicknesses():
    """打印标准厚度"""
    labels = ", ".join(f"{t:.0f}mm" for t in STANDARD_THICKNESSES)
    print(f"\n标准厚度：{labels}")


EPS = 1e-6
QUICK_CONFIGS = [
    ("short", "skyline", 0),
    ("short", "col", 0),
    ("area", "skyline", 0),
    ("long", "col", 0),
]


def parse_special_size(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    text = value.strip().lower().replace("×", "x").replace("x", "*")
    if "," in text:
        parts = text.split(",")
    elif "*" in text:
        parts = text.split("*")
    else:
        parts = text.split()
    try:
        w, h = [float(x.strip()) for x in parts[:2]]
        return w, h
    except Exception:
        print(f"错误：无法识别特殊板尺寸 '{value}'，示例：3225x1625")
        sys.exit(1)


def part_fits(part, width: float, height: float, rotations: list[int]) -> bool:
    outer = part.outer_polygon
    origin = (outer.centroid.x, outer.centroid.y)
    for angle in rotations:
        rotated = outer if not angle else affinity.rotate(outer, angle,
                                                          origin=origin)
        b = rotated.bounds
        if b[2] - b[0] <= width + EPS and b[3] - b[1] <= height + EPS:
            return True
    return False


def _normalized_rotated_polygon(polygon, angle: float):
    """绕质心旋转多边形，并将其包围盒左下角平移到原点。"""
    if angle:
        rotated = affinity.rotate(
            polygon, angle, origin=(polygon.centroid.x, polygon.centroid.y)
        )
    else:
        rotated = polygon
    minx, miny, _, _ = rotated.bounds
    if abs(minx) > EPS or abs(miny) > EPS:
        rotated = affinity.translate(rotated, xoff=-minx, yoff=-miny)
    return rotated


def _candidate_offsets(polygon, sheet_size: float, axis: str) -> list[float]:
    """候选左下角偏移：板边对齐 + 多边形顶点对齐。"""
    bounds = polygon.bounds
    size = bounds[2] - bounds[0] if axis == "x" else bounds[3] - bounds[1]
    values = {0.0, sheet_size - size}
    for x, y in polygon.exterior.coords:
        v = x if axis == "x" else y
        values.add(-v)
        values.add(sheet_size - v)
    return sorted(v for v in values
                  if v >= -EPS and v <= sheet_size - size + EPS)


def _rotated_part_form(part, angle: float):
    """返回 (旋转后的外轮廓, (宽, 高))，外轮廓包围盒左下角已归一化。"""
    polygon = _normalized_rotated_polygon(part.outer_polygon, angle)
    bounds = polygon.bounds
    return polygon, (bounds[2] - bounds[0], bounds[3] - bounds[1])


def _place_part_copy(part, angle: float, x: float, y: float):
    """生成放置后的 Part 副本，几何与孔洞/标签一起做刚体变换。"""
    outer = part.outer_polygon
    origin = (outer.centroid.x, outer.centroid.y)
    rotated_outer = (affinity.rotate(outer, angle, origin=origin)
                     if angle else outer)
    ox = x - rotated_outer.bounds[0]
    oy = y - rotated_outer.bounds[1]

    def transform(geom):
        g = affinity.rotate(geom, angle, origin=origin) if angle else geom
        return affinity.translate(g, xoff=ox, yoff=oy)

    label = Point(part.label_position)
    if angle:
        label = affinity.rotate(label, angle, origin=origin)
    label = affinity.translate(label, xoff=ox, yoff=oy)

    return Part(
        id=part.id,
        number=part.number,
        polygon=transform(part.polygon),
        outer_polygon=affinity.translate(rotated_outer, xoff=ox, yoff=oy),
        holes=[transform(h) for h in part.holes],
        original_number=part.original_number,
        area=part.area,
        label_position=(label.x, label.y),
    )


def _find_special_pair_placement(part_a, part_b,
                                  sheet_w: float, sheet_h: float,
                                  rotations: list[int]):
    """搜索两块特殊板头尾相接的可行放置，返回旋转角度和左下角坐标。"""
    for ra in rotations:
        pa, (wa, ha) = _rotated_part_form(part_a, ra)
        if wa > sheet_w + EPS or ha > sheet_h + EPS:
            continue
        xs_a = _candidate_offsets(pa, sheet_w, "x")
        ys_a = _candidate_offsets(pa, sheet_h, "y")
        for rb in rotations:
            pb, (wb, hb) = _rotated_part_form(part_b, rb)
            if wb > sheet_w + EPS or hb > sheet_h + EPS:
                continue
            xs_b = _candidate_offsets(pb, sheet_w, "x")
            ys_b = _candidate_offsets(pb, sheet_h, "y")
            for xa, ya in itertools.product(xs_a, ys_a):
                ca = affinity.translate(pa, xoff=xa, yoff=ya)
                ba = ca.bounds
                if (ba[0] < -EPS or ba[1] < -EPS or
                        ba[2] > sheet_w + EPS or ba[3] > sheet_h + EPS):
                    continue
                for xb, yb in itertools.product(xs_b, ys_b):
                    cb = affinity.translate(pb, xoff=xb, yoff=yb)
                    bb = cb.bounds
                    if (bb[0] < -EPS or bb[1] < -EPS or
                            bb[2] > sheet_w + EPS or bb[3] > sheet_h + EPS):
                        continue
                    if shapely.relate_pattern(ca, cb, "T********"):
                        continue
                    return ra, xa, ya, rb, xb, yb
    return None


def _first_fit_single_special(part, sheet_w, sheet_h, rotations):
    """返回单个特殊板能放入大板的最小角度。"""
    for angle in rotations:
        polygon, (w, h) = _rotated_part_form(part, angle)
        if w <= sheet_w + EPS and h <= sheet_h + EPS:
            return angle, polygon, (w, h)
    return None, None, None


def _build_special_sheet(placements, sheet_w, sheet_h, thickness):
    """placements: [(part, angle, x, y), ...]"""
    placed_parts = [
        _place_part_copy(part, angle, x, y)
        for part, angle, x, y in placements
    ]
    return Sheet(index=0, width=sheet_w, height=sheet_h,
                 thickness=thickness, parts=placed_parts)


def nest_special_parts(parts, sheet_w, sheet_h, thickness, unit, rotations):
    """特殊板两两头尾相接排板；无法配对的再单独放一张。"""
    remaining = sorted(parts, key=lambda p: (p.area, p.number), reverse=True)
    used = [False] * len(remaining)
    sheets = []

    for i, part in enumerate(remaining):
        if used[i]:
            continue
        used[i] = True
        pair = None
        for j in range(i + 1, len(remaining)):
            if used[j]:
                continue
            placement = _find_special_pair_placement(
                part, remaining[j], sheet_w, sheet_h, rotations
            )
            if placement is not None:
                pair = (j, placement)
                break

        if pair is not None:
            j, (ra, xa, ya, rb, xb, yb) = pair
            used[j] = True
            sheets.append(_build_special_sheet(
                [(part, ra, xa, ya), (remaining[j], rb, xb, yb)],
                sheet_w, sheet_h, thickness,
            ))
        else:
            angle, polygon, dims = _first_fit_single_special(
                part, sheet_w, sheet_h, rotations
            )
            if angle is None:
                raise RuntimeError(f"特殊板 {part.number} 无法放入 "
                                   f"{sheet_w:.0f}x{sheet_h:.0f} 大板")
            x = (sheet_w - dims[0]) / 2.0
            y = (sheet_h - dims[1]) / 2.0
            sheets.append(_build_special_sheet(
                [(part, angle, x, y)], sheet_w, sheet_h, thickness
            ))

    total_area = sum(p.area for p in parts)
    total_sheet_area = sum(s.total_area for s in sheets)
    return NestingResult(
        sheets=sheets, unit=unit,
        total_parts=len(parts), total_sheets=len(sheets),
        total_part_area=total_area, total_sheet_area=total_sheet_area,
    )


def combine_nesting_results(results: list) -> NestingResult:
    sheets = []
    total_parts = 0
    total_part_area = 0.0
    total_sheet_area = 0.0
    unit = results[0].unit if results else "metric"
    for result in results:
        sheets.extend(result.sheets)
        total_parts += result.total_parts
        total_part_area += result.total_part_area
        total_sheet_area += result.total_sheet_area
    for index, sheet in enumerate(sheets, start=1):
        sheet.index = index
    return NestingResult(sheets=sheets, unit=unit,
                         total_parts=total_parts,
                         total_sheets=len(sheets),
                         total_part_area=total_part_area,
                         total_sheet_area=total_sheet_area)


def validate_mixed_nesting(result: NestingResult) -> list:
    errors = []
    for sheet in result.sheets:
        single = NestingResult(
            sheets=[sheet], unit=result.unit,
            total_parts=len(sheet.parts), total_sheets=1,
            total_part_area=sum(p.area for p in sheet.parts),
            total_sheet_area=sheet.total_area,
        )
        errors.extend(validate_nesting(single, sheet.width, sheet.height))
    return errors

# ═══════════════════════════════════════════════════════════════
#  核心排板流程
# ═══════════════════════════════════════════════════════════════

def run(dxf_path: str, width: float, height: float, thickness: float,
        unit: UnitSystem, trials: int, seed: int, budget: float,
        skip_unnumbered: bool, layers: list | None,
        exclude_layers: list | None,
        profile: NestingProfile | None = None,
        confirm_sheet_count: bool = True,
        report_only: bool = False,
        special_size: tuple[float, float] | None = None,
        quick: bool = False) -> None:
    """核心排板流程"""
    if profile is None:
        profile = PROFILE_MIN_SHEETS

    unit_label = UNIT_LABELS[unit]
    width_mm = convert_to_mm(width, unit)
    height_mm = convert_to_mm(height, unit)
    thickness_from_args = convert_to_mm(thickness, unit) if thickness else None

    # 厚度优先级：CLI 参数 > profile > 默认 20
    effective_thickness = (
        thickness_from_args
        or profile.sheet_thickness
        or 20.0
    )

    # ── 打印约束摘要 ──
    rot_str = ",".join(str(r) for r in profile.rotation)
    print(f"约束: rotation=[{rot_str}], gap={profile.min_gap}mm, "
          f"group={profile.group_mode}, slide={profile.slide_to_edge}, "
          f"align={profile.align_edges}, thickness={effective_thickness}mm")

    # ── 读取 DXF ──
    print(f"读取 DXF: {dxf_path}")
    parts_data, _doc = read_dxf(dxf_path,
                                 panel_layers=layers,
                                 exclude_layers=exclude_layers)
    if not parts_data:
        print("错误：未找到任何封闭图形。")
        sys.exit(1)

    parts = assign_numbers(parts_data, skip_unnumbered=skip_unnumbered)
    total_area = sum(p.area for p in parts)
    special_w, special_h = special_size if special_size else (None, None)

    stem = Path(dxf_path).stem
    out_dir = None
    if not report_only:
        out_dir = make_output_dir(dxf_path)
        numbered_check = out_dir / f"{stem}_numbered_原位.dxf"
        write_numbered_parts_dxf(parts, str(numbered_check),
                                 unit_system=unit.value)

    normal_parts = []
    special_parts = []
    unfit_numbers = []
    for p in parts:
        if part_fits(p, width_mm, height_mm, profile.rotation):
            normal_parts.append(p)
        elif special_w and special_h and part_fits(p, special_w, special_h,
                                                    profile.rotation):
            special_parts.append(p)
        else:
            unfit_numbers.append(p.number)

    if unfit_numbers:
        print("错误：以下面板无法放入指定大板：")
        print(", ".join(unfit_numbers[:20]))
        sys.exit(1)

    normal_area = sum(p.area for p in normal_parts)
    special_area = sum(p.area for p in special_parts)
    normal_sheet_area = width_mm * height_mm
    if special_parts:
        special_sheet_area = special_w * special_h
        min_sheets = (math.ceil(normal_area / normal_sheet_area) +
                      math.ceil(special_area / special_sheet_area))
        special_pair_min_sheets = math.ceil(len(special_parts) / 2.0)
    else:
        min_sheets = math.ceil(total_area / normal_sheet_area)
        special_pair_min_sheets = None

    print(f"{len(parts)} 块零件，总面积 {total_area/1e6:.1f} m2，" 
          f"理论最少 {min_sheets} 张板")
    print(f"普通板 {len(normal_parts)} 块 -> {width_mm:.0f}x{height_mm:.0f} mm，"
          f"特殊板 {len(special_parts)} 块")
    if special_parts:
        print(f"特殊板排板尺寸: {special_w:.0f}x{special_h:.0f} mm")
        print(f"特殊板两两配对理论最少 {special_pair_min_sheets} 张")

    # ── 分别排板：普通板用名义尺寸，特殊板用指定大尺寸 ──
    print(f"排板中... ({trials} 次试验 x {budget:.0f}s LNS)")
    t0 = time.time()
    results = []
    if normal_parts:
        print(f"  普通板排板: {len(normal_parts)} 块, "
              f"{width_mm:.0f}x{height_mm:.0f}")
        results.append(nest_parts(
            normal_parts, width_mm, height_mm, effective_thickness,
            unit=unit.value, improve_budget=budget,
            trials=trials, seed=seed, rotations=profile.rotation,
            configs=QUICK_CONFIGS if quick else None))
    if special_parts:
        print(f"  特殊板排板: {len(special_parts)} 块, "
              f"{special_w:.0f}x{special_h:.0f}")
        results.append(nest_special_parts(
            special_parts, special_w, special_h, effective_thickness,
            unit=unit.value, rotations=profile.rotation))
    result = combine_nesting_results(results)
    elapsed = time.time() - t0

    # ── 排板完成后先报告板数，确认后再执行后处理 ──
    print(f"排板完成：使用 {result.total_sheets} 张大板，"
          f"出材率 {result.yield_rate:.2f}%，耗时 {elapsed:.1f}s")
    if report_only:
        print("已按 --report-only 停止：未执行后处理和输出文件。")
        return
    if confirm_sheet_count and sys.stdin.isatty():
        ans = input("是否接受该板数并开始后处理推板？[y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消：未执行后处理和输出。")
            return
    elif confirm_sheet_count:
        print("非交互模式：自动确认板数，开始后处理。")

    # ── 后处理：推边压实 + 边缘对齐 + 最小间距 ──
    if profile.slide_to_edge or profile.align_edges or profile.min_gap > 0:
        for sheet in result.sheets:
            pp = PostProcessor(sheet.width, sheet.height)
            pp.run(
                [sheet],
                slide=profile.slide_to_edge,
                align=profile.align_edges,
                gap_mm=profile.min_gap,
            )

    # ── 校验 ──
    errors = validate_mixed_nesting(result)
    status = "通过" if not errors else f"{len(errors)} 处违规"
    print(f"完成：{result.total_sheets} 张板，"
          f"出材率 {result.yield_rate:.2f}%，"
          f"校验{status}，耗时 {elapsed:.1f}s")
    for e in errors[:5]:
        print(f"  ! {e}")

    # ── 输出 ──
    if special_w and special_h and special_parts:
        suffix = (f"{int(width_mm)}x{int(height_mm)}"
                  f"+{int(special_w)}x{int(special_h)}")
    else:
        suffix = f"{int(width_mm)}x{int(height_mm)}"
    out_dxf = str(out_dir / f"{stem}_nested_{suffix}.dxf")
    out_json = str(out_dir / f"{stem}_report_{suffix}.json")

    write_nested_dxf(result, out_dxf, unit_system=unit.value)

    report = {
        "sheet_dimensions": {
            "width": width_mm, "height": height_mm,
            "special_width": special_w, "special_height": special_h,
            "thickness": effective_thickness, "unit": unit_label,
        },
        "total_sheets": result.total_sheets,
        "total_parts": result.total_parts,
        "total_part_area": round(result.total_part_area, 1),
        "total_sheet_area": result.total_sheet_area,
        "yield_rate": round(result.yield_rate, 2),
        "theoretical_min_sheets": min_sheets,
        "special_pair_min_sheets": special_pair_min_sheets,
        "elapsed_seconds": round(elapsed, 1),
        "validation_errors": len(errors),
        "profile": {
            "rotation": profile.rotation,
            "min_gap": profile.min_gap,
            "group_mode": profile.group_mode,
            "slide_to_edge": profile.slide_to_edge,
            "align_edges": profile.align_edges,
        },
        "parameters": {
            "trials": trials, "seed": seed, "budget_s": budget,
            "skip_unnumbered": skip_unnumbered,
        },
        "sheets": [
            {
                "index": s.index,
                "width": s.width,
                "height": s.height,
                "part_count": len(s.parts),
                "parts": [p.number for p in s.parts],
            }
            for s in result.sheets
        ],
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"DXF: {out_dxf}")
    print(f"报告: {out_json}")


# ═══════════════════════════════════════════════════════════════
#  交互模式
# ═══════════════════════════════════════════════════════════════

def interactive():
    """交互模式（无参数时）"""
    print("请选择单位制：")
    print("  [1] 公制 (mm)")
    print("  [2] 英制 (inch)")
    choice = input("请输入选项 (1/2): ").strip()
    unit = UnitSystem.IMPERIAL if choice == "2" else UnitSystem.METRIC

    # ── 选择大板尺寸 ──
    print("\n标准大板尺寸：")
    for i, (w, h, label) in enumerate(STANDARD_SHEET_SIZES, 1):
        area = w * h / 1e6
        print(f"  [{i}] {label}  ({area:.2f} m2)")
    print(f"  [0] 自定义")
    sz = input(f"请选择 (1-{len(STANDARD_SHEET_SIZES)}, 0=自定义): ").strip()
    try:
        idx = int(sz) if sz else 1
        if 1 <= idx <= len(STANDARD_SHEET_SIZES):
            width, height = STANDARD_SHEET_SIZES[idx - 1][0], STANDARD_SHEET_SIZES[idx - 1][1]
        else:
            width = float(input(f"大板长度 X ({UNIT_LABELS[unit]}): ").strip())
            height = float(input(f"大板宽度 Y ({UNIT_LABELS[unit]}): ").strip())
    except (ValueError, IndexError):
        print("错误：请输入有效数字。")
        sys.exit(1)

    # ── 选择厚度 ──
    labels = ", ".join(f"{t:.0f}mm" for t in STANDARD_THICKNESSES)
    print(f"\n标准厚度：{labels}")
    try:
        thickness = float(th) if th else 20.0
    except ValueError:
        print("错误：请输入有效数字。")
        sys.exit(1)

    # ── 选择约束模板 ──
    print("\n请选择约束模板：")
    for i, (key, label) in enumerate(PROFILE_HELP.items(), 1):
        print(f"  [{i}] {key}: {label}")
    p_choice = input(f"请输入选项 (1-{len(PROFILES)}, 默认1): ").strip()
    try:
        idx = int(p_choice) - 1 if p_choice else 0
        profile_key = list(PROFILES.keys())[idx]
    except (ValueError, IndexError):
        profile_key = "min_sheets"
    profile = PROFILES[profile_key]

    dxf_path = input("规格板 DXF 文件路径: ").strip()
    dxf_path = os.path.expanduser(dxf_path)
    if not os.path.exists(dxf_path):
        print(f"错误：文件不存在 - {dxf_path}")
        sys.exit(1)

    run(dxf_path, width, height, thickness, unit,
        trials=1, seed=0, budget=180.0,
        skip_unnumbered=True, layers=None, exclude_layers=None,
        profile=profile)


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    if args.list_profiles:
        print("可用约束模板：")
        for key, label in PROFILE_HELP.items():
            print(f"  {key:12s} {label}")
        return

    if args.list_sizes:
        print_sizes()
        print_thicknesses()
        return

    if not args.dxf:
        interactive()
        return

    dxf_path = os.path.expanduser(args.dxf)
    if not os.path.exists(dxf_path):
        print(f"错误：文件不存在 - {dxf_path}")
        sys.exit(1)

    width, height = resolve_sheet_size(args)
    if width is None or height is None:
        print("错误：CLI 模式需要指定大板尺寸，"
              "如 python main.py input.dxf 3200 1800 20\n"
              "或用 --size 选择标准规格，--list-sizes 查看")
        sys.exit(1)

    layers = args.layers.split(",") if args.layers else None
    exclude_layers = args.exclude_layers.split(",") if args.exclude_layers else None
    unit = UnitSystem.IMPERIAL if args.imperial else UnitSystem.METRIC

    # ── 构建 NestingProfile ──
    if args.profile:
        profile = PROFILES[args.profile]
    else:
        profile = PROFILE_MIN_SHEETS

    # 应用覆盖
    overrides = {}
    if args.rotation is not None:
        overrides["rotation"] = [int(x.strip())
                                 for x in args.rotation.split(",")]
    if args.min_gap is not None:
        overrides["min_gap"] = args.min_gap
    if args.group is not None:
        overrides["group_mode"] = args.group
    if args.no_slide:
        overrides["slide_to_edge"] = False
    if args.no_align:
        overrides["align_edges"] = False
    if args.thickness is not None:
        overrides["sheet_thickness"] = args.thickness

    if overrides:
        profile = profile.with_overrides(**overrides)

    run(dxf_path, width, height, args.thickness, unit,
        trials=args.trials, seed=args.seed, budget=args.budget,
        skip_unnumbered=not args.include_unnumbered,
        layers=layers, exclude_layers=exclude_layers,
        profile=profile,
        confirm_sheet_count=not args.no_confirm,
        report_only=args.report_only,
        special_size=parse_special_size(args.special_size),
        quick=args.quick)


if __name__ == "__main__":
    main()
