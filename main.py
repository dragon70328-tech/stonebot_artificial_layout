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
import itertools
import re
from datetime import datetime
from pathlib import Path
from collections import Counter
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
    PROCESSING_CLASS_BRIDGE,
    PROCESSING_CLASS_WATERJET_LASER,
    PROCESSING_CLASS_HELP,
    STANDARD_SHEET_SIZES, STANDARD_THICKNESSES,
    get_sheet_size, get_sheet_size_by_index,
)
from src.postprocess import PostProcessor
from src.deepnest_engine import nest_parts_deepnest
from src.pairing import nest_parts_deepnest_paired
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
    p.add_argument("--process", type=str, default=None,
                   choices=["bridge", "waterjet", "laser"],
                   help="加工方式：bridge=桥切机，waterjet=水刀，laser=激光")

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
    p.add_argument("--audit", action="store_true",
                   help="仅执行读图审图并输出问题报告，不进入排板")
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

def make_output_dir(dxf_path: str) -> Path:
    """按 时间戳_文件名 创建输出子目录"""
    stem = Path(dxf_path).stem
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = PROJECT_ROOT / "output" / f"{ts}_{stem}"
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


def resolve_drawing_profile(dxf_path: str):
    """Match a validated case first, then fall back to profile ranking."""
    try:
        fingerprint = analyze_drawing(dxf_path)
        profiles = load_profiles(PROJECT_ROOT / "drawing_profiles")
        cases = load_cases(PROJECT_ROOT / "drawing_profiles" / "validated_cases")
        case_match = match_case(fingerprint, cases, min_score=MIN_CASE_SCORE)
        if case_match is not None:
            case, case_score = case_match
            profile = load_case_profile(case, profiles)
            if profile is not None:
                print(
                    f"matched validated case: {case.case_id} "
                    f"(score={case_score:.1f}, profile={profile.name})"
                )
                return profile
        ranked = rank_profiles(fingerprint, profiles)
    except Exception:
        return None
    if not ranked:
        return None
    profile, score = ranked[0]
    if score < MIN_PROFILE_SCORE:
        return None
    return profile


def material_group_for_number(number: str, drawing_profile) -> str | None:
    """Return the normalized material prefix when a drawing profile enables it."""
    if drawing_profile is None or not drawing_profile.material_group_enabled:
        return None
    match = re.match(drawing_profile.material_prefix_pattern, number or "")
    if not match:
        return None
    prefix = (
        match.group("prefix")
        if "prefix" in match.groupdict()
        else match.group(0)
    ).upper()
    allowed = {value.upper() for value in drawing_profile.allowed_material_prefixes}
    if allowed and prefix not in allowed:
        return None
    return prefix


def apply_material_grouping(parts, drawing_profile):
    """Assign material groups and remove parts without an allowed material prefix."""
    if drawing_profile is None or not drawing_profile.material_group_enabled:
        return parts, []

    grouped = []
    skipped = []
    for part in parts:
        prefix = material_group_for_number(part.number, drawing_profile)
        if prefix is None:
            skipped.append(part.number)
            continue
        part.material_group = prefix
        grouped.append(part)
    return grouped, skipped


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
MIN_PROFILE_SCORE = 30.0
MIN_CASE_SCORE = 75.0
MAX_EVIDENCE_ISSUES = 100
QUICK_CONFIGS = [
    ("short", "skyline", 0),
    ("short", "col", 0),
    ("area", "skyline", 0),
    ("long", "col", 0),
]


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
        material_group=part.material_group,
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


