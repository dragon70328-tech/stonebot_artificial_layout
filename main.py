#!python
# -*- coding: utf-8 -*-
"""DXF 自动排板系统 - 主入口（支持 CLI 参数 + 交互模式）"""

import argparse
import json
import math
import os
import sys
import time
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROJECT_ROOT = Path(__file__).resolve().parent

from src.units import UnitSystem, UNIT_LABELS, convert_to_mm
from src.dxf_reader import read_dxf
from src.numbering import assign_numbers
from src.dxf_writer import write_nested_dxf, write_numbered_parts_dxf
from src.list_nesting import run_list_nesting
from src.constraints import (
    NestingProfile, PROFILES, PROFILE_HELP,
    PROFILE_MIN_SHEETS,
    PROCESSING_CLASS_BRIDGE,
    PROCESSING_CLASS_WATERJET_LASER,
    PROCESSING_CLASS_HELP,
    STANDARD_SHEET_SIZES, STANDARD_THICKNESSES,
    get_sheet_size, get_sheet_size_by_index,
)
from src.project_config import (
    ProjectConfig,
    QUALITY_PRESETS,
    apply_quality_to_optimizer,
)
from src.drawing_profile import (
    analyze_drawing,
    load_profiles,
    rank_profiles,
    read_dxf_with_profile,
)
from src.case_library import load_cases, load_case_profile, match_case
from src.recognized_contract import parts_data_to_recognized_drawing
from src.workflow import (
    PreparedDrawing,
    GroupNestingOutcome,
    apply_material_grouping,
    build_check_payload,
    build_report,
    combine_nesting_results,
    material_group_for_number,
    nest_groups,
    nest_special_parts,
    postprocess_result,
    prepare_drawing,
    resolve_drawing_profile,
    validate_mixed_nesting,
    _split_normal_parts_by_group,
)
from src.nesting_reviewer import find_reductions
from src.postprocess import estimate_kerf_material, validate_kerf_material
from src.template_nesting import match_template, nest_from_template
from src.artifact_store import ArtifactStore
from src.workflow_session import WorkflowSession, WorkflowStage
from src.agent_cli import run_agent_mode
from src.cli_audit import run_audit, split_issue_ids
from src.cli_paths import make_output_dir


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
    p.add_argument("--quality", type=str, default=None,
                   choices=["fast", "balanced", "best"],
                   help="排板质量档位：fast=快速预览，balanced=折中，best=高质量")
    p.add_argument("--quick", action="store_true",
                   help="快速模式：使用 4 组贪心配置，适合先看板数")
    p.add_argument("--no-beam", action="store_true",
                   help="关闭逐板最佳填充束搜索，只用多轮贪心配置")
    p.add_argument("--beam-width", type=int, default=None,
                   help="束搜索每层保留的布局数（默认 full=1000 / quick=400）")
    p.add_argument("--imperial", action="store_true",
                   help="英制模式（默认公制）")

    # 约束模板
    p.add_argument("--profile", type=str, default=None,
                   choices=list(PROFILES.keys()),
                   help="约束模板：" + " / ".join(
                       f"{k}={v}" for k, v in PROFILE_HELP.items()))
    p.add_argument("--drawing-profile", type=str, default=None,
                   help="\u5f3a\u5236\u6307\u5b9a\u8bfb\u56fe\u753b\u50cf\u540d\u79f0\uff0c\u5982 canada3l")
    p.add_argument("--config", type=str, default=None,
                   help="项目 JSON 配置文件路径（与 CLI 参数组合时 CLI 优先）")
    p.add_argument("--list-profiles", action="store_true",
                   help="列出可用约束模板后退出")
    p.add_argument("--process", type=str, default=None,
                   choices=["bridge", "waterjet", "laser"],
                   help="加工方式：bridge=桥切机，waterjet=水刀，laser=激光")
    p.add_argument("--agent", action="store_true",
                   help="使用本地 agent loop 模式（带三处确认门）")
    p.add_argument("--agent-provider", type=str, default="queue",
                    choices=["queue", "llm"],
                    help="agent 计划来源：queue=确定性计划，llm=OpenAI-compatible LLM")

    # 约束覆盖（可与 --profile 组合，也可独立使用）
    p.add_argument("--rotation", type=str, default=None,
                   help="允许的旋转角度，逗号分隔，如 0,90")
    p.add_argument("--free-rotation", action="store_true",
                   help="允许任意角度旋转（水刀/激光密集排板）")
    p.add_argument("--no-rotation", action="store_true",
                   help="禁止所有旋转，规格板保持原始角度")
    p.add_argument("--pairing", action="store_true",
                   help="启用相同形状 180° 共边配对预排（可选增强）")
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
    p.add_argument("--no-auto-review", action="store_true",
                   help="排板后不自动回看跨板减板机会")
    p.add_argument("--use-template", action="store_true",
                   help="允许复用已验证模板布局（默认关闭，只用算法排板）")
    p.add_argument("--no-compress", action="store_true",
                   help="关闭多板联合重排压缩（默认开启：把 k 张最低填充板"
                        "联合重排进 k-1 张，是压缩板数的显式通道）")
    p.add_argument("--cold-start", action="store_true",
                   help="冷启动：忽略模板/已验证案例/审图缓存，仅用原始 DXF + 配置")
    p.add_argument("--review-budget", type=float, default=0.0,
                   help="自动回看的总时间上限（秒）。0（默认）= 不用墙钟上限，"
                        "只受确定性上限约束（reductions/donor 次数/搜索节点数），"
                        "因此同参数同输入必然同输出；给正数则可能被截断而不可复现")
    p.add_argument("--report-only", action="store_true",
                   help="仅完成排板并报告大板使用量，不执行后处理和输出文件")
    p.add_argument("--check-only", action="store_true",
                   help="排板与后处理完成后先输出检查报告，不生成最终 DXF")
    p.add_argument("--special-size", type=str, default=None,
                   help="特殊面板排板尺寸，如 3225x1625")
    p.add_argument("--sizes", type=str, default=None,
                   help="清单排板可用大板尺寸，逗号分隔，如 2400x1200,2500x1400")
    p.add_argument("--output-dxf", type=str, default=None,
                   help="清单排板结果输出 DXF；相同排板只画一张并标注数量")
    p.add_argument("--no-dxf", action="store_true",
                   help="清单排板时不生成 DXF 文件")

    # 标准规格快捷选择
    p.add_argument("--list-sizes", action="store_true",
                   help="列出标准大板尺寸后退出")
    p.add_argument("--size", type=str, default=None,
                   help="选择标准大板尺寸，如 3200x1800 或序号 1")

    # DXF 读取
    p.add_argument("--audit", action="store_true",
                   help="仅执行读图审图并输出问题报告，不进入排板")
    p.add_argument("--list-nest", action="store_true",
                   help="按 Excel/PDF 规格尺寸与数量清单排板，输出文字结论")
    p.add_argument("--kerf", type=float, default=0.0,
                   help="锯缝宽度 mm（清单排板或桥切刀路损耗，零件为净尺寸）")
    p.add_argument("--oversize", type=float, default=0.0,
                   help="大板让尺 mm（清单排板）。清单排板的口径是"
                        "\"可利用尺寸包括让尺\"：可用尺寸=标称+让尺；"
                        "与 DXF 排板默认（可利用尺寸不含让尺）不同")
    p.add_argument("--kerf-reserve", type=float, default=0.0,
                   help="[兼容别名] 等同于 --sheet-oversize（大板让尺 mm）；"
                        "两个参数同时给出时以 --sheet-oversize 为准。"
                        "默认 0：让尺必须在口径里说明，不再隐式默认 30")
    p.add_argument("--sheet-oversize", type=str, default=None,
                   help="大板让尺 mm，如 30 或 30,30（每维度总量 +30，不是"
                        "每边 +30）。让尺是标称之外的实物余量，只用于消化"
                        "刀缝：默认不放大零件可利用尺寸，也不参与 DXF 外框")
    p.add_argument("--usable-includes-oversize", action="store_true",
                   help="声明本次排板大板可利用尺寸包括让尺（零件可用 "
                        "标称+让尺）；默认不包括，即可利用尺寸=标称")
    p.add_argument("--material", type=str, default=None,
                   help="只排指定材料组（列头材料号，如 ST-6；多个用逗号分隔）。"
                        "不同材料绝不共板，分单排板时用它过滤；缺省排全部零件")
    p.add_argument("--previous-state", type=str, default=None,
                   help="上次审图生成的 review_state.json，用于修正重传后的复检")
    p.add_argument("--accept-issue", type=str, default=None,
                   help="接受的问题 ID，逗号分隔，例如 1,3")
    p.add_argument("--ignore-issue", type=str, default=None,
                   help="忽略的问题 ID，逗号分隔")
    p.add_argument("--mark-fixed", type=str, default=None,
                   help="标记为已修复的问题 ID，逗号分隔")
    p.add_argument("--include-unnumbered", action="store_true",
                   help="包含无编号封闭图形（默认跳过）")
    p.add_argument("--layers", type=str, default=None,
                   help="指定读取图层，逗号分隔，默认自动检测")
    p.add_argument("--exclude-layers", type=str, default=None,
                   help="排除图层，逗号分隔")

    return p.parse_args()


