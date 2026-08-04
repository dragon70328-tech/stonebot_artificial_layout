"""DXF 自动排板系统 - 主入口（支持 CLI 参数 + 交互模式）"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROJECT_ROOT = Path(__file__).resolve().parent

from src.units import UnitSystem, UNIT_LABELS, convert_to_mm
from src.dxf_reader import read_dxf
from src.numbering import assign_numbers
from src.nesting import nest_parts, validate_nesting
from src.dxf_writer import write_nested_dxf
from src.models import NestingResult


def parse_args():
    p = argparse.ArgumentParser(description="DXF 自动排板系统")
    p.add_argument("dxf", nargs="?", help="规格板 DXF 文件路径")
    p.add_argument("width", nargs="?", type=float, help="大板长度 X (mm)")
    p.add_argument("height", nargs="?", type=float, help="大板宽度 Y (mm)")
    p.add_argument("thickness", nargs="?", type=float, default=20.0, help="大板厚度 (mm，默认 20)")
    p.add_argument("--trials", type=int, default=1, help="独立试验次数（不同种子），默认 1")
    p.add_argument("--seed", type=int, default=0, help="随机种子基数，默认 0")
    p.add_argument("--budget", type=float, default=180.0, help="每次试验 LNS 搜索时间（秒），默认 180")
    p.add_argument("--imperial", action="store_true", help="英制模式（默认公制）")
    p.add_argument("--include-unnumbered", action="store_true",
                   help="包含无编号封闭图形（默认跳过）")
    p.add_argument("--layers", type=str, default=None,
                   help="指定读取图层，逗号分隔，默认自动检测")
    p.add_argument("--exclude-layers", type=str, default=None,
                   help="排除图层，逗号分隔")
    return p.parse_args()


def make_output_dir(dxf_path: str) -> Path:
    """按 时间戳+文件名 创建输出子目录"""
    stem = Path(dxf_path).stem
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = PROJECT_ROOT / "output" / f"{ts}_{stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def run(dxf_path: str, width: float, height: float, thickness: float,
        unit: UnitSystem, trials: int, seed: int, budget: float,
        skip_unnumbered: bool, layers: list | None,
        exclude_layers: list | None) -> None:
    """核心排板流程"""
    unit_label = UNIT_LABELS[unit]
    width_mm = convert_to_mm(width, unit)
    height_mm = convert_to_mm(height, unit)
    thickness_mm = convert_to_mm(thickness, unit)

    print(f"读取 DXF: {dxf_path}")
    parts_data, _doc = read_dxf(dxf_path,
                                 panel_layers=layers,
                                 exclude_layers=exclude_layers)
    if not parts_data:
        print("错误：未找到任何封闭图形。")
        sys.exit(1)

    parts = assign_numbers(parts_data, skip_unnumbered=skip_unnumbered)
    total_area = sum(p.area for p in parts)
    min_sheets = -(-int(total_area) // int(width_mm * height_mm))
    print(f"{len(parts)} 块零件，总面积 {total_area/1e6:.1f} mm²，理论最少 {min_sheets} 张板")

    print(f"排板中... ({width_mm:.0f}x{height_mm:.0f}, {trials} 次试验 x {budget:.0f}s LNS)")
    t0 = time.time()
    result = nest_parts(parts, width_mm, height_mm, thickness_mm,
                        unit=unit.value, improve_budget=budget,
                        trials=trials, seed=seed)
    elapsed = time.time() - t0

    errors = validate_nesting(result, width_mm, height_mm)
    status = "通过" if not errors else f"{len(errors)} 处违规"
    print(f"完成：{result.total_sheets} 张板，出材率 {result.yield_rate:.2f}%，"
          f"校验{status}，耗时 {elapsed:.1f}s")
    for e in errors[:5]:
        print(f"  ! {e}")

    # 输出
    out_dir = make_output_dir(dxf_path)
    stem = Path(dxf_path).stem
    suffix = f"{int(width_mm)}x{int(height_mm)}"
    out_dxf = str(out_dir / f"{stem}_nested_{suffix}.dxf")
    out_json = str(out_dir / f"{stem}_report_{suffix}.json")

    write_nested_dxf(result, out_dxf, unit_system=unit.value)

    report = {
        "sheet_dimensions": {"width": width_mm, "height": height_mm,
                             "thickness": thickness_mm, "unit": unit_label},
        "total_sheets": result.total_sheets,
        "total_parts": result.total_parts,
        "total_part_area": round(result.total_part_area, 1),
        "total_sheet_area": result.total_sheet_area,
        "yield_rate": round(result.yield_rate, 2),
        "theoretical_min_sheets": min_sheets,
        "elapsed_seconds": round(elapsed, 1),
        "validation_errors": len(errors),
        "parameters": {"trials": trials, "seed": seed, "budget_s": budget,
                        "skip_unnumbered": skip_unnumbered},
        "sheets": [{"index": s.index,
                    "part_count": len(s.parts),
                    "parts": [p.number for p in s.parts]}
                   for s in result.sheets],
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"DXF: {out_dxf}")
    print(f"报告: {out_json}")


def interactive():
    """交互模式（无参数时）"""
    print("请选择单位制：")
    print("  [1] 公制 (mm)")
    print("  [2] 英制 (inch)")
    choice = input("请输入选项 (1/2): ").strip()
    unit = UnitSystem.IMPERIAL if choice == "2" else UnitSystem.METRIC

    try:
        width = float(input(f"大板长度 X ({UNIT_LABELS[unit]}): ").strip())
        height = float(input(f"大板宽度 Y ({UNIT_LABELS[unit]}): ").strip())
        thickness = float(input(f"大板厚度 ({UNIT_LABELS[unit]}): ").strip() or "20")
    except ValueError:
        print("错误：请输入有效数字。")
        sys.exit(1)

    dxf_path = input("规格板 DXF 文件路径: ").strip()
    dxf_path = os.path.expanduser(dxf_path)
    if not os.path.exists(dxf_path):
        print(f"错误：文件不存在 - {dxf_path}")
        sys.exit(1)

    run(dxf_path, width, height, thickness, unit,
        trials=1, seed=0, budget=180.0,
        skip_unnumbered=True, layers=None, exclude_layers=None)


def main():
    args = parse_args()
    if not args.dxf:
        interactive()
        return

    dxf_path = os.path.expanduser(args.dxf)
    if not os.path.exists(dxf_path):
        print(f"错误：文件不存在 - {dxf_path}")
        sys.exit(1)
    if args.width is None or args.height is None:
        print("错误：CLI 模式需要指定大板尺寸，如: python main.py input.dxf 3200 1800 20")
        sys.exit(1)

    layers = args.layers.split(",") if args.layers else None
    exclude_layers = args.exclude_layers.split(",") if args.exclude_layers else None
    unit = UnitSystem.IMPERIAL if args.imperial else UnitSystem.METRIC

    run(dxf_path, args.width, args.height, args.thickness, unit,
        trials=args.trials, seed=args.seed, budget=args.budget,
        skip_unnumbered=not args.include_unnumbered,
        layers=layers, exclude_layers=exclude_layers)


if __name__ == "__main__":
    main()
