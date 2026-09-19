#!python
# -*- coding: utf-8 -*-
"""DXF 自动排板系统 - 主入口（支持 CLI 参数 + 交互模式）"""

import argparse
import hashlib
import json
import math
import os
import sys
import time
import re
from datetime import datetime
from pathlib import Path
from collections import Counter

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
    audit_drawing,
    load_profiles,
    rank_profiles,
    read_dxf_with_profile,
    write_audit_dxf,
)
from src.case_library import load_cases, load_case_profile, match_case
from src.audit_cache import (
    DEFAULT_CACHE_DIR,
    audit_cache_key,
    load_cached_issues,
    save_cached_issues,
)
from src.visual_evidence import write_issue_evidence_svg
from src.visual_renderer import write_dxf_overview_svg
from src.contracts import (
    DrawingIssue as ContractDrawingIssue,
    IssueSeverity,
    IssueStatus,
    RecheckSummary,
    ReviewState,
)
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
    p.add_argument("--imperial", action="store_true",
                   help="英制模式（默认公制）")

    # 约束模板
    p.add_argument("--profile", type=str, default=None,
                   choices=list(PROFILES.keys()),
                   help="约束模板：" + " / ".join(
                       f"{k}={v}" for k, v in PROFILE_HELP.items()))
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
    p.add_argument("--review-budget", type=float, default=120.0,
                   help="自动回看的总时间上限（秒，默认 120）")
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
                   help="大板让尺 mm（清单排板；可用尺寸=标称+让尺）")
    p.add_argument("--sheet-oversize", type=str, default=None,
                   help="桥切大板长/宽让尺 mm，如 30 或 30,30（默认 0；"
                        "让尺只用于容纳刀路，不放大零件可用尺寸）")
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


# ═══════════════════════════════════════════════════════════════
#  辅助
# ═══════════════════════════════════════════════════════════════

def make_output_dir(dxf_path: str, *, root: Path | None = None) -> Path:
    """按 时间戳_文件名 创建输出子目录"""
    stem = Path(dxf_path).stem
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = root if root is not None else PROJECT_ROOT / "output"
    out_dir = base_dir / f"{ts}_{stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _severity_contract(value: str) -> IssueSeverity:
    return {
        "error": IssueSeverity.ERROR,
        "warning": IssueSeverity.WARNING,
        "info": IssueSeverity.INFO,
    }.get(value, IssueSeverity.WARNING)


def _to_contract_issue(issue) -> ContractDrawingIssue:
    """把 drawing_profile.DrawingIssue 转成读图数据契约。"""
    return ContractDrawingIssue(
        issue_id=str(issue.issue_id),
        severity=_severity_contract(issue.severity),
        issue_type=issue.type,
        entity_handle=issue.entity_handle,
        layer=issue.layer,
        coordinates=issue.coordinates,
        message=issue.message,
        suggestion=issue.suggestion,
        status=IssueStatus.NEW,
    )


def _file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _issues_match(
    previous: ContractDrawingIssue,
    current: ContractDrawingIssue,
    coordinate_tolerance: float = 10.0,
) -> bool:
    """按实体句柄、坐标或图层/消息判断是否为同一问题。"""
    if previous.issue_type != current.issue_type:
        return False
    if previous.entity_handle and current.entity_handle:
        return previous.entity_handle == current.entity_handle
    if previous.coordinates and current.coordinates:
        return (
            math.hypot(
                previous.coordinates[0] - current.coordinates[0],
                previous.coordinates[1] - current.coordinates[1],
            )
            <= coordinate_tolerance
        )
    return previous.layer == current.layer and previous.message == current.message


def _split_issue_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _apply_issue_statuses(
    issues: list[ContractDrawingIssue],
    accept_issue_ids: list[str],
    ignore_issue_ids: list[str],
    fixed_issue_ids: list[str],
) -> list[ContractDrawingIssue]:
    """把用户指定的状态写入当前问题列表。"""
    status_by_id: dict[str, IssueStatus] = {}
    for issue_id in accept_issue_ids:
        status_by_id[issue_id] = IssueStatus.ACCEPTED
    for issue_id in ignore_issue_ids:
        status_by_id[issue_id] = IssueStatus.IGNORED
    for issue_id in fixed_issue_ids:
        status_by_id[issue_id] = IssueStatus.FIXED
    if not status_by_id:
        return issues

    updated: list[ContractDrawingIssue] = []
    for issue in issues:
        status = status_by_id.get(issue.issue_id)
        if status is None:
            updated.append(issue)
        else:
            updated.append(issue.model_copy(update={"status": status}))
    return updated


