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
from src.confirmed_drawing import (
    build_confirmed_drawing,
    parts_signature,
    persist_confirmed_drawing,
)
from src.correction_contracts import CorrectionPlan
from src.drawing_corrections import apply_correction_plan
from src.drawing_profile import (
    DrawingProfile,
    analyze_drawing,
    audit_drawing,
    curve_fidelity_issues,
    machine_readiness_drawing_issues,
    read_dxf_with_profile,
)
from src.dxf_reader import extract_closed_entity_candidates, read_dxf
from src.dxf_writer import write_nested_dxf, write_numbered_parts_dxf
from src.job_context import JobContext
from src.numbering import assign_numbers
from src.nesting_revisions import (
    build_nesting_request,
    build_nesting_revision,
    outcome_from_revision,
    persist_nesting_revision,
    result_from_revision,
    update_revision_after_postprocess,
)
from src.nesting_readback import sheets_payload_from_result, verify_nested_dxf
from src.nesting_validation import run_full_validation
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
from src.saw_process import diagnose_saw_access
from src.saw_repair import repair_nesting_result_saw_clearance


def _saw_clearance_mm(ctx: JobContext) -> float | None:
    policy = ctx.process_policy
    if policy is None or policy.circular_saw is None:
        return None
    return policy.circular_saw.clearance_for_thickness(
        ctx.thickness_mm,
    ).clearance_mm


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
        # 没有命中画像时规则审图跳过，但"曲线没能保真复写"是通用
        # 安全问题，必须照常报出来。
        ctx.audit_issues = curve_fidelity_issues(ctx.parts_data or [])
        import ezdxf

        try:
            doc = ezdxf.readfile(str(path))
        except Exception:  # noqa: BLE001 - 读不回就只保留曲线问题
            doc = None
        if doc is not None:
            regions = [
                part.get("outer_polygon") or part.get("polygon")
                for part in (ctx.parts_data or [])
            ]
            ctx.audit_issues.extend(
                machine_readiness_drawing_issues(doc, regions=regions)
            )
        return {"issues": len(ctx.audit_issues), "profile_audit_skipped": True}
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
    result: dict[str, Any] = {
        "decision": args.get("decision"),
        "parts": len(ctx.parts) if ctx.parts is not None else 0,
    }
    decision = str(args.get("decision") or "").strip().lower()
    if decision not in {"confirm", "confirmed", "yes", "y"}:
        return result
    # 确认动作物化为不可变快照：排板阶段的唯一输入来源。
    drawing = build_confirmed_drawing(ctx)
    ctx.confirmed_drawing = drawing
    ctx.confirmed_drawing_digest = drawing.confirmed_digest
    ctx.confirmed_parts_signature = parts_signature(ctx.parts)
    ctx.confirmed_drawing_path = persist_confirmed_drawing(ctx, drawing)
    result["confirmed_drawing"] = {
        "confirmed_id": drawing.confirmed_id,
        "revision_id": drawing.revision_id,
        "revision_no": ctx.confirmed_revision_no,
        "digest": drawing.confirmed_digest,
        "panels": len(drawing.panels),
        "path": (
            str(ctx.confirmed_drawing_path)
            if ctx.confirmed_drawing_path is not None
            else None
        ),
    }
    return result


