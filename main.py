"""DXF 自动排板系统 - 主入口"""

import sys
import os
import json
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

from src.units import UnitSystem, UNIT_LABELS, convert_to_mm
from src.dxf_reader import read_dxf
from src.numbering import assign_numbers
from src.nesting import nest_parts, validate_nesting
from src.dxf_writer import write_nested_dxf
from src.models import NestingResult


def print_report(result: NestingResult,
                 sheet_width: float,
                 sheet_height: float,
                 sheet_thickness: float,
                 unit_label: str) -> None:
    """输出出材率报告"""
    print("=" * 50)
    print("         DXF 自动排板系统 - 出材率报告")
    print("=" * 50)
    print(f"大板尺寸：{sheet_width:.1f} x {sheet_height:.1f} x {sheet_thickness:.1f} {unit_label}")
    print(f"单位制：{'公制 (mm)' if result.unit == 'metric' else '英制 (inch)'}")
    print(f"使用大板数量：{result.total_sheets} 张")
    print(f"规格板总数：{result.total_parts} 块")
    print()

    for sheet in result.sheets:
        part_numbers = [p.number for p in sheet.parts]
        print(f"  Sheet {sheet.index}: {', '.join(part_numbers)}")
        print(f"    规格板数量: {len(sheet.parts)}, "
              f"使用面积: {sheet.used_area:.1f} {unit_label}²")

    print()
    print(f"所有规格板总面积：{result.total_part_area:.1f} {unit_label}²")
    print(f"大板总面积：{result.total_sheet_area:.1f} {unit_label}²")
    print(f"出材率：{result.yield_rate:.1f}%")
    print("=" * 50)


def export_json(result: NestingResult,
                sheet_width: float,
                sheet_height: float,
                sheet_thickness: float,
                unit_label: str,
                output_path: str) -> None:
    """导出 JSON 报告"""
    data = {
        "sheet_dimensions": {
            "width": sheet_width,
            "height": sheet_height,
            "thickness": sheet_thickness,
            "unit": unit_label,
        },
        "unit_system": result.unit,
        "total_sheets": result.total_sheets,
        "total_parts": result.total_parts,
        "total_part_area": result.total_part_area,
        "total_sheet_area": result.total_sheet_area,
        "yield_rate": round(result.yield_rate, 2),
        "sheets": []
    }

    for sheet in result.sheets:
        sheet_data = {
            "index": sheet.index,
            "parts": [p.number for p in sheet.parts],
            "part_count": len(sheet.parts),
            "used_area": sheet.used_area,
        }
        data["sheets"].append(sheet_data)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\nJSON 报告已保存至：{output_path}")


def main():
    """主流程"""
    # 1. 选择单位制
    print("请选择单位制：")
    print("  [1] 公制 (mm)")
    print("  [2] 英制 (inch)")
    choice = input("请输入选项 (1/2): ").strip()

    if choice == "2":
        unit = UnitSystem.IMPERIAL
    else:
        unit = UnitSystem.METRIC

    unit_label = UNIT_LABELS[unit]
    print(f"已选择：{unit_label}\n")

    # 2. 输入大板尺寸
    try:
        width = float(input(f"请输入大板长度 X ({unit_label}): ").strip())
        height = float(input(f"请输入大板宽度 Y ({unit_label}): ").strip())
        thickness = float(input(f"请输入大板厚度 ({unit_label}): ").strip())
    except ValueError:
        print("错误：请输入有效的数字。")
        sys.exit(1)

    # 内部统一使用 mm 计算
    width_mm = convert_to_mm(width, unit)
    height_mm = convert_to_mm(height, unit)
    thickness_mm = convert_to_mm(thickness, unit)

    # 3. 输入 DXF 文件路径
    dxf_path = input("\n请输入规格板 DXF 文件路径: ").strip()
    dxf_path = os.path.expanduser(dxf_path)

    if not os.path.exists(dxf_path):
        print(f"错误：文件不存在 - {dxf_path}")
        sys.exit(1)

    # 4. 读取 DXF
    print(f"\n正在读取 DXF 文件：{dxf_path} ...")
    parts_data, _doc = read_dxf(dxf_path)

    if not parts_data:
        print("错误：未在 DXF 中找到任何封闭图形。")
        sys.exit(1)

    print(f"找到 {len(parts_data)} 个规格板。")

    # 5. 编号
    parts = assign_numbers(parts_data, skip_unnumbered=True)
    for p in parts:
        print(f"  {p.number}: 面积 {p.area:.1f} {unit_label}²")

    # 6. 排板
    print(f"\n开始排板（大板 {width_mm:.1f} x {height_mm:.1f} {unit_label}）...")
    t0 = time.time()
    result = nest_parts(parts, width_mm, height_mm, thickness_mm,
                        unit=unit.value)
    print(f"排板耗时：{time.time() - t0:.1f} 秒")

    # 6.1 精确校验：边界 + 重叠
    errors = validate_nesting(result, width_mm, height_mm)
    if errors:
        print("警告：排板结果校验未通过：")
        for e in errors:
            print(f"  - {e}")
    else:
        print("校验通过：无越界、无重叠。")

    # 7. 输出报告
    print_report(result, width, height, thickness, unit_label)

    # 8. 写回 DXF
    input_path = Path(dxf_path)
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    sheet_suffix = f"{int(width)}x{int(height)}"
    output_dxf = str(output_dir / f"{input_path.stem}_nested_{sheet_suffix}.dxf")

    print(f"\n正在写入排板结果 DXF：{output_dxf} ...")
    write_nested_dxf(result, output_dxf, unit_system=unit.value)
    print(f"排板结果 DXF 已保存至：{output_dxf}")

    # 9. 导出 JSON 报告
    output_json = str(output_dir / f"{input_path.stem}_report_{sheet_suffix}.json")
    export_json(result, width, height, thickness, unit_label, output_json)

    print("\n排板完成！")


if __name__ == "__main__":
    main()