def run_audit(
    dxf_path: str,
    previous_state_path: str | None = None,
    accept_issue_ids: list[str] | None = None,
    ignore_issue_ids: list[str] | None = None,
    fixed_issue_ids: list[str] | None = None,
) -> None:
    """只做读图审图，输出问题 JSON 和高亮 DXF，不进入排板。"""
    if Path(dxf_path).suffix.lower() != ".dxf":
        print(f"错误：仅支持 DXF 输入，收到 - {dxf_path}")
        sys.exit(1)
    stem = Path(dxf_path).stem
    out_dir = make_output_dir(dxf_path)

    drawing_profile = resolve_drawing_profile(dxf_path)
    if drawing_profile is None:
        print("错误：审图模式需要匹配到 drawing_profiles/*.json 图纸画像。")
        sys.exit(1)

    print(f"matched drawing profile: {drawing_profile.name}")
    current_hash = _file_sha256(dxf_path)
    cache_key = audit_cache_key(current_hash, drawing_profile)
    issues = load_cached_issues(DEFAULT_CACHE_DIR, cache_key)
    if issues is None:
        print("开始审图...")
        issues = audit_drawing(dxf_path, drawing_profile)
        save_cached_issues(DEFAULT_CACHE_DIR, cache_key, issues)
    else:
        print(f"使用审计缓存: {cache_key[:24]}...")
    contract_issues = [_to_contract_issue(issue) for issue in issues]

    previous_state = None
    recheck = None
    if previous_state_path:
        previous_path = Path(previous_state_path)
        if not previous_path.exists():
            print(f"错误：找不到上次审图状态文件 - {previous_state_path}")
            sys.exit(1)
        previous_state = ReviewState.from_json(
            previous_path.read_text(encoding="utf-8")
        )
        if previous_state.file_sha256 == current_hash:
            print("提示：当前文件与上次审图文件哈希相同，复检差异可能为空。")

    audit_json = out_dir / f"{stem}_audit.json"
    audit_dxf = out_dir / f"{stem}_audit.dxf"
    state_json = out_dir / f"{stem}_review_state.json"
    recheck_json = out_dir / f"{stem}_recheck.json"

    if previous_state is not None:
        matched_new_indexes: set[int] = set()
        recheck_fixed_issue_ids: list[str] = []
        still_open_issue_ids: list[str] = []
        for old_issue in previous_state.issues:
            matched = False
            for index, current_issue in enumerate(contract_issues):
                if index in matched_new_indexes:
                    continue
                if _issues_match(old_issue, current_issue):
                    contract_issues[index] = current_issue.model_copy(
                        update={"status": old_issue.status}
                    )
                    still_open_issue_ids.append(old_issue.issue_id)
                    matched_new_indexes.add(index)
                    matched = True
                    break
            if not matched:
                recheck_fixed_issue_ids.append(old_issue.issue_id)
        new_issue_ids = [
            issue.issue_id
            for index, issue in enumerate(contract_issues)
            if index not in matched_new_indexes
        ]
        recheck = RecheckSummary(
            parent_review_id=previous_state.review_id,
            child_review_id=f"review_{current_hash[:16]}",
            fixed_issue_ids=recheck_fixed_issue_ids,
            still_open_issue_ids=still_open_issue_ids,
            new_issue_ids=new_issue_ids,
        )

    contract_issues = _apply_issue_statuses(
        contract_issues,
        accept_issue_ids=accept_issue_ids or [],
        ignore_issue_ids=ignore_issue_ids or [],
        fixed_issue_ids=fixed_issue_ids or [],
    )

    if len(contract_issues) <= MAX_EVIDENCE_ISSUES:
        evidence_results = write_issue_evidence_svg(
            dxf_path, contract_issues, out_dir
        )
    else:
        print(
            f"警告：问题数量 {len(contract_issues)} 超过 "
            f"{MAX_EVIDENCE_ISSUES}，跳过逐问题 SVG，仅生成整图检查图。"
        )
        evidence_results = []
    if evidence_results:
        evidence_by_issue_id = {
            item["issue_id"]: item for item in evidence_results
        }
        contract_issues = [
            issue.model_copy(
                update={
                    "evidence_artifact_id": evidence_by_issue_id.get(
                        issue.issue_id, {}
                    ).get("artifact_id"),
                    "evidence_digest": evidence_by_issue_id.get(
                        issue.issue_id, {}
                    ).get("digest"),
                }
            )
            if issue.issue_id in evidence_by_issue_id
            else issue
            for issue in contract_issues
        ]

    summary = Counter(issue.issue_type for issue in contract_issues)
    payload = {
        "schema_version": contract_issues[0].schema_version if contract_issues else "0.1.0",
        "source": str(Path(dxf_path).resolve()),
        "file_sha256": current_hash,
        "issue_count": len(contract_issues),
        "summary": dict(summary),
        "issues": [issue.model_dump(mode="json") for issue in contract_issues],
    }
    if recheck is not None:
        payload["recheck"] = recheck.model_dump(mode="json")
    audit_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_audit_dxf(dxf_path, issues, audit_dxf)
    overview_svg = out_dir / f"{stem}_overview.svg"
    try:
        write_dxf_overview_svg(dxf_path, overview_svg, issues=issues)
    except Exception as exc:
        print(f"警告：生成整图检查 SVG 失败 - {exc}")
        overview_svg = None

    review_state = ReviewState(
        review_id=recheck.child_review_id if recheck else f"review_{current_hash[:16]}",
        drawing_path=str(Path(dxf_path).resolve()),
        file_sha256=current_hash,
        profile_name=drawing_profile.name,
        parent_review_id=previous_state.review_id if previous_state else None,
        issues=contract_issues,
    )
    state_json.write_text(review_state.to_json(), encoding="utf-8")
    if recheck is not None:
        recheck_json.write_text(recheck.to_json(), encoding="utf-8")

    print(f"审图完成：发现 {len(contract_issues)} 个问题")
    for issue_type, count in sorted(summary.items()):
        print(f"  {issue_type}: {count}")
    for issue in issues[:10]:
        print(
            f"  ! [{issue.severity}] {issue.type} "
            f"({issue.coordinates[0]:.1f}, {issue.coordinates[1]:.1f}) "
            f"{issue.message}"
        )
    print(f"JSON: {audit_json}")
    print(f"高亮 DXF: {audit_dxf}")
    if overview_svg is not None:
        print(f"检查图: {overview_svg}")
    print(f"状态: {state_json}")
    if recheck is not None:
        print(
            f"复检: 修复 {len(recheck.fixed_issue_ids)}，"
            f"仍存在 {len(recheck.still_open_issue_ids)}，"
            f"新增 {len(recheck.new_issue_ids)}"
        )
        print(f"复检 JSON: {recheck_json}")


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