def resolve_requested_drawing_profile(name: str | None):
    """Load an explicit drawing profile by name, bypassing auto ranking."""
    if not name:
        return None
    profiles = load_profiles(PROJECT_ROOT / "drawing_profiles")
    requested = name.casefold()
    for profile in profiles:
        if profile.name.casefold() == requested:
            return profile
    available = ", ".join(sorted(profile.name for profile in profiles))
    raise ValueError(
        f"unknown drawing profile {name!r}; available: {available}"
    )


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


@dataclass(frozen=True)
class SheetCaliber:
    """大板口径的三层：标称 / 让尺 / 可利用尺寸（2026-10-13 用户确认）。

    - 标称：用户口中"大板尺寸"（如 3200x1600）；
    - 让尺：标称之外的实物余量，**每维度总量加一次**（30 = 3230，不是每边
      +30 的 3260），存在意义只有一条——消化切割刀缝；
    - 可利用尺寸：零件实际能占的范围，**默认等于标称**（不含让尺）；仅当
      用户明确声明"本次排板大板可利用尺寸包括让尺"时才等于标称 + 让尺。
    """

    nominal_w_mm: float
    nominal_h_mm: float
    oversize_w_mm: float
    oversize_h_mm: float
    usable_w_mm: float
    usable_h_mm: float
    usable_includes_oversize: bool = False

    @property
    def physical_w_mm(self) -> float:
        """实物尺寸 = 标称 + 让尺。"""
        return self.nominal_w_mm + self.oversize_w_mm

    @property
    def physical_h_mm(self) -> float:
        return self.nominal_h_mm + self.oversize_h_mm

    @property
    def allowance_w_mm(self) -> float:
        """刀缝校验允许的额外长度 = 实物 − 可利用（默认即让尺）。"""
        return self.physical_w_mm - self.usable_w_mm

    @property
    def allowance_h_mm(self) -> float:
        return self.physical_h_mm - self.usable_h_mm


def resolve_sheet_caliber(
    width_mm: float,
    height_mm: float,
    sheet_oversize: tuple[float, float] | None = None,
    kerf_reserve_mm: float = 0.0,
    usable_includes_oversize: bool = False,
) -> SheetCaliber:
    """把 CLI 参数解析成显式的大板口径。

    排板空间用**可利用尺寸**；刀缝材料校验允许**实物尺寸**（= 可利用 +
    allowance），因此让尺只计一次。历史缺陷：让尺既被塞进排板空间、又被
    加进 allowance（3230 当可利用 + 再放宽 30 = 3260）。

    ``--kerf-reserve`` 是 ``--sheet-oversize`` 的兼容别名（同一个"让尺"
    概念），两者都给时以 ``--sheet-oversize`` 为准，避免重复计入。
    """
    if sheet_oversize is None:
        oversize_w = oversize_h = float(kerf_reserve_mm or 0.0)
    else:
        oversize_w, oversize_h = (
            float(sheet_oversize[0]),
            float(sheet_oversize[1]),
        )
    if usable_includes_oversize:
        usable_w = width_mm + oversize_w
        usable_h = height_mm + oversize_h
    else:
        usable_w, usable_h = width_mm, height_mm
    return SheetCaliber(
        nominal_w_mm=width_mm,
        nominal_h_mm=height_mm,
        oversize_w_mm=oversize_w,
        oversize_h_mm=oversize_h,
        usable_w_mm=usable_w,
        usable_h_mm=usable_h,
        usable_includes_oversize=usable_includes_oversize,
    )


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