def _write_numbered_dxf(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.parts:
        raise ValueError(
            "write_numbered_dxf received no numbered panels; numbering must "
            "complete successfully before an in-place DXF can be written"
        )
    path = str(Path(args["path"]))
    write_numbered_parts_dxf(
        ctx.parts,
        path,
        unit_system=ctx.unit.value,
        source_path=str(ctx.dxf_path) if ctx.dxf_path else None,
    )
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
    payload = result.model_dump(mode="json")
    if ctx.confirmed_drawing is not None and ctx.parts:
        # 确认后的修正会使既有快照失配；nest_parts 会据此拒绝排板，
        # 这里先把"需要重新确认"显式暴露给调用方。
        payload["confirmed_drawing_stale"] = (
            parts_signature(ctx.parts) != ctx.confirmed_parts_signature
        )
    return payload


def _record_nesting_revision(ctx: JobContext) -> dict[str, Any]:
    """把本次排板结果固化为不可变 NestingRevision 并置为 active。"""
    request = build_nesting_request(ctx)
    revision = build_nesting_revision(ctx, request)
    path = persist_nesting_revision(ctx, revision)
    ctx.nesting_revisions.append(revision)
    ctx.active_revision_id = revision.revision_id
    if path is not None:
        ctx.nesting_revision_paths[revision.revision_id] = str(path)
    # 新版本生效后，旧的校验/后处理状态全部作废，必须重新 postprocess。
    ctx.postprocess_revision_id = None
    ctx.validation_errors = []
    return {
        "revision_id": revision.revision_id,
        "request_id": revision.request.request_id,
        "algorithm": revision.algorithm,
        "path": str(path) if path is not None else None,
    }


def _nest_parts(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if not ctx.parts:
        raise ValueError("nest_parts requires assign_numbers first")
    # 排板只消费"用户确认过的快照"：没有快照或确认后零件被改动都 fail
    # closed，要求重新确认，避免把未核对的内容送上大板。
    if ctx.confirmed_drawing is None or ctx.confirmed_parts_signature is None:
        raise ValueError(
            "nest_parts requires confirm_numbering first: the confirmed "
            "drawing snapshot is the only allowed nesting input"
        )
    current_signature = parts_signature(ctx.parts)
    if current_signature != ctx.confirmed_parts_signature:
        raise ValueError(
            "parts changed since numbering confirmation "
            f"(confirmed r{ctx.confirmed_revision_no}, digest "
            f"{ctx.confirmed_drawing_digest}); re-run confirm_numbering "
            "before nesting"
        )
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
    # 认证确定性模式：解除排板链路的一切墙钟分支，同输入同环境必得
    # 同结果；默认关闭，探索性用法（墙钟预算/并行）行为不变。
    ctx.certified = bool(args.get("certified", ctx.certified))
    ctx.pairing = bool(args.get("pairing", ctx.pairing))
    if ctx.certified and profile.uses_deepnest:
        raise ValueError(
            "certified mode currently supports bridge process only; "
            "waterjet/laser (deepnest) is outside the certified envelope"
        )
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
        groups = sorted({
            getattr(part, "packing_group", None) or part.material_group
            for part in ctx.parts
        })
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
                ctx.template_name = template_record.name
                revision_info = _record_nesting_revision(ctx)
                return {
                    "total_sheets": ctx.result.total_sheets,
                    "total_parts": ctx.result.total_parts,
                    "yield_rate": round(ctx.result.yield_rate, 2),
                    "elapsed_seconds": 0.0,
                    "template": template_record.name,
                    # 模板复刻天然逐位确定；certified 只承诺引擎路径。
                    "certified": False,
                    "revision": revision_info,
                }

    optimizer_stats: dict[str, Any] = {}
    saw_clearance_mm = _saw_clearance_mm(ctx)
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
        stats=optimizer_stats,
        beam=bool(args.get("beam", True)),
        beam_width=(
            int(args["beam_width"]) if args.get("beam_width") is not None
            else None
        ),
        compress=bool(args.get("compress", True)),
        rect_fast=rect_fast,
        certified=ctx.certified,
        low_fill_rework=low_fill_rework,
        low_fill_threshold=low_fill_threshold,
        candidate_mode=candidate_mode,
        saw_clearance_mm=saw_clearance_mm,
    )
    ctx.nesting_outcome = outcome
    ctx.result = outcome.result
    revision_info = _record_nesting_revision(ctx)
    output = {
        "total_sheets": ctx.result.total_sheets,
        "total_parts": ctx.result.total_parts,
        "yield_rate": round(ctx.result.yield_rate, 2),
        "elapsed_seconds": round(outcome.elapsed_seconds, 1),
        "certified": bool(optimizer_stats.get("certified")),
        "revision": revision_info,
    }
    hazards = optimizer_stats.get("determinism_hazards")
    if hazards:
        output["determinism_hazards"] = hazards
    return output


def _postprocess(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.result is None:
        raise ValueError("postprocess requires nest_parts first")
    profile = ctx.nesting_profile or PROFILE_MIN_SHEETS
    if ctx.template_result:
        ctx.manufacturing_metrics = {
            "edge_contact_mm": 0.0,
            "through_cut_mm": 0.0,
        }
        saw_clearance_mm = _saw_clearance_mm(ctx)
        if saw_clearance_mm is not None:
            ctx.manufacturing_metrics["saw_access"] = diagnose_saw_access(
                ctx.result.sheets,
                saw_clearance_mm,
            ).as_dict()
    else:
        ctx.manufacturing_metrics = postprocess_result(
            ctx.result,
            profile,
            saw_clearance_mm=_saw_clearance_mm(ctx),
        )
    saw_clearance_mm = _saw_clearance_mm(ctx)
    if saw_clearance_mm is not None:
        saw_repair = repair_nesting_result_saw_clearance(
            ctx.result,
            clearance_mm=saw_clearance_mm,
            min_gap_mm=profile.min_gap,
            max_moves_per_sheet=12,
        )
        ctx.manufacturing_metrics["saw_repair"] = saw_repair.as_dict()
        ctx.manufacturing_metrics["saw_access"] = diagnose_saw_access(
            ctx.result.sheets,
            saw_clearance_mm,
        ).as_dict()
    # 统一验证器：完整性/编号唯一/几何/材料不共板/孔洞跟随/刀缝材料。
    # ctx.validation_errors 是"阻塞出图"的总集，write_dxf 会据此拒绝写文件。
    categories = run_full_validation(ctx)
    ctx.validation_errors = list(categories["all"])
    # 校验结果归档到当前生效的排板版本，并标记该版本已完成校验。
    ctx.postprocess_revision_id = ctx.active_revision_id
    if ctx.active_revision_id is not None:
        for revision in ctx.nesting_revisions:
            if revision.revision_id == ctx.active_revision_id:
                update_revision_after_postprocess(ctx, revision)
                repersisted = persist_nesting_revision(ctx, revision)
                if repersisted is not None:
                    ctx.nesting_revision_paths[revision.revision_id] = str(
                        repersisted
                    )
                break
    return {
        "geometry_errors": len(categories["geometry"]),
        "kerf_material_errors": len(categories["kerf_material"]),
        "completeness_errors": len(categories["completeness"]),
        "validation_categories": {
            name: len(errors)
            for name, errors in categories.items()
            if name != "all"
        },
        "manufacturability": ctx.manufacturing_metrics,
    }


def _write_dxf(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.result is None:
        raise ValueError("write_dxf requires postprocess first")
    active_revision = None
    if (
        ctx.active_revision_id is not None
        and ctx.postprocess_revision_id != ctx.active_revision_id
    ):
        raise ValueError(
            "write_dxf requires postprocess on the active nesting revision "
            f"({ctx.active_revision_id}); run postprocess before writing"
        )
    elif ctx.active_revision_id is not None:
        active_revision = next(
            (
                item
                for item in ctx.nesting_revisions
                if item.revision_id == ctx.active_revision_id
            ),
            None,
        )
    if ctx.validation_errors:
        raise ValueError(
            "validation failed (geometry/kerf, "
            f"{len(ctx.validation_errors)} 处); final DXF is blocked"
        )
    path = str(Path(args["path"]))
    # 交付永远以 active 版本记录为准（含后处理后的交付态几何）；
    # 无版本记录的旧路径回退到 ctx.result。
    deliverable = (
        result_from_revision(active_revision)
        if active_revision is not None
        else ctx.result
    )
    write_nested_dxf(deliverable, path, unit_system=ctx.unit.value)
    # 回读门禁：写出的 DXF 必须能读回且与期望布局逐件一致，
    # 否则删除文件并拒绝交付（fail closed）。
    expected_sheets = (
        active_revision.layout["sheets"]
        if active_revision is not None
        else sheets_payload_from_result(ctx.result)
    )
    readback_errors = verify_nested_dxf(path, expected_sheets)
    if readback_errors:
        Path(path).unlink(missing_ok=True)
        raise ValueError(
            "write_dxf read-back verification failed "
            f"({len(readback_errors)} 处), file removed: "
            + "; ".join(readback_errors[:3])
        )
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
        saw_clearance=(
            ctx.process_policy.circular_saw.clearance_for_thickness(
                ctx.thickness_mm
            )
            if (
                ctx.process_policy is not None
                and ctx.process_policy.circular_saw is not None
            )
            else None
        ),
    )
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ctx.report_path = Path(path)
    return {"path": path, "total_sheets": ctx.result.total_sheets}


def _rollback_nesting(ctx: JobContext, args: dict[str, Any]) -> dict[str, Any]:
    """把 active 排板版本切换到历史版本；历史版本不被覆盖。"""
    revision_id = str(args.get("revision_id") or "").strip()
    if not revision_id:
        raise ValueError("rollback_nesting requires revision_id")
    revision = next(
        (
            item
            for item in ctx.nesting_revisions
            if item.revision_id == revision_id
        ),
        None,
    )
    if revision is None:
        known = [item.revision_id for item in ctx.nesting_revisions]
        raise ValueError(
            f"unknown nesting revision: {revision_id}; known: {known}"
        )
    # 图纸修正后，旧版本的布局是针对旧几何排的：回滚会交付一张
    # 与当前确认图纸不一致的排板图。按确认摘要拦截。
    revision_digest = (
        getattr(revision.request.confirmed_drawing, "confirmed_digest", None)
        if revision.request.confirmed_drawing is not None
        else None
    )
    if (
        ctx.confirmed_drawing_digest
        and revision_digest
        and revision_digest != ctx.confirmed_drawing_digest
    ):
        raise ValueError(
            f"revision {revision_id} belongs to a different confirmed "
            f"drawing (revision {revision_digest[:12]}, current "
            f"{ctx.confirmed_drawing_digest[:12]}); the drawing was "
            "corrected after this layout was nested, so it cannot be "
            "rolled back — re-nest the corrected drawing instead"
        )
    result = result_from_revision(revision)
    ctx.result = result
    ctx.nesting_outcome = outcome_from_revision(revision, result)
    ctx.active_revision_id = revision.revision_id
    ctx.template_result = bool(revision.layout.get("template"))
    # 版本记录携带自己的校验状态：已校验的版本回滚后可直接交付；
    # 未校验的必须重新 postprocess。旧产物路径一律作废。
    postprocess_state = revision.layout.get("postprocess") or {}
    validated = bool(postprocess_state.get("postprocessed"))
    ctx.postprocess_revision_id = revision.revision_id if validated else None
    ctx.validation_errors = (
        list(revision.validation_errors) if validated else []
    )
    ctx.manufacturing_metrics = (
        dict(postprocess_state.get("manufacturing_metrics") or {})
        if validated
        else {}
    )
    ctx.kerf_metrics = (
        postprocess_state.get("kerf_metrics") if validated else None
    )
    ctx.nested_dxf_path = None
    ctx.report_path = None
    return {
        "revision_id": revision.revision_id,
        "algorithm": revision.algorithm,
        "total_sheets": result.total_sheets,
        "total_parts": result.total_parts,
        "yield_rate": round(result.yield_rate, 2),
        "requires_postprocess": not validated,
        "validation_errors": len(ctx.validation_errors),
        "history": [
            {
                "revision_id": item.revision_id,
                "algorithm": item.algorithm,
                "sheets": item.metrics.get("total_sheets"),
                "yield_rate": item.metrics.get("yield_rate"),
                "active": item.revision_id == ctx.active_revision_id,
            }
            for item in ctx.nesting_revisions
        ],
    }


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
        "rollback_nesting": lambda args: _rollback_nesting(context, args),
        "postprocess": lambda args: _postprocess(context, args),
        "write_dxf": lambda args: _write_dxf(context, args),
        "write_report": lambda args: _write_report(context, args),
    }