def _nest_one_group(parts, width_mm, height_mm, special_w, special_h,
                    effective_thickness, unit, profile, drawing_profile,
                    trials, seed, budget, quick, pairing=False):
    """Nest one material group and return result plus group stats."""
    normal_parts = []
    special_parts = []
    unfit_numbers = []
    for part in parts:
        if part_fits(part, width_mm, height_mm, profile.rotation):
            normal_parts.append(part)
        elif special_w and special_h and part_fits(
            part, special_w, special_h, profile.rotation
        ):
            special_parts.append(part)
        else:
            unfit_numbers.append(part.number)

    if unfit_numbers:
        raise RuntimeError(
            "以下零件无法放入可用大板：" + ", ".join(unfit_numbers[:20])
        )

    group_area = sum(part.area for part in parts)
    normal_area = sum(part.area for part in normal_parts)
    special_area = sum(part.area for part in special_parts)
    normal_sheet_area = width_mm * height_mm
    if special_parts:
        special_sheet_area = special_w * special_h
        min_sheets = (
            math.ceil(normal_area / normal_sheet_area)
            + math.ceil(special_area / special_sheet_area)
        )
        special_pair_min_sheets = math.ceil(len(special_parts) / 2.0)
    else:
        min_sheets = math.ceil(group_area / normal_sheet_area)
        special_pair_min_sheets = None

    results = []
    if normal_parts:
        first_left = bool(
            drawing_profile is not None
            and drawing_profile.first_part_left_edge
        )
        if profile.uses_deepnest:
            if pairing:
                results.append(nest_parts_deepnest_paired(
                    normal_parts, width_mm, height_mm, effective_thickness,
                    unit=unit.value, trials=trials, seed=seed,
                    rotations=profile.rotation,
                    arbitrary_rotation=profile.arbitrary_rotation,
                    first_part_left_edge=first_left,
                    rotation_step=15.0 if quick else 5.0))
            else:
                results.append(nest_parts_deepnest(
                    normal_parts, width_mm, height_mm, effective_thickness,
                    unit=unit.value, improve_budget=budget,
                    trials=trials, seed=seed, rotations=profile.rotation,
                    arbitrary_rotation=profile.arbitrary_rotation,
                    first_part_left_edge=first_left,
                    rotation_step=15.0 if quick else 5.0,
                    configs=QUICK_CONFIGS if quick else None))
        else:
            results.append(nest_parts(
                normal_parts, width_mm, height_mm, effective_thickness,
                unit=unit.value, improve_budget=budget,
                trials=trials, seed=seed, rotations=profile.rotation,
                first_part_left_edge=first_left,
                configs=QUICK_CONFIGS if quick else None))
    if special_parts:
        results.append(nest_special_parts(
            special_parts, special_w, special_h, effective_thickness,
            unit=unit.value, rotations=profile.rotation))

    result = combine_nesting_results(results)
    return (result, len(normal_parts), len(special_parts), min_sheets,
            special_pair_min_sheets)


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
        drawing_profile=None,
        profile: NestingProfile | None = None,
        confirm_sheet_count: bool = True,
        report_only: bool = False,
        special_size: tuple[float, float] | None = None,
        quick: bool = False,
        pairing: bool = False) -> None:
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
          f"align={profile.align_edges}, thickness={effective_thickness}mm, "
          f"process={PROCESSING_CLASS_HELP[profile.processing_class]}, "
          f"free_rotation={profile.arbitrary_rotation}")

    # ── 读取 DXF ──
    print(f"读取 DXF: {dxf_path}")
    if drawing_profile is None:
        drawing_profile = resolve_drawing_profile(dxf_path)
    if drawing_profile is not None:
        print(f"matched drawing profile: {drawing_profile.name} "
              f"(panel_layers={len(drawing_profile.panel_layers or ([drawing_profile.panel_layer] if drawing_profile.panel_layer else []))}, "
              f"number_layers={len(drawing_profile.number_layers)})")
        effective_panel_layers = (
            layers
            if layers is not None
            else (
                drawing_profile.panel_layers
                or ([drawing_profile.panel_layer]
                    if drawing_profile.panel_layer else None)
            )
        )
        effective_number_layers = drawing_profile.number_layers or None
        effective_label_pattern = drawing_profile.label_pattern or None
        effective_exclude_linetypes = drawing_profile.exclude_linetypes or None
    else:
        effective_panel_layers = layers
        effective_number_layers = None
        effective_label_pattern = None
        effective_exclude_linetypes = None

    if drawing_profile is not None:
        parts_data, _doc = read_dxf_with_profile(
            dxf_path,
            drawing_profile,
            panel_layers=effective_panel_layers,
            exclude_layers=exclude_layers,
        )
    else:
        parts_data, _doc = read_dxf(
            dxf_path,
            panel_layers=effective_panel_layers,
            exclude_layers=exclude_layers,
            exclude_linetypes=effective_exclude_linetypes,
            number_layers=effective_number_layers,
            label_pattern=effective_label_pattern,
        )
    if not parts_data:
        print("错误：未找到任何封闭图形。")
        sys.exit(1)

    parts = assign_numbers(parts_data, skip_unnumbered=skip_unnumbered)
    skipped_material_numbers = []
    if drawing_profile is not None and drawing_profile.material_group_enabled:
        parts, skipped_material_numbers = apply_material_grouping(parts, drawing_profile)
        if skipped_material_numbers:
            print(f"已跳过无材料前缀件 {len(skipped_material_numbers)} 个")
        group_counts = Counter(p.material_group for p in parts)
        print("材料分组: " + ", ".join(
            f"{key}: {value} ?" for key, value in sorted(group_counts.items())
        ))

    if not parts:
        print("错误：材料分组后没有可排板零件。")
        sys.exit(1)

    total_area = sum(p.area for p in parts)
    special_w, special_h = special_size if special_size else (None, None)

    stem = Path(dxf_path).stem
    out_dir = None
    if not report_only:
        out_dir = make_output_dir(dxf_path)
        numbered_original = out_dir / f"{stem}_numbered_原位.dxf"
        write_numbered_parts_dxf(parts, str(numbered_original),
                                 unit_system=unit.value)

    material_group_enabled = bool(
        drawing_profile is not None and drawing_profile.material_group_enabled
    )
    if material_group_enabled:
        groups = sorted({part.material_group for part in parts})
    else:
        groups = [None]

    if profile.uses_deepnest:
        print(f"开始排板... (DeepNest/BLF, trials={trials})")
    else:
        print(f"开始排板... ({trials} 轮 x {budget:.0f}s LNS)")

    t0 = time.time()
    group_results = []
    material_summary = []
    total_min_sheets = 0
    total_normal_parts = 0
    total_special_parts = 0
    total_special_pair_min_sheets = None

    for group in groups:
        group_parts = (
            [part for part in parts if part.material_group == group]
            if group is not None else parts
        )
        group_label = group or "ALL"
        group_result, normal_count, special_count, min_sheets, special_pair_min = (
            _nest_one_group(
                group_parts, width_mm, height_mm, special_w, special_h,
                effective_thickness, unit, profile, drawing_profile,
                trials, seed, budget, quick, pairing,
            )
        )
        group_results.append(group_result)
        total_min_sheets += min_sheets
        total_normal_parts += normal_count
        total_special_parts += special_count
        if special_pair_min is not None:
            total_special_pair_min_sheets = (
                (total_special_pair_min_sheets or 0) + special_pair_min
            )
        material_summary.append({
            "material_group": group_label,
            "part_count": len(group_parts),
            "sheets": group_result.total_sheets,
            "yield_rate": round(group_result.yield_rate, 2),
            "theoretical_min_sheets": min_sheets,
        })
        print(
            f"  {group_label}: {len(group_parts)} 件 -> "
            f"{group_result.total_sheets} 张大板（理论最少 {min_sheets} 张）"
        )

    result = combine_nesting_results(group_results)
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
    if special_w and special_h and total_special_parts:
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
        "theoretical_min_sheets": total_min_sheets,
        "special_pair_min_sheets": total_special_pair_min_sheets,
        "material_summary": material_summary,
        "elapsed_seconds": round(elapsed, 1),
        "validation_errors": len(errors),
        "profile": {
            "processing_class": profile.processing_class,
            "arbitrary_rotation": profile.arbitrary_rotation,
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

    if args.audit:
        run_audit(
            dxf_path,
            previous_state_path=args.previous_state,
            accept_issue_ids=_split_issue_ids(args.accept_issue),
            ignore_issue_ids=_split_issue_ids(args.ignore_issue),
            fixed_issue_ids=_split_issue_ids(args.mark_fixed),
        )
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

    run(dxf_path, width, height, args.thickness, unit,
        trials=args.trials, seed=args.seed, budget=args.budget,
        skip_unnumbered=not args.include_unnumbered,
        layers=layers, exclude_layers=exclude_layers,
        profile=profile,
        confirm_sheet_count=not args.no_confirm,
        report_only=args.report_only,
        special_size=parse_special_size(args.special_size),
        quick=args.quick,
        pairing=args.pairing)


if __name__ == "__main__":
    main()