def resolve_processing_class(value: str | None) -> str | None:
    """把 CLI 加工方式映射为 NestingProfile.processing_class。"""
    if value is None:
        return None
    if value == "bridge":
        return PROCESSING_CLASS_BRIDGE
    return PROCESSING_CLASS_WATERJET_LASER


def default_profile_for_process(value: str | None) -> NestingProfile | None:
    """未显式选择模板时，按加工方式返回默认模板。"""
    if value == "waterjet":
        return PROFILES["waterjet"]
    if value == "laser":
        return PROFILES["laser"]
    return None


def parse_sizes_value(value: str | None) -> list[tuple[float, float]]:
    if not value:
        return []
    sizes: list[tuple[float, float]] = []
    for part in value.split(","):
        text = part.strip().lower().replace("×", "x").replace("*", "x")
        if not text:
            continue
        match = re.match(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)", text)
        if not match:
            print(f"错误：无法识别大板尺寸 '{part}'，示例：2400x1200,2500x1400")
            sys.exit(1)
        sizes.append((float(match.group(1)), float(match.group(2))))
    return sizes

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


def parse_sheet_oversize(value: str | None) -> tuple[float, float]:
    """解析桥切大板长/宽让尺；单值表示长宽相同，双值按长,宽。"""
    if not value:
        return (0.0, 0.0)
    text = value.strip().lower().replace("×", "x").replace("*", "x")
    tokens = [part.strip() for part in re.split(r"[,x]", text) if part.strip()]
    if len(tokens) not in (1, 2):
        print(f"错误：大板让尺需为单值或长,宽，示例：30 或 30,30")
        sys.exit(1)
    try:
        nums = [float(token) for token in tokens]
    except ValueError:
        print(f"错误：无法识别大板让尺 '{value}'，示例：30 或 30,30")
        sys.exit(1)
    if any(num < 0 for num in nums):
        print(f"错误：大板让尺不能为负数 '{value}'")
        sys.exit(1)
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (nums[0], nums[1])


def check_only_exit_code(outcome: dict | None) -> int:
    """Return the process exit code for a ``--check-only`` run.

    Geometry errors are fatal (exit 2); manufacturability warnings alone
    should not block automation and therefore exit 0.
    """
    if outcome and outcome.get("geometry_errors"):
        return 2
    return 0


def run_with_config(dxf_path: str, config, unit: UnitSystem = UnitSystem.METRIC,
                    drawing_profile=None, confirm_sheet_count: bool = True,
                    report_only: bool = False, check_only: bool = False):
    """Run a nesting job from a ProjectConfig while keeping engine APIs stable."""
    sheet = config.sheet
    reader = config.reader
    optimizer = config.optimizer
    return run(
        dxf_path,
        sheet.width,
        sheet.height,
        sheet.thickness,
        unit,
        trials=optimizer.trials,
        seed=optimizer.seed,
        budget=optimizer.budget_seconds,
        skip_unnumbered=reader.skip_unnumbered,
        layers=reader.layers,
        exclude_layers=reader.exclude_layers,
        drawing_profile=drawing_profile,
        profile=config.to_nesting_profile(),
        confirm_sheet_count=confirm_sheet_count,
        report_only=report_only,
        special_size=sheet.special_size,
        check_only=check_only,
        quick=optimizer.quick,
        pairing=optimizer.pairing,
        reader_options=reader.to_read_options(),
        kerf_mm=sheet.kerf_mm,
        sheet_oversize=sheet.sheet_oversize,
        usable_includes_oversize=sheet.usable_includes_oversize,
    )


def apply_cli_overrides_to_project_config(config, args) -> None:
    """Apply CLI flags on top of a loaded ProjectConfig."""
    if args.thickness is not None:
        config.sheet.thickness = args.thickness
        config.profile.sheet_thickness = args.thickness
    if args.special_size:
        special = parse_special_size(args.special_size)
        config.sheet.special_width = special[0]
        config.sheet.special_height = special[1]
    if getattr(args, "kerf", None):
        config.sheet.kerf_mm = float(args.kerf)
    if getattr(args, "sheet_oversize", None):
        oversize = parse_sheet_oversize(args.sheet_oversize)
        config.sheet.sheet_oversize_x = oversize[0]
        config.sheet.sheet_oversize_y = oversize[1]

    processing_class = resolve_processing_class(args.process)
    if processing_class is not None:
        config.profile.processing_class = processing_class
    if args.rotation is not None:
        config.profile.rotation = [
            int(x.strip()) for x in args.rotation.split(",")
        ]
    if args.free_rotation:
        config.profile.arbitrary_rotation = True
    if args.no_rotation:
        config.profile.rotation = [0]
        config.profile.arbitrary_rotation = False
    if args.min_gap is not None:
        config.profile.min_gap = args.min_gap
    if args.group is not None:
        config.profile.group_mode = args.group
    if args.no_slide:
        config.profile.slide_to_edge = False
    if args.no_align:
        config.profile.align_edges = False

    quality = getattr(args, "quality", None)
    if quality:
        apply_quality_to_optimizer(config.optimizer, quality)

    if args.trials != 1:
        config.optimizer.trials = args.trials
    if args.seed != 0:
        config.optimizer.seed = args.seed
    if args.budget != 180.0:
        config.optimizer.budget_seconds = args.budget
    if args.quick:
        config.optimizer.quick = True
    if args.pairing:
        config.optimizer.pairing = True

    if args.include_unnumbered:
        config.reader.skip_unnumbered = False
    if args.layers:
        config.reader.layers = args.layers.split(",")
    if args.exclude_layers:
        config.reader.exclude_layers = args.exclude_layers.split(",")

# ═══════════════════════════════════════════════════════════════
#  核心排板流程
# ═══════════════════════════════════════════════════════════════

def _forced_closure_parts(recognized_drawing, threshold_mm: float = 5.0):
    """列出靠强制闭合才成形的轮廓（缺口 > 阈值）。

    图纸不规范时，画图人只画了几条边，未闭合的缺口只能用
    两端点直线补上。这种轮廓必须提示用户确认，不能静默当作规范图形。
    """
    forced = []
    for panel in getattr(recognized_drawing, "panels", []) or []:
        gap = float((panel.properties or {}).get("closure_gap", 0.0) or 0.0)
        if gap > threshold_mm:
            forced.append({
                "number": panel.number,
                "handle": panel.entity_handle,
                "closure_gap": round(gap, 1),
            })
    return forced


