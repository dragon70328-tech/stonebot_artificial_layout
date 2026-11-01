"""统一排板验证器：所有交付前校验的唯一入口。

此前校验分散在 nest_parts（件数）、postprocess（几何+刀缝）等处，
口径容易漂移。现在统一为 `run_full_validation(ctx)`：
完整性 / 编号唯一 / 几何 / 材料不共板 / 孔洞跟随 / 刀缝材料。
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from src.constraints import PROFILE_MIN_SHEETS
from src.nesting_contracts import CuttingProcess
from src.postprocess import estimate_kerf_material, validate_kerf_material
from src.workflow import validate_mixed_nesting

if TYPE_CHECKING:
    from src.job_context import JobContext


def kerf_material_params(ctx: "JobContext"):
    """返回 (kerf_mm, allowance_x, allowance_y)；不适用时为 None。

    口径（与 CLI 的 `resolve_sheet_caliber` 一致）：刀缝校验允许尺寸 = 实物
    尺寸 = 可利用尺寸 + 让尺（让尺只计一次）。契约里可利用尺寸是
    `SheetSpec.usable_*`，让尺是 `SheetSpec.oversize_added_*`。
    水刀/激光按口径"刀缝可不计"，不走这项校验。
    """
    policy = ctx.process_policy
    spec = ctx.sheet_spec
    if policy is None or ctx.result is None:
        return None
    if policy.process != CuttingProcess.BRIDGE:
        return None
    kerf_mm = float(policy.kerf_mm or 0.0)
    if kerf_mm <= 0:
        return None
    if spec is None:
        return (kerf_mm, 0.0, 0.0)
    return (
        kerf_mm,
        float(spec.nominal_width_mm + spec.oversize_added_width_mm
              - spec.usable_width_mm),
        float(spec.nominal_height_mm + spec.oversize_added_height_mm
              - spec.usable_height_mm),
    )


def kerf_material_errors(ctx: "JobContext") -> list[str]:
    """桥切刀缝材料校验；同时把指标写进 ctx 供报告使用。"""
    params = kerf_material_params(ctx)
    if params is None:
        ctx.kerf_metrics = None
        return []
    kerf_mm, allowance_x, allowance_y = params
    ctx.kerf_metrics = estimate_kerf_material(ctx.result.sheets, kerf_mm)
    return validate_kerf_material(
        ctx.result.sheets,
        kerf_mm,
        allowance_x_mm=allowance_x,
        allowance_y_mm=allowance_y,
    )


def _completeness_errors(ctx: "JobContext") -> list[str]:
    """确认快照里的每个零件都必须出现在排板结果中（多重集比对）。"""
    if not ctx.parts or ctx.result is None:
        return []
    expected = Counter(part.number for part in ctx.parts)
    placed = Counter(
        part.number
        for sheet in ctx.result.sheets
        for part in sheet.parts
    )
    if expected == placed:
        return []
    missing = expected - placed
    extra = placed - expected
    return [
        "完整性校验失败：排板结果与确认快照不一致"
        f"（缺少 {sum(missing.values())} 件：{sorted(missing)[:5]}；"
        f"多出 {sum(extra.values())} 件：{sorted(extra)[:5]}）"
    ]


def _uniqueness_errors(ctx: "JobContext") -> list[str]:
    if ctx.result is None:
        return []
    numbers = Counter(
        part.number
        for sheet in ctx.result.sheets
        for part in sheet.parts
    )
    duplicates = sorted(n for n, count in numbers.items() if count > 1)
    if not duplicates:
        return []
    return [
        f"编号唯一性校验失败：{len(duplicates)} 个编号重复"
        f"（{duplicates[:5]}）"
    ]


def _material_purity_errors(ctx: "JobContext") -> list[str]:
    """启用材料分组的画像：不同材料绝不共板。"""
    profile = ctx.drawing_profile
    if (
        profile is None
        or not getattr(profile, "material_group_enabled", False)
        or ctx.result is None
    ):
        return []
    errors = []
    for sheet in ctx.result.sheets:
        groups = sorted(
            {str(part.material_group) for part in sheet.parts}
        )
        if len(groups) > 1:
            errors.append(
                f"材料分组校验失败：第 {sheet.index} 张板混入多种材料 "
                f"{groups}"
            )
    return errors


def _holes_follow_errors(ctx: "JobContext") -> list[str]:
    """每个已排零件携带的孔洞数必须与确认快照一致。"""
    if not ctx.parts or ctx.result is None:
        return []
    expected_holes = {part.number: len(part.holes) for part in ctx.parts}
    errors = []
    for sheet in ctx.result.sheets:
        for part in sheet.parts:
            expected = expected_holes.get(part.number)
            if expected is not None and len(part.holes) != expected:
                errors.append(
                    f"孔洞跟随校验失败：零件 {part.number} 孔洞数 "
                    f"{len(part.holes)} != 确认快照 {expected}"
                )
    return errors


def run_full_validation(ctx: "JobContext") -> dict[str, list[str]]:
    """统一交付校验，返回分类问题清单（all 为总集）。"""
    if ctx.result is None:
        return {"all": ["validate requires nest_parts first"]}
    profile = ctx.nesting_profile or PROFILE_MIN_SHEETS
    geometry = validate_mixed_nesting(ctx.result, min_gap=profile.min_gap)
    kerf = kerf_material_errors(ctx)
    categories: dict[str, list[str]] = {
        "completeness": _completeness_errors(ctx),
        "uniqueness": _uniqueness_errors(ctx),
        "geometry": geometry,
        "material_purity": _material_purity_errors(ctx),
        "holes_follow": _holes_follow_errors(ctx),
        "kerf_material": kerf,
    }
    all_errors: list[str] = []
    for errors in categories.values():
        all_errors.extend(errors)
    categories["all"] = all_errors
    return categories
