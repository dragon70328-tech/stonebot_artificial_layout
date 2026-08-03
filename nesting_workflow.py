# -*- coding: utf-8 -*-
"""DXF 自动排板系统 - 交互式排板流程

用法：用户说"进入排板流程"，脚本以提问方式收集参数，
      两阶段执行（检查 → 排板）。
"""

import sys, os, json, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.units import UnitSystem, UNIT_LABELS, convert_to_mm
from src.dxf_reader import read_dxf
from src.numbering import assign_numbers
from src.nesting import nest_parts, validate_nesting
from src.dxf_writer import write_nested_dxf, write_inplace_check_dxf
from src.models import NestingResult


def ask(question, default=None):
    """带默认值的提问"""
    if default is not None:
        prompt = "{} [{}]: ".format(question, default)
    else:
        prompt = "{}: ".format(question)
    ans = input(prompt).strip()
    if not ans and default is not None:
        return default
    return ans


def ask_yes_no(question, default="y"):
    ans = ask(question + " (y/n)", default)
    return ans.lower().startswith("y")


def phase1_check():
    """阶段一：读取 DXF，识别面板，匹配编号，输出原位检查 DXF"""
    print()
    print("=" * 60)
    print("  阶段一：DXF 读取与编号检查")
    print("=" * 60)
    print()

    # 1. DXF 路径
    dxf_path = ask("请输入 DXF 文件完整路径")
    dxf_path = os.path.expanduser(dxf_path)
    if not os.path.exists(dxf_path):
        print("错误：文件不存在 - {}".format(dxf_path))
        return None, None

    # 2. 编号图层
    number_layer = ask("编号在哪个图层？", "编号")

    # 3. 规格板图层
    panel_layers_str = ask("规格板在哪些图层？（多个用逗号分隔，回车=全部图层）", "")
    if panel_layers_str:
        panel_layers = [s.strip() for s in panel_layers_str.split(",") if s.strip()]
    else:
        panel_layers = None

    # 4. 排除图层
    exclude_str = ask("需要排除哪些图层？（多个用逗号分隔，回车=不排除）", "")
    if exclude_str:
        exclude_layers = [s.strip() for s in exclude_str.split(",") if s.strip()]
    else:
        exclude_layers = None

    # 5. 特殊线型
    extra_lt_str = ask("需要额外排除的线型？（多个用逗号分隔，回车=使用默认排除集）", "")
    if extra_lt_str:
        from src.dxf_reader import DEFAULT_EXCLUDE_LINETYPES
        extra = set(s.strip().upper() for s in extra_lt_str.split(",") if s.strip())
        exclude_linetypes = DEFAULT_EXCLUDE_LINETYPES | extra
    else:
        exclude_linetypes = None

    # 6. 其他要求
    print()
    other = ask("还有其他要求吗？（直接回车=没有）", "")
    if other:
        print("已记录：{}".format(other))

    # 确认
    print()
    print("--- 参数确认 ---")
    print("  DXF: {}".format(dxf_path))
    print("  编号图层: {}".format(number_layer))
    print("  规格板图层: {}".format(panel_layers or "全部"))
    print("  排除图层: {}".format(exclude_layers or "无"))
    print("  额外排除线型: {}".format(extra_lt_str or "默认"))
    print("  其他要求: {}".format(other or "无"))
    print()

    if not ask_yes_no("确认以上参数，开始读取 DXF？", "y"):
        print("已取消。")
        return None, None

    # 读取 DXF
    print("\n正在读取 DXF...")
    try:
        parts_data, doc = read_dxf(
            dxf_path,
            panel_layers=panel_layers,
            exclude_layers=exclude_layers,
            exclude_linetypes=exclude_linetypes,
            number_layer=number_layer,
        )
    except Exception as e:
        print("读取失败：{}".format(e))
        return None, None

    if not parts_data:
        print("错误：未找到任何规格板！请检查图层和线型过滤参数。")
        return None, None

    print("找到 {} 个规格板。".format(len(parts_data)))

    # 分配编号
    parts = assign_numbers(parts_data)
    total_holes = sum(len(p.holes) for p in parts)
    parts_with_holes = sum(1 for p in parts if p.holes)
    print("带孔面板: {} 个，孔洞总数: {} 个".format(parts_with_holes, total_holes))

    # 输出原位检查 DXF
    input_path = Path(dxf_path)
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    check_path = str(output_dir / "{}_numbered_\u539f\u4f4d.dxf".format(input_path.stem))

    print("\n正在生成原位编号检查 DXF...")
    try:
        write_inplace_check_dxf(parts, dxf_path, check_path, unit_system="metric")
        print("\n原位检查 DXF 已保存：{}".format(check_path))
    except Exception as e:
        print("写入失败：{}".format(e))
        return None, None

    # 显示面板列表
    print()
    print("--- 面板列表 ---")
    for p in parts:
        hole_info = " ({}孔)".format(len(p.holes)) if p.holes else ""
        print("  {}: {} mm{}".format(p.number, int(p.area), hole_info))

    print()
    print("请在 CAD 中打开检查 DXF，核对面板识别和编号是否正确。")

    return parts, dxf_path