def parse_material_filter(value: str | None) -> list[str] | None:
    """解析 --material，如 ``"ST-6"`` / ``"ST-6,ST-7"``。"""
    if not value:
        return None
    codes = [code.strip() for code in value.split(",") if code.strip()]
    return codes or None


def _filter_parts_by_material(parts, wanted: list[str]):
    """只保留指定材料组的零件（分单排板）。

    不同材料绝不共板：分单排板时先在这里把零件过滤到目标材料组，再走原有
    排板引擎。材料组在整个图纸里都找不到时直接报错，避免静默排出空结果
    （留白比排错板更容易被忽略）。
    """
    wanted_codes = [code.strip().upper() for code in wanted if code.strip()]
    available: dict[str, int] = {}
    for part in parts:
        key = (part.material_group or "").upper()
        available[key] = available.get(key, 0) + 1

    missing = [code for code in wanted_codes if code not in available]
    if missing:
        print("错误：图纸里没有材料组 " + "、".join(missing)
              + "。可用材料组："
              + "、".join(
                  f"{key or '(无材料)'}({count} 件)"
                  for key, count in sorted(available.items())
              ))
        sys.exit(1)

    wanted_set = set(wanted_codes)
    kept = [
        part for part in parts
        if (part.material_group or "").upper() in wanted_set
    ]
    if not kept:
        print("错误：材料筛选后没有可排板零件。")
        sys.exit(1)

    skipped = len(parts) - len(kept)
    print(f"材料筛选：只排 {'/'.join(wanted_codes)} —— {len(kept)} 件"
          + (f"，跳过其他材料 {skipped} 件" if skipped else ""))
    filtered_groups = sorted(
        {part.material_group for part in kept if part.material_group}
    )
    return kept, (filtered_groups or [None])


def _rebuild_material_summary(result, previous_summary):
    """回看减板后按材料分组重算板数与出材率。"""
    previous = {
        item.get("material_group"): item
        for item in (previous_summary or [])
    }
    buckets: dict[str, list] = {}
    for sheet in result.sheets:
        labels = {part.material_group or "ALL" for part in sheet.parts}
        label = next(iter(labels)) if len(labels) == 1 else "MIXED"
        buckets.setdefault(label, []).append(sheet)

    ordered = [label for label in previous if label in buckets]
    ordered += [label for label in buckets if label not in previous]
    summary = []
    for label in ordered:
        sheets = buckets[label]
        part_area = sum(part.area for sheet in sheets for part in sheet.parts)
        sheet_area = sum(sheet.total_area for sheet in sheets)
        prev = previous.get(label, {})
        summary.append({
            "material_group": label,
            "part_count": sum(len(sheet.parts) for sheet in sheets),
            "sheets": len(sheets),
            "yield_rate": round(part_area / sheet_area * 100, 2) if sheet_area else 0.0,
            "theoretical_min_sheets": prev.get("theoretical_min_sheets", 0),
        })
    return summary


def _auto_reduce_sheets(result, profile, material_summary, manufacturing_metrics, *,
                        max_reductions: int = 5,
                         max_donor_attempts: int = 8,
                         max_donor_parts: int = 6,
                         max_nodes_per_donor: int = 3000,
                         time_budget_s: float = 0.0,
                         per_donor_time_s: float | None = None,
                         stats: dict | None = None):
    """后处理后回看排板结果，把能合并的板并掉。

    回看是增强项：失败、超时、校验不通过都不影响主流程，
    只是保留原排板。

    确定性（2026-10-14 起）：默认不设墙钟上限，只受 max_reductions /
    max_donor_attempts / max_donor_parts / max_nodes_per_donor 这些确定性上限约束；
    传正的 time_budget_s 会恢复旧的"时间截断"行为（结果可能不可复现）。

    上限取值的依据：donor 拆除搜索的代价 ≈ 零件数 × 目标板数 × 每件候选数，
    大单（50+ 张板）上单个 donor 就能吃掉几十秒。所以既要限"试几个 donor"
    （8），也要限"单个 donor 的节点数"（3000），并跳过零件数 > 6 的 donor
    ——需要腾空的板几乎都只有一两件，件数多的板几何上基本并不掉。
    """
    print(f"回看排板结果：{result.total_sheets} 张板，"
          f"寻找可合并的大板...")
    t_review = time.time()
    try:
        candidates, reduced = find_reductions(
            result,
            profile=profile,
            max_reductions=max_reductions,
            max_donor_attempts=max_donor_attempts,
            max_donor_parts=max_donor_parts,
            max_nodes_per_donor=max_nodes_per_donor,
            min_gap=profile.min_gap,
            time_budget_s=(time_budget_s if time_budget_s and time_budget_s > 0
                           else None),
            per_donor_time_s=per_donor_time_s,
        )
    except Exception as exc:  # noqa: BLE001 - 回看不能影响主流程
        print(f"回看跳过：{exc}")
        if stats is not None:
            stats["review_seconds"] = round(time.time() - t_review, 3)
            stats["review_error"] = str(exc)
        return [], result, material_summary, manufacturing_metrics

    if stats is not None:
        stats["review_seconds"] = round(time.time() - t_review, 3)
        stats["review_reduced_sheets"] = len(candidates)
        stats["review_deterministic"] = not (time_budget_s and time_budget_s > 0)
        stats["review_max_reductions"] = max_reductions
        stats["review_max_donor_attempts"] = max_donor_attempts
        stats["review_max_donor_parts"] = max_donor_parts
        stats["review_max_nodes_per_donor"] = max_nodes_per_donor

    if not candidates:
        print("回看完成：未发现可合并的大板。")
        return [], result, material_summary, manufacturing_metrics

    # 合并后重新压实并校验，不通过就回退到原排板
    try:
        new_metrics = postprocess_result(reduced, profile)
        errors = validate_mixed_nesting(reduced, min_gap=profile.min_gap)
    except Exception as exc:  # noqa: BLE001
        print(f"回看结果不可用（{exc}），保留原排板。")
        return [], result, material_summary, manufacturing_metrics
    if errors:
        print(f"回看结果校验未通过（{len(errors)} 处），保留原排板。")
        return [], result, material_summary, manufacturing_metrics

    removed = [candidate.removed_sheet_index for candidate in candidates]
    print(f"回看减板：共减少 {len(candidates)} 张板"
          f"（原 sheet {removed}），"
          f"{result.total_sheets} -> {reduced.total_sheets} 张，"
          f"出材率 {result.yield_rate:.2f}% -> {reduced.yield_rate:.2f}%")
    return (
        candidates,
        reduced,
        _rebuild_material_summary(reduced, material_summary),
        new_metrics,
    )