MAX_EVIDENCE_ISSUES = 100


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
                        max_donor_attempts: int = 60,
                        time_budget_s: float = 120.0,
                        per_donor_time_s: float = 20.0):
    """后处理后回看排板结果，把能合并的板并掉。

    回看是增强项：失败、超时、校验不通过都不影响主流程，
    只是保留原排板。
    """
    print(f"回看排板结果：{result.total_sheets} 张板，"
          f"寻找可合并的大板...")
    try:
        candidates, reduced = find_reductions(
            result,
            profile=profile,
            max_reductions=max_reductions,
            max_donor_attempts=max_donor_attempts,
            min_gap=profile.min_gap,
            time_budget_s=time_budget_s,
            per_donor_time_s=per_donor_time_s,
        )
    except Exception as exc:  # noqa: BLE001 - 回看不能影响主流程
        print(f"回看跳过：{exc}")
        return [], result, material_summary, manufacturing_metrics

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
        sheet_oversize: tuple[float, float] | None = None,
        output_root: Path | None = None,
        auto_review: bool = True,
        review_budget_s: float = 120.0) -> None:
    """核心排板流程"""
    if profile is None:
        profile = PROFILE_MIN_SHEETS

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

    # ── 打印约束摘要 ──
    rot_str = ",".join(str(r) for r in profile.rotation)
    oversize_x, oversize_y = sheet_oversize or (0.0, 0.0)
    print(f"约束: rotation=[{rot_str}], gap={profile.min_gap}mm, "
          f"group={profile.group_mode}, slide={profile.slide_to_edge}, "
          f"align={profile.align_edges}, thickness={effective_thickness}mm, "
          f"process={PROCESSING_CLASS_HELP[profile.processing_class]}, "
          f"free_rotation={profile.arbitrary_rotation}, "
          f"sheet_oversize={oversize_x:.0f}x{oversize_y:.0f}mm")

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
    if (
        groups == [None]
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
                print(f"模板排板命中: {template_record.name}")

    if nesting is None:
        nesting = nest_groups(
            parts, groups, width_mm, height_mm, special_w, special_h,
            effective_thickness, unit, profile, drawing_profile,
            trials, seed, budget, quick, pairing,
        )
    result = nesting.result
    material_summary = nesting.material_summary
    total_min_sheets = nesting.total_min_sheets
    total_special_parts = nesting.total_special_parts
    total_special_pair_min_sheets = nesting.total_special_pair_min_sheets
    elapsed = nesting.elapsed_seconds

    # ── 排板完成后先报告板数，确认后再执行后处理 ──
    print(f"排板完成：使用 {result.total_sheets} 张大板，"
          f"出材率 {result.yield_rate:.2f}%，耗时 {elapsed:.1f}s")
    session.transition(
        WorkflowStage.NESTED_REPORTED,
        summary={
            "total_sheets": result.total_sheets,
            "yield_rate": round(result.yield_rate, 2),
            "elapsed_seconds": round(elapsed, 1),
        },
    )
    if report_only:
        print("已按 --report-only 停止：未执行后处理和输出文件。")
        return {"report_only": True}
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
        )
    kerf_metrics = (
        estimate_kerf_material(result.sheets, kerf_mm)
        if kerf_mm > 0 else None
    )
    kerf_material_errors = (
        validate_kerf_material(
            result.sheets, kerf_mm,
            allowance_x_mm=oversize_x,
            allowance_y_mm=oversize_y,
        )
        if kerf_mm > 0 else []
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
        }

    # ── 输出 ──
    if special_w and special_h and total_special_parts:
        suffix = (f"{int(width_mm)}x{int(height_mm)}"
                  f"+{int(special_w)}x{int(special_h)}")
    else:
        suffix = f"{int(width_mm)}x{int(height_mm)}"
    out_dxf = str(out_dir / f"{stem}_nested_{suffix}.dxf")
    out_json = str(out_dir / f"{stem}_report_{suffix}.json")

    write_nested_dxf(result, out_dxf, unit_system=unit.value)

    report = build_report(
        result, profile, width_mm, height_mm, special_w, special_h,
        effective_thickness, unit_label, total_min_sheets,
        total_special_pair_min_sheets, material_summary, elapsed,
        errors, manufacturing_metrics, trials, seed, budget,
        skip_unnumbered,
        kerf_metrics=kerf_metrics,
        sheet_oversize=(oversize_x, oversize_y),
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
            accept_issue_ids=_split_issue_ids(args.accept_issue),
            ignore_issue_ids=_split_issue_ids(args.ignore_issue),
            fixed_issue_ids=_split_issue_ids(args.mark_fixed),
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
        profile=profile,
        confirm_sheet_count=not args.no_confirm,
        report_only=args.report_only,
        special_size=parse_special_size(args.special_size),
        check_only=args.check_only,
        quick=quick,
        pairing=args.pairing,
        kerf_mm=args.kerf,
        sheet_oversize=sheet_oversize,
        auto_review=not args.no_auto_review,
        review_budget_s=args.review_budget,
    )
    if args.check_only:
        sys.exit(check_only_exit_code(outcome))


if __name__ == "__main__":
    main()