def phase2_nest(parts, source_dxf_path):
    """阶段二：输入大板尺寸，执行排板"""
    print()
    print("=" * 60)
    print("  阶段二：排板")
    print("=" * 60)
    print()

    # 1. 单位制
    unit_choice = ask("单位制：[1] 公制 mm  [2] 英制 inch", "1")
    if unit_choice == "2":
        unit = UnitSystem.IMPERIAL
    else:
        unit = UnitSystem.METRIC
    unit_label = UNIT_LABELS[unit]

    # 2. 大板尺寸
    try:
        width = float(ask("大板长度 X ({})".format(unit_label)))
        height = float(ask("大板宽度 Y ({})".format(unit_label)))
        thickness = float(ask("大板厚度 ({})".format(unit_label)))
    except ValueError:
        print("错误：请输入有效数字。")
        return

    width_mm = convert_to_mm(width, unit)
    height_mm = convert_to_mm(height, unit)

    # 3. 排板预算
    budget_str = ask("LNS 优化时间（秒，回车=180）", "180")
    try:
        improve_budget = float(budget_str)
    except ValueError:
        improve_budget = 180.0

    # 4. 其他要求
    print()
    other = ask("还有其他要求吗？（直接回车=没有）", "")

    # 确认
    print()
    print("--- 排板参数确认 ---")
    print("  大板: {} x {} x {} {}".format(width, height, thickness, unit_label))
    print("  面板数: {}".format(len(parts)))
    print("  LNS 预算: {} 秒".format(improve_budget))
    if other:
        print("  其他要求: {}".format(other))
    print()

    if not ask_yes_no("确认以上参数，开始排板？", "y"):
        print("已取消。")
        return

    print("\n正在排板（可能需要 3-5 分钟）...")
    t0 = time.time()

    def progress_cb(current, total, best_sheets):
        pass  # 静默，避免刷屏

    result = nest_parts(
        parts, width_mm, height_mm, thickness,
        unit=unit.value,
        improve_budget=improve_budget,
        progress=progress_cb,
    )
    elapsed = time.time() - t0
    print("排板完成，耗时 {:.1f} 秒".format(elapsed))

    # 校验
    errors = validate_nesting(result, width_mm, height_mm)
    if errors:
        print("\n警告：校验发现 {} 个问题：".format(len(errors)))
        for e in errors:
            print("  - {}".format(e))
    else:
        print("校验通过：无越界、无重叠。")

    # 报告
    print()
    print("=" * 60)
    print("  出材率报告")
    print("=" * 60)
    print("使用大板: {} 张".format(result.total_sheets))
    print("规格板总数: {} 块".format(result.total_parts))
    print("面板净面积: {:.1f} {}^2".format(
        result.total_part_area, unit_label))
    print("大板总面积: {:.1f} {}^2".format(
        result.total_sheet_area, unit_label))
    print("出材率: {:.1f}%".format(result.yield_rate))

    # 输出
    input_path = Path(source_dxf_path)
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    sheet_suffix = "{}x{}".format(int(width), int(height))
    output_dxf = str(output_dir / "{}_nested_{}.dxf".format(
        input_path.stem, sheet_suffix))

    print("\n正在写入排板 DXF...")
    write_nested_dxf(result, output_dxf, unit_system=unit.value)
    print("排板 DXF: {}".format(output_dxf))

    # JSON 报告
    output_json = str(output_dir / "{}_report_{}.json".format(
        input_path.stem, sheet_suffix))
    report = {
        "sheet_dimensions": {
            "width": width, "height": height, "thickness": thickness,
            "unit": unit_label
        },
        "unit_system": result.unit,
        "total_sheets": result.total_sheets,
        "total_parts": result.total_parts,
        "total_part_area": result.total_part_area,
        "total_sheet_area": result.total_sheet_area,
        "yield_rate": round(result.yield_rate, 2),
        "nesting_time_s": round(elapsed, 1),
        "sheets": [
            {
                "index": s.index,
                "parts": [p.number for p in s.parts],
                "part_count": len(s.parts),
                "used_area": s.used_area,
            }
            for s in result.sheets
        ]
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("JSON 报告: {}".format(output_json))

    print("\n排板完成！")


def main():
    print()
    print("=" * 60)
    print("  人造石台面 DXF 自动排板系统")
    print("=" * 60)

    # 阶段一
    parts, dxf_path = phase1_check()
    if parts is None:
        return

    # 等待用户确认
    while True:
        print()
        ans = ask("核对无误，进入排板阶段？(y=继续 / n=退出 / r=重跑检查)", "y")
        if ans.lower() == "y":
            break
        elif ans.lower() == "r":
            parts, dxf_path = phase1_check()
            if parts is None:
                return
        else:
            print("已退出。")
            return

    # 阶段二
    phase2_nest(parts, dxf_path)


if __name__ == "__main__":
    main()