def run(dxf_path: str, width: float, height: float, thickness: float,
        unit: UnitSystem, trials: int, seed: int, budget: float,
        skip_unnumbered: bool, layers: list | None,
        exclude_layers: list | None,
        drawing_profile=None,
        profile: NestingProfile | None = None,
        confirm_sheet_count: bool = True,
        report_only: bool = False,
        special_size: tuple[float, float] | None = None,
        check_only: bool = False,
        quick: bool = False,
        pairing: bool = False,
        reader_options: dict | None = None,
        input_limits=None,
        kerf_mm: float = 0.0,
        kerf_reserve_mm: float = 0.0,
        sheet_oversize: tuple[float, float] | None = None,
        usable_includes_oversize: bool = False,
        output_root: Path | None = None,
        auto_review: bool = True,
        review_budget_s: float = 120.0,
        use_template: bool = False,
        cold_start: bool = False,
        beam: bool = True,
        beam_width: int | None = None,
        compress: bool = True,
        material_filter: list[str] | None = None) -> None:
    """核心排板流程

    ``use_template`` 默认 False：模板布局复用是显式选择，默认只跑算法，
    避免"看起来是算法结果、实际是历史布局复刻"。``cold_start`` 进一步
    忽略已验证案例库与审图缓存，只依赖原始 DXF + 画像配置。
    """
    if profile is None:
        profile = PROFILE_MIN_SHEETS

    if cold_start:
        use_template = False
        print("冷启动模式：忽略历史模板/已验证案例/审图缓存，"
              "仅用原始 DXF + 画像配置")

    # 工作流会话记录层：CLI 是状态机的第一个消费者，仅记录不改变行为
    session = WorkflowSession(
        session_id=f"cli-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    )
    project_id = Path(dxf_path).stem

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

    # ── 大板口径 + 打印约束摘要 ──
    # 排板空间 = 可利用尺寸（默认=标称）；让尺只进刀缝材料校验，且只计一次。
    rot_str = ",".join(str(r) for r in profile.rotation)
    caliber = resolve_sheet_caliber(
        width_mm,
        height_mm,
        sheet_oversize=sheet_oversize,
        kerf_reserve_mm=kerf_reserve_mm,
        usable_includes_oversize=usable_includes_oversize,
    )
    nesting_width_mm = caliber.usable_w_mm
    nesting_height_mm = caliber.usable_h_mm
    oversize_x = caliber.oversize_w_mm
    oversize_y = caliber.oversize_h_mm
    if nesting_width_mm <= 0 or nesting_height_mm <= 0:
        print(f"错误：排板空间无效 {nesting_width_mm:.0f}x"
              f"{nesting_height_mm:.0f}mm")
        sys.exit(1)
    print(f"约束: rotation=[{rot_str}], gap={profile.min_gap}mm, "
          f"group={profile.group_mode}, slide={profile.slide_to_edge}, "
          f"align={profile.align_edges}, thickness={effective_thickness}mm, "
          f"process={PROCESSING_CLASS_HELP[profile.processing_class]}, "
          f"free_rotation={profile.arbitrary_rotation}, "
          f"sheet_oversize={oversize_x:.0f}x{oversize_y:.0f}mm, "
          f"kerf={kerf_mm:.0f}mm")
    print(f"大板口径: 标称 {caliber.nominal_w_mm:.0f}x"
          f"{caliber.nominal_h_mm:.0f}mm，让尺 {oversize_x:.0f}x"
          f"{oversize_y:.0f}mm（每维度总量），可利用尺寸 "
          f"{nesting_width_mm:.0f}x{nesting_height_mm:.0f}mm"
          f"（{'含' if caliber.usable_includes_oversize else '不含'}让尺），"
          f"实物 {caliber.physical_w_mm:.0f}x{caliber.physical_h_mm:.0f}mm")
    if kerf_mm > 0 and not profile.uses_deepnest:
        print(f"刀缝校验: 允许 {nesting_width_mm + caliber.allowance_w_mm:.0f}x"
              f"{nesting_height_mm + caliber.allowance_h_mm:.0f}mm"
              f"（= 可利用尺寸 + 让尺），刀缝 {kerf_mm:.0f}mm；"
              f"DXF 外框 = 可利用尺寸 {nesting_width_mm:.0f}x"
              f"{nesting_height_mm:.0f}mm")
    else:
        print(f"DXF 外框 = 可利用尺寸 {nesting_width_mm:.0f}x"
              f"{nesting_height_mm:.0f}mm")
        if kerf_mm > 0:
            print(f"刀缝: {kerf_mm:.0f}mm 仅记录，水刀/激光刀缝不计入材料校验")

    # ── 读取 DXF / 匹配画像 / 编号 / 材料分组 ──
    prepared = prepare_drawing(
        dxf_path,
        drawing_profile=drawing_profile,
        skip_unnumbered=skip_unnumbered,
        layers=layers,
        exclude_layers=exclude_layers,
        reader_options=reader_options,
        group_mode=profile.group_mode,
        input_limits=input_limits,
        use_case_library=not cold_start,
    )
    if prepared.error:
        print(f"错误：{prepared.error}")
        sys.exit(1)

    drawing_profile = prepared.drawing_profile
    parts = prepared.parts
    recognized_drawing = prepared.recognized_drawing
    skipped_material_numbers = prepared.skipped_material_numbers
    groups = prepared.groups
    material_group_enabled = prepared.material_group_enabled
    forced_closure_parts = _forced_closure_parts(recognized_drawing)

    if material_filter:
        parts, groups = _filter_parts_by_material(parts, material_filter)
        print("注意：本次输出目录里的原位 DXF / 排板 DXF / 报告"
              f"只含材料 {', '.join(material_filter)} 的零件。")

    if forced_closure_parts:
        preview = "，".join(
            f"{item['number'] or item['handle']}(缺口 {item['closure_gap']:.0f}mm)"
            for item in forced_closure_parts[:5]
        )
        print(f"注意：{len(forced_closure_parts)} 个轮廓未闭合，已按两端点直线强制闭合，"
              f"请核对原位 DXF：{preview}")

    session.transition(WorkflowStage.ANALYZED, summary={"dxf": dxf_path})
    session.transition(
        WorkflowStage.PROFILE_MATCHED,
        summary={"profile": drawing_profile.name if drawing_profile else None},
    )
    session.transition(
        WorkflowStage.READ,
        summary={
            "parts": len(parts),
            "total_area": round(prepared.total_area, 1),
            "material_groups": [g for g in groups if g],
            "material_filter": material_filter,
            "skipped_material_numbers": len(skipped_material_numbers),
        },
    )
    session.transition(
        WorkflowStage.AUDITED,
        summary={"skipped": True, "note": "审图由 run_audit 独立入口执行"},
    )
    session.transition(
        WorkflowStage.NUMBERING_CONFIRMED,
        summary={"mode": "auto", "skip_unnumbered": skip_unnumbered},
    )

    special_w, special_h = special_size if special_size else (None, None)

    stem = Path(dxf_path).stem
    out_dir = None
    store = None

    def _track_artifact(stage: WorkflowStage, path) -> None:
        if store is None or path is None:
            return
        ref = store.track(project_id, stage.value, path)
        session.record_artifact(ref.artifact_id, ref.to_dict())

    def _write_session_file() -> None:
        if out_dir is None:
            return
        payload = session.to_dict()
        (out_dir / f"{stem}_workflow_session.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if not report_only:
        out_dir = make_output_dir(dxf_path, root=output_root)
        store = ArtifactStore(out_dir)
        recognized_json = out_dir / f"{stem}_recognized.json"
        recognized_json.write_text(
            recognized_drawing.to_json(),
            encoding="utf-8",
        )
        numbered_original = out_dir / f"{stem}_numbered_原位.dxf"
        write_numbered_parts_dxf(parts, str(numbered_original),
                                 unit_system=unit.value)
        _track_artifact(WorkflowStage.READ, recognized_json)
        _track_artifact(WorkflowStage.READ, numbered_original)

    if profile.uses_deepnest:
        print(f"开始排板... (DeepNest/BLF, trials={trials})")
    else:
        print(f"开始排板... ({trials} 轮 x {budget:.0f}s LNS)")

    template_result = False
    nesting = None
    layout_source = "algorithm"
    if (
        use_template
        and not cold_start
        and groups == [None]
        and special_w is None
        and not profile.uses_deepnest
    ):
        template_record = match_template(dxf_path, width_mm, height_mm)
        if template_record is not None:
            candidate = nest_from_template(parts, template_record)
            template_errors = validate_mixed_nesting(
                candidate, min_gap=profile.min_gap
            )
            if not template_errors:
                template_min_sheets = math.ceil(
                    candidate.total_part_area / (width_mm * height_mm)
                )
                nesting = GroupNestingOutcome(
                    result=candidate,
                    material_summary=[
                        {
                            "material_group": "ALL",
                            "part_count": len(parts),
                            "sheets": candidate.total_sheets,
                            "yield_rate": round(candidate.yield_rate, 2),
                            "theoretical_min_sheets": template_min_sheets,
                        }
                    ],
                    total_min_sheets=template_min_sheets,
                    total_normal_parts=len(parts),
                    total_special_parts=0,
                    total_special_pair_min_sheets=None,
                    elapsed_seconds=0.0,
                )
                template_result = True
                layout_source = f"template:{template_record.name}"
                print(f"模板排板命中: {template_record.name}")

    # 优化器诊断：确定性预算消耗、是否被墙钟护栏截断（进报告 parameters.optimizer）
    optimizer_stats: dict = {}
    if nesting is None:
        nesting = nest_groups(
            parts, groups, nesting_width_mm, nesting_height_mm,
            special_w, special_h,
            effective_thickness, unit, profile, drawing_profile,
            trials, seed, budget, quick, pairing,
            beam=beam, beam_width=beam_width,
            stats=optimizer_stats,
            compress=compress,
        )
    result = nesting.result
    print(f"布局来源: 模板 {layout_source}" if template_result
          else "布局来源: 算法排板")
    material_summary = nesting.material_summary
    total_min_sheets = nesting.total_min_sheets
    total_special_parts = nesting.total_special_parts
    total_special_pair_min_sheets = nesting.total_special_pair_min_sheets
    elapsed = nesting.elapsed_seconds

    # ── 排板完成后先报告板数，确认后再执行后处理 ──
    print(f"排板完成：使用 {result.total_sheets} 张大板，"
          f"出材率 {result.yield_rate:.2f}%，耗时 {elapsed:.1f}s")
    _lns_it = optimizer_stats.get("lns_iterations")
    if _lns_it is not None:
        print(f"优化预算：LNS {_lns_it} 轮"
              f"（连续无改进 {optimizer_stats.get('lns_dry_rounds')} 轮停止），"
              f"{optimizer_stats.get('lns_seconds')}s，"
              + ("墙钟护栏已触发（本次结果不可复现）"
                 if optimizer_stats.get("lns_budget_exhausted")
                 else "确定性预算内完成"))
    session.transition(
        WorkflowStage.NESTED_REPORTED,
        summary={
            "total_sheets": result.total_sheets,
            "yield_rate": round(result.yield_rate, 2),
            "elapsed_seconds": round(elapsed, 1),
            "layout_source": layout_source,
        },
    )
    if report_only:
        print("已按 --report-only 停止：未执行后处理和输出文件。")
        return {
            "report_only": True,
            "layout_source": layout_source,
            "total_sheets": result.total_sheets,
            "yield_rate": round(result.yield_rate, 2),
        }
    if confirm_sheet_count and sys.stdin.isatty():
        ans = input("是否接受该板数并开始后处理推板？[y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消：未执行后处理和输出。")
            session.cancel("用户拒绝板数，未执行后处理")
            _write_session_file()
            return {"cancelled": True}
    elif confirm_sheet_count:
        print("非交互模式：自动确认板数，开始后处理。")

    # ── 后处理：推边压实 + 边缘对齐 + 最小间距 ──
    if template_result:
        manufacturing_metrics = {
            "edge_contact_mm": 0.0,
            "through_cut_mm": 0.0,
        }
    else:
        manufacturing_metrics = postprocess_result(result, profile)

    # ── 回看排板结果：基于压实后的图形，能合并的少量板就合并 ──
    review_candidates: list = []
    if auto_review and not template_result and special_w is None:
        (
            review_candidates, result, material_summary, manufacturing_metrics,
        ) = _auto_reduce_sheets(
            result, profile, material_summary, manufacturing_metrics,
            time_budget_s=review_budget_s,
            stats=optimizer_stats,
        )
    kerf_metrics = (
        estimate_kerf_material(result.sheets, kerf_mm)
        if kerf_mm > 0 else None
    )
    # 刀缝校验允许 = 可利用尺寸 + 让尺（= 实物尺寸），让尺只计一次。
    # 水刀/激光的刀缝按口径"可不计"，因此不做这项校验，只保留指标。
    kerf_material_errors = (
        validate_kerf_material(
            result.sheets, kerf_mm,
            allowance_x_mm=caliber.allowance_w_mm,
            allowance_y_mm=caliber.allowance_h_mm,
        )
        if kerf_mm > 0 and not profile.uses_deepnest else []
    )

    # ── 校验 ──
    errors = validate_mixed_nesting(result, min_gap=profile.min_gap)
    if kerf_material_errors:
        errors.extend(kerf_material_errors)
    status = "通过" if not errors else f"{len(errors)} 处违规"
    print(f"完成：{result.total_sheets} 张板，"
          f"出材率 {result.yield_rate:.2f}%，"
          f"校验{status}，耗时 {elapsed:.1f}s")
    for e in errors[:5]:
        print(f"  ! {e}")

    # ── DXF 生成前检查 ──
    check_payload, postprocess_warnings, waterjet_metrics = build_check_payload(
        result, profile, drawing_profile, width_mm, height_mm,
        special_w, special_h, effective_thickness, unit_label,
        errors, manufacturing_metrics, sheet_oversize=(oversize_x, oversize_y),
        usable_size=(nesting_width_mm, nesting_height_mm),
        usable_includes_oversize=caliber.usable_includes_oversize,
    )
    check_payload["forced_closure_count"] = len(forced_closure_parts)
    check_payload["forced_closure_parts"] = forced_closure_parts
    if template_result:
        postprocess_warnings = []
        check_payload["postprocess_warning_count"] = 0
        check_payload["postprocess_warnings"] = []
    check_path = out_dir / f"{stem}_postprocess_check.json"
    with open(check_path, "w", encoding="utf-8") as f:
        json.dump(check_payload, f, ensure_ascii=False, indent=2)

    if postprocess_warnings:
        print(f"后处理检查：{len(postprocess_warnings)} 项未完全达标")
        for warning in postprocess_warnings[:5]:
            print(f"  - {warning['type']}: sheet {warning['sheet']}")
    else:
        print("后处理检查：靠边/通切/间距均达标")
    print(f"检查报告: {check_path}")

    if errors:
        print("几何校验未通过，已停止生成最终 DXF。")
        session.block(
            "几何校验未通过",
            summary={"error_count": len(errors)},
        )
        _track_artifact(WorkflowStage.POSTPROCESS_CONFIRMED, check_path)
        _write_session_file()
        return {
            "geometry_errors": errors,
            "check_path": str(check_path),
            "layout_source": layout_source,
        }

    session.transition(
        WorkflowStage.POSTPROCESS_CONFIRMED,
        summary={
            "postprocess_warnings": len(postprocess_warnings),
            "check_only": check_only,
        },
    )
    _track_artifact(WorkflowStage.POSTPROCESS_CONFIRMED, check_path)

    if check_only:
        print("已按 --check-only 停止：未生成最终 DXF 和排板报告。")
        _write_session_file()
        return {
            "check_only": True,
            "geometry_errors": errors,
            "postprocess_warnings": postprocess_warnings,
            "check_path": str(check_path),
            "layout_source": layout_source,
        }

    # ── 输出 ──
    if special_w and special_h and total_special_parts:
        suffix = (f"{int(width_mm)}x{int(height_mm)}"
                  f"+{int(special_w)}x{int(special_h)}")
    else:
        suffix = f"{int(width_mm)}x{int(height_mm)}"
    out_dxf = str(out_dir / f"{stem}_nested_{suffix}.dxf")
    out_json = str(out_dir / f"{stem}_report_{suffix}.json")

    # 大板外框按【可利用尺寸】绘制（默认 = 标称 3200x1600）。让尺是标称之外
    # 的实物余量，不进零件可用区；只有声明"可利用尺寸包括让尺"时才画出让尺带。
    write_nested_dxf(result, out_dxf, unit_system=unit.value,
                     board_size=(nesting_width_mm, nesting_height_mm))

    report = build_report(
        result, profile, width_mm, height_mm, special_w, special_h,
        effective_thickness, unit_label, total_min_sheets,
        total_special_pair_min_sheets, material_summary, elapsed,
        errors, manufacturing_metrics, trials, seed, budget,
        skip_unnumbered,
        kerf_metrics=kerf_metrics,
        sheet_oversize=(oversize_x, oversize_y),
        usable_size=(nesting_width_mm, nesting_height_mm),
        usable_includes_oversize=caliber.usable_includes_oversize,
        optimizer_stats=optimizer_stats,
    )
    report["forced_closure_parts"] = forced_closure_parts
    report["auto_review"] = {
        "reduced_sheets": len(review_candidates),
        "removed_sheet_indices": [
            candidate.removed_sheet_index for candidate in review_candidates
        ],
        "moved_parts": [
            number for candidate in review_candidates
            for number in candidate.moved_parts
        ],
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"DXF: {out_dxf}")
    print(f"报告: {out_json}")
    _track_artifact(WorkflowStage.COMPLETED, out_dxf)
    _track_artifact(WorkflowStage.COMPLETED, out_json)
    session.transition(
        WorkflowStage.COMPLETED,
        summary={"out_dxf": out_dxf, "out_json": out_json},
    )
    _write_session_file()
    return {
        "geometry_errors": errors,
        "postprocess_warnings": postprocess_warnings,
        "check_path": str(check_path),
        "layout_source": layout_source,
    }


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

    try:
        requested_drawing_profile = resolve_requested_drawing_profile(
            args.drawing_profile
        )
    except ValueError as exc:
        print(f"\u9519\u8bef\uff1a{exc}")
        sys.exit(1)

    if args.config:
        config = ProjectConfig.from_file(args.config)
        apply_cli_overrides_to_project_config(config, args)
        outcome = run_with_config(
            dxf_path,
            config,
            unit=UnitSystem.METRIC,
            confirm_sheet_count=not args.no_confirm,
            report_only=args.report_only,
            check_only=args.check_only,
        )
        if args.check_only:
            sys.exit(check_only_exit_code(outcome))
        return

    if args.audit:
        run_audit(
            dxf_path,
            previous_state_path=args.previous_state,
            accept_issue_ids=split_issue_ids(args.accept_issue),
            ignore_issue_ids=split_issue_ids(args.ignore_issue),
            fixed_issue_ids=split_issue_ids(args.mark_fixed),
            drawing_profile=requested_drawing_profile,
            use_cache=not args.cold_start,
        )
        return

    file_suffix = Path(dxf_path).suffix.lower()
    if args.list_nest or file_suffix in {".xlsx", ".xls", ".pdf"}:
        unit = UnitSystem.IMPERIAL if args.imperial else UnitSystem.METRIC
        if args.sizes:
            raw_sizes = parse_sizes_value(args.sizes)
            if not raw_sizes:
                print("错误：--sizes 至少需要一个尺寸")
                sys.exit(1)
            sizes_mm = [
                (convert_to_mm(width, unit), convert_to_mm(height, unit))
                for width, height in raw_sizes
            ]
        else:
            width, height = resolve_sheet_size(args)
            if width is None or height is None:
                print("错误：清单排板需要指定大板尺寸，"
                      "如 python main.py input.xlsx 3200 1800 --list-nest")
                sys.exit(1)
            sizes_mm = [(convert_to_mm(width, unit), convert_to_mm(height, unit))]
            special_size = parse_special_size(args.special_size)
            if special_size:
                sizes_mm.append(
                    (
                        convert_to_mm(special_size[0], unit),
                        convert_to_mm(special_size[1], unit),
                    )
                )
        thickness_mm = convert_to_mm(args.thickness, unit) if args.thickness else 20.0
        if args.no_rotation:
            rotations = [0]
        elif args.rotation:
            rotations = [int(x.strip()) for x in args.rotation.split(",")]
        else:
            rotations = [0, 90]
        if args.output_dxf:
            output_dxf_path = args.output_dxf
        elif args.no_dxf:
            output_dxf_path = None
        else:
            output_dir = PROJECT_ROOT / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_dxf_path = str(
                output_dir / f"{Path(dxf_path).stem}_list_nested.dxf"
            )
        try:
            conclusion = run_list_nesting(
                dxf_path,
                thickness_mm=thickness_mm,
                rotations=tuple(rotations),
                trials=args.trials,
                seed=args.seed,
                sheet_sizes=sizes_mm,
                output_dxf_path=output_dxf_path,
                kerf_mm=args.kerf,
                oversize_mm=args.oversize,
            )
            print(conclusion)
            if output_dxf_path:
                print(f"DXF 已生成：{output_dxf_path}")
        except ValueError as exc:
            print(f"错误：{exc}")
            sys.exit(1)
        return

    width, height = resolve_sheet_size(args)
    if width is None or height is None:
        print("错误：CLI 模式需要指定大板尺寸，"
              "如 python main.py input.dxf 3200 1800 20\n"
              "或用 --size 选择标准规格，--list-sizes 查看")
        sys.exit(1)

    layers = args.layers.split(",") if args.layers else None
    exclude_layers = args.exclude_layers.split(",") if args.exclude_layers else None
    unit = UnitSystem.IMPERIAL if args.imperial else UnitSystem.METRIC

    trials = args.trials
    budget = args.budget
    quick = args.quick
    if args.quality:
        preset = QUALITY_PRESETS[args.quality]
        if args.trials == 1:
            trials = preset["trials"]
        if args.budget == 180.0:
            budget = preset["budget_seconds"]
        if not args.quick:
            quick = preset["quick"]

    # ── 构建 NestingProfile ──
    if args.profile:
        profile = PROFILES[args.profile]
    else:
        profile = default_profile_for_process(args.process) or PROFILE_MIN_SHEETS

    # 应用覆盖
    overrides = {}
    processing_class = resolve_processing_class(args.process)
    if processing_class is not None:
        overrides["processing_class"] = processing_class
    if args.rotation is not None:
        overrides["rotation"] = [int(x.strip())
                                 for x in args.rotation.split(",")]
    if args.free_rotation:
        overrides["arbitrary_rotation"] = True
    if args.no_rotation:
        overrides["rotation"] = [0]
        overrides["arbitrary_rotation"] = False
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

    raw_oversize = parse_sheet_oversize(args.sheet_oversize)
    sheet_oversize = (
        convert_to_mm(raw_oversize[0], unit),
        convert_to_mm(raw_oversize[1], unit),
    )

    if args.agent:
        sys.exit(
            run_agent_mode(
                dxf_path,
                width,
                height,
                args.thickness,
                unit,
                profile,
                agent_provider=args.agent_provider,
                trials=trials,
                seed=args.seed,
                budget=budget,
                quick=quick,
                pairing=args.pairing,
                skip_unnumbered=not args.include_unnumbered,
            )
        )

    outcome = run(
        dxf_path, width, height, args.thickness, unit,
        trials=trials, seed=args.seed, budget=budget,
        skip_unnumbered=not args.include_unnumbered,
        layers=layers, exclude_layers=exclude_layers,
        drawing_profile=requested_drawing_profile,
        profile=profile,
        confirm_sheet_count=not args.no_confirm,
        report_only=args.report_only,
        special_size=parse_special_size(args.special_size),
        check_only=args.check_only,
        quick=quick,
        pairing=args.pairing,
        kerf_mm=args.kerf,
        kerf_reserve_mm=args.kerf_reserve,
        sheet_oversize=sheet_oversize,
        usable_includes_oversize=args.usable_includes_oversize,
        auto_review=not args.no_auto_review,
        review_budget_s=args.review_budget,
        use_template=args.use_template,
        cold_start=args.cold_start,
        beam=not args.no_beam,
        beam_width=args.beam_width,
        compress=not args.no_compress,
        material_filter=parse_material_filter(args.material),
    )
    if args.check_only:
        sys.exit(check_only_exit_code(outcome))


if __name__ == "__main__":
    main()
