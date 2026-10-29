"""Real deterministic handlers for the agent tool registry.

Each handler mutates only the supplied ``JobContext`` and returns a small,
JSON-safe result. Handlers do not talk to a model and do not perform stage
transitions; the orchestrator owns both of those concerns.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from src.constraints import PROFILE_MIN_SHEETS
from src.correction_contracts import CorrectionPlan
from src.drawing_corrections import apply_correction_plan
from src.drawing_profile import (
    DrawingProfile,
    analyze_drawing,
    audit_drawing,
    read_dxf_with_profile,
)
from src.dxf_reader import extract_closed_entity_candidates, read_dxf
from src.dxf_writer import write_nested_dxf, write_numbered_parts_dxf
from src.job_context import JobContext
from src.numbering import assign_numbers
from src.nesting_contracts import CuttingProcess
from src.postprocess import estimate_kerf_material, validate_kerf_material
from src.recognized_contract import parts_data_to_recognized_drawing
from src.template_nesting import match_template, nest_from_template
from src.units import UNIT_LABELS
from src.workflow import (
    GroupNestingOutcome,
    apply_material_grouping,
    assign_group_ids,
    build_report,
    nest_groups,
    postprocess_result,
    resolve_drawing_profile,
    validate_mixed_nesting,
)


def _set_input_path(ctx: JobContext, args: dict[str, Any]) -> str:
    path = str(Path(args.get("path") or ctx.dxf_path))
    if path:
        ctx.dxf_path = path
    if not ctx.project_id and path:
        ctx.project_id = Path(path).stem
    return path


def _analyze_drawing(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    path = _set_input_path(ctx, args)
    ctx.fingerprint = analyze_drawing(path)
    return {
        "path": path,
        "dxf_version": ctx.fingerprint.get("dxf_version"),
        "entity_counts": ctx.fingerprint.get("entity_counts", {}),
    }


def _match_profile(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    path = _set_input_path(ctx, args)
    if ctx.drawing_profile_override is not None:
        ctx.drawing_profile = DrawingProfile.from_dict(
            ctx.drawing_profile_override
        )
        ctx.drawing_profile_source = (
            ctx.drawing_profile_source or "project_override"
        )
    else:
        ctx.drawing_profile = resolve_drawing_profile(
            path, use_case_library=ctx.use_case_library
        )
        ctx.drawing_profile_source = (
            "matched_profile"
            if ctx.drawing_profile is not None
            else "none"
        )
    return {
        "matched": ctx.drawing_profile is not None,
        "profile": (
            ctx.drawing_profile.name if ctx.drawing_profile is not None else None
        ),
        "source": ctx.drawing_profile_source,
    }


def _read_dxf(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    path = _set_input_path(ctx, args)
    panel_layers = args.get("panel_layers")
    exclude_layers = args.get("exclude_layers")
    ctx.classified_candidates = []
    if ctx.drawing_profile is not None:
        parts_data, _doc = read_dxf_with_profile(
            path,
            ctx.drawing_profile,
            panel_layers=panel_layers,
            exclude_layers=exclude_layers,
            recognition_memory=ctx.recognition_memory,
            recognition_model=ctx.recognition_model,
            classification_sink=ctx.classified_candidates,
        )
    else:
        parts_data, _doc = read_dxf(
            path,
            panel_layers=panel_layers,
            exclude_layers=exclude_layers,
        )
    ctx.parts_data = parts_data
    ctx.closed_entities = extract_closed_entity_candidates(_doc)
    ctx.recognized_drawing = parts_data_to_recognized_drawing(
        parts_data,
        path,
        profile_name=(
            ctx.drawing_profile.name if ctx.drawing_profile is not None else None
        ),
    )
    return {
        "parts": len(parts_data),
        "recognized_drawing_id": ctx.recognized_drawing.drawing_id,
    }


def _audit_drawing(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    path = _set_input_path(ctx, args)
    if ctx.drawing_profile is None:
        return {"issues": 0, "skipped": True}
    ctx.audit_issues = audit_drawing(path, ctx.drawing_profile)
    return {"issues": len(ctx.audit_issues)}


def _text_entity_count(ctx: JobContext) -> int:
    counts = (ctx.fingerprint or {}).get("entity_counts", {})
    return sum(
        int(counts.get(entity_type, 0) or 0)
        for entity_type in ("TEXT", "MTEXT")
    )


def _matched_original_number_count(
    parts_data: list[dict[str, Any]],
) -> int:
    return sum(
        1
        for part in parts_data
        if str(part.get("original_number") or "").strip()
    )


def _assign_numbers(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.parts_data:
        raise ValueError("assign_numbers requires read_dxf to run first")
    ctx.skip_unnumbered = bool(args.get("skip_unnumbered", ctx.skip_unnumbered))
    matched_numbers = _matched_original_number_count(ctx.parts_data)
    text_entities = _text_entity_count(ctx)
    if (
        not ctx.skip_unnumbered
        and ctx.drawing_profile is None
        and matched_numbers == 0
        and text_entities > 0
    ):
        raise ValueError(
            "automatic numbering blocked: the drawing contains text labels "
            "but no panel received an original number; match a drawing "
            "profile or correct the closed panel geometry instead of "
            "treating arbitrary closed frames as panels"
        )
    parts = assign_numbers(
        ctx.parts_data,
        skip_unnumbered=ctx.skip_unnumbered,
    )
    if not parts:
        raise ValueError(
            "numbering blocked: 0 panels matched original numbers; provide a "
            "drawing profile or a DXF with valid closed, numbered panels"
        )
    skipped_material_numbers: list[str] = []
    if (
        ctx.drawing_profile is not None
        and ctx.drawing_profile.material_group_enabled
    ):
        parts, skipped_material_numbers = apply_material_grouping(
            parts, ctx.drawing_profile
        )
    group_mode = (
        ctx.nesting_profile.group_mode
        if ctx.nesting_profile is not None
        else None
    )
    parts = assign_group_ids(parts, ctx.drawing_profile, group_mode)
    ctx.parts = parts
    return {
        "parts": len(parts),
        "skipped_material_numbers": len(skipped_material_numbers),
        "skip_unnumbered": ctx.skip_unnumbered,
    }


def _confirm_numbering(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": args.get("decision"),
        "parts": len(ctx.parts) if ctx.parts is not None else 0,
    }


def _write_numbered_dxf(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.parts:
        raise ValueError(
            "write_numbered_dxf received no numbered panels; numbering must "
            "complete successfully before an in-place DXF can be written"
        )
    path = str(Path(args["path"]))
    write_numbered_parts_dxf(ctx.parts, path, unit_system=ctx.unit.value)
    ctx.numbered_dxf_path = Path(path)
    return {"path": path, "parts": len(ctx.parts)}


def _apply_drawing_corrections(
    ctx: JobContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    raw_plan = args.get("plan")
    if not isinstance(raw_plan, dict):
        raise ValueError("apply_drawing_corrections requires a plan object")
    plan = CorrectionPlan.model_validate(raw_plan)
    result = apply_correction_plan(ctx, plan)
    return result.model_dump(mode="json")


def _nest_parts(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.parts:
        raise ValueError("nest_parts requires assign_numbers first")
    profile = ctx.nesting_profile or PROFILE_MIN_SHEETS
    # 排板空间 = 可利用尺寸。契约（SheetSpec.usable_*）是权威来源；工具参数
    # 只作为没有契约时的回退（旧路径）。
    spec = ctx.sheet_spec
    if spec is not None:
        ctx.width_mm = float(spec.usable_width_mm)
        ctx.height_mm = float(spec.usable_height_mm)
    else:
        ctx.width_mm = float(args["sheet_width"])
        ctx.height_mm = float(args["sheet_height"])
    ctx.thickness_mm = float(args.get("thickness", ctx.thickness_mm))
    ctx.trials = int(args.get("trials", ctx.trials))
    ctx.seed = int(args.get("seed", ctx.seed))
    ctx.budget = float(args.get("budget", ctx.budget))
    ctx.quick = bool(args.get("quick", ctx.quick))
    ctx.pairing = bool(args.get("pairing", ctx.pairing))
    rect_fast = bool(args.get("rect_fast", True))
    low_fill_rework = bool(args.get("low_fill_rework", False))
    low_fill_threshold = float(args.get("low_fill_threshold", 0.6))
    candidate_mode = str(args.get("candidate_mode", "bbox")).casefold()
    if not 0.0 < low_fill_threshold <= 1.0:
        raise ValueError("low_fill_threshold must be in (0, 1]")
    if candidate_mode not in {"bbox", "nfp_lite"}:
        raise ValueError("candidate_mode must be bbox or nfp_lite")
    if low_fill_rework and not profile.uses_deepnest:
        raise ValueError("low_fill_rework requires waterjet or laser")
    if candidate_mode != "bbox" and not profile.uses_deepnest:
        raise ValueError("non-bbox candidate_mode requires waterjet or laser")
    if low_fill_rework and ctx.pairing:
        raise ValueError("low_fill_rework cannot be combined with pairing")

    groups: list[str | None]
    if (
        ctx.drawing_profile is not None
        and ctx.drawing_profile.material_group_enabled
    ):
        groups = sorted({part.material_group for part in ctx.parts})
    else:
        groups = [None]

    template_record = None
    if ctx.use_template and groups == [None] and not profile.uses_deepnest:
        template_record = match_template(
            ctx.dxf_path,
            ctx.width_mm,
            ctx.height_mm,
        )
        if template_record is not None:
            template_result = nest_from_template(ctx.parts, template_record)
            template_errors = validate_mixed_nesting(
                template_result, min_gap=profile.min_gap
            )
            if not template_errors:
                total_min_sheets = math.ceil(
                    template_result.total_part_area
                    / (ctx.width_mm * ctx.height_mm)
                )
                ctx.nesting_outcome = GroupNestingOutcome(
                    result=template_result,
                    material_summary=[
                        {
                            "material_group": "ALL",
                            "part_count": len(ctx.parts),
                            "sheets": template_result.total_sheets,
                            "yield_rate": round(template_result.yield_rate, 2),
                            "theoretical_min_sheets": total_min_sheets,
                        }
                    ],
                    total_min_sheets=total_min_sheets,
                    total_normal_parts=len(ctx.parts),
                    total_special_parts=0,
                    total_special_pair_min_sheets=None,
                    elapsed_seconds=0.0,
                )
                ctx.result = template_result
                ctx.template_result = True
                return {
                    "total_sheets": ctx.result.total_sheets,
                    "total_parts": ctx.result.total_parts,
                    "yield_rate": round(ctx.result.yield_rate, 2),
                    "elapsed_seconds": 0.0,
                    "template": template_record.name,
                }

    outcome = nest_groups(
        ctx.parts,
        groups,
        ctx.width_mm,
        ctx.height_mm,
        None,
        None,
        ctx.thickness_mm,
        ctx.unit,
        profile,
        ctx.drawing_profile,
        ctx.trials,
        ctx.seed,
        ctx.budget,
        ctx.quick,
        ctx.pairing,
        beam=bool(args.get("beam", True)),
        beam_width=(
            int(args["beam_width"]) if args.get("beam_width") is not None
            else None
        ),
        compress=bool(args.get("compress", True)),
        rect_fast=rect_fast,
        low_fill_rework=low_fill_rework,
        low_fill_threshold=low_fill_threshold,
        candidate_mode=candidate_mode,
    )
    ctx.nesting_outcome = outcome
    ctx.result = outcome.result
    return {
        "total_sheets": ctx.result.total_sheets,
        "total_parts": ctx.result.total_parts,
        "yield_rate": round(ctx.result.yield_rate, 2),
        "elapsed_seconds": round(outcome.elapsed_seconds, 1),
    }


def _postprocess(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.result is None:
        raise ValueError("postprocess requires nest_parts first")
    profile = ctx.nesting_profile or PROFILE_MIN_SHEETS
    if ctx.template_result:
        ctx.manufacturing_metrics = {
            "edge_contact_mm": 0.0,
            "through_cut_mm": 0.0,
        }
    else:
        ctx.manufacturing_metrics = postprocess_result(ctx.result, profile)
    # 几何校验 + 桥切刀缝材料校验（口径与 CLI 相同：允许 = 可利用 + 让尺）。
    # ctx.validation_errors 是"阻塞出图"的总集，write_dxf 会据此拒绝写文件。
    ctx.validation_errors = validate_mixed_nesting(
        ctx.result, min_gap=profile.min_gap
    )
    kerf_errors = _kerf_material_errors(ctx)
    ctx.validation_errors.extend(kerf_errors)
    return {
        "geometry_errors": len(ctx.validation_errors),
        "kerf_material_errors": len(kerf_errors),
        "manufacturability": ctx.manufacturing_metrics,
    }


def _kerf_material_params(ctx: JobContext):
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


def _kerf_material_errors(ctx: JobContext) -> list[str]:
    """桥切刀缝材料校验；同时把指标写进 ctx 供报告使用。"""
    params = _kerf_material_params(ctx)
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


def _write_dxf(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.result is None:
        raise ValueError("write_dxf requires postprocess first")
    if ctx.validation_errors:
        raise ValueError(
            "validation failed (geometry/kerf, "
            f"{len(ctx.validation_errors)} 处); final DXF is blocked"
        )
    path = str(Path(args["path"]))
    write_nested_dxf(ctx.result, path, unit_system=ctx.unit.value)
    ctx.nested_dxf_path = Path(path)
    return {"path": path, "sheets": ctx.result.total_sheets}


def _write_report(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.result is None or ctx.nesting_outcome is None:
        raise ValueError("write_report requires nest_parts first")
    path = str(Path(args["path"]))
    outcome = ctx.nesting_outcome
    spec = ctx.sheet_spec
    if spec is not None:
        # 口径三层：标称 / 让尺（标称之外的实物余量）/ 可利用尺寸。
        nominal_size = (spec.nominal_width_mm, spec.nominal_height_mm)
        usable_size = (spec.usable_width_mm, spec.usable_height_mm)
        sheet_oversize = (
            spec.oversize_added_width_mm,
            spec.oversize_added_height_mm,
        )
        usable_includes_oversize = usable_size[0] > spec.nominal_width_mm
    else:
        nominal_size = (ctx.width_mm, ctx.height_mm)
        usable_size = (ctx.width_mm, ctx.height_mm)
        sheet_oversize = None
        usable_includes_oversize = False
    report = build_report(
        ctx.result,
        ctx.nesting_profile or PROFILE_MIN_SHEETS,
        nominal_size[0],
        nominal_size[1],
        None,
        None,
        ctx.thickness_mm,
        UNIT_LABELS[ctx.unit],
        outcome.total_min_sheets,
        outcome.total_special_pair_min_sheets,
        outcome.material_summary,
        outcome.elapsed_seconds,
        ctx.validation_errors,
        ctx.manufacturing_metrics,
        ctx.trials,
        ctx.seed,
        ctx.budget,
        ctx.skip_unnumbered,
        kerf_metrics=ctx.kerf_metrics,
        sheet_oversize=sheet_oversize,
        usable_size=usable_size,
        usable_includes_oversize=usable_includes_oversize,
    )
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ctx.report_path = Path(path)
    return {"path": path, "total_sheets": ctx.result.total_sheets}


def build_tool_handlers(ctx: JobContext | None = None) -> dict[str, Any]:
    """Return the real handler registry bound to one job context."""
    context = ctx or JobContext()
    return {
        "analyze_drawing": lambda args: _analyze_drawing(context, args),
        "match_profile": lambda args: _match_profile(context, args),
        "read_dxf": lambda args: _read_dxf(context, args),
        "audit_drawing": lambda args: _audit_drawing(context, args),
        "assign_numbers": lambda args: _assign_numbers(context, args),
        "confirm_numbering": lambda args: _confirm_numbering(context, args),
        "write_numbered_dxf": lambda args: _write_numbered_dxf(context, args),
        "apply_drawing_corrections": lambda args: _apply_drawing_corrections(context, args),
        "nest_parts": lambda args: _nest_parts(context, args),
        "postprocess": lambda args: _postprocess(context, args),
        "write_dxf": lambda args: _write_dxf(context, args),
        "write_report": lambda args: _write_report(context, args),
    }
