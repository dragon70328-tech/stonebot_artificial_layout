"""NestingRevision 物化、持久化与回滚。

每次 nest_parts 产出一个不可变 NestingRevision：完整请求快照
（ConfirmedDrawing + SheetSpec + ProcessPolicy + 优化器参数）、布局几何、
指标与算法签名。回滚 = 切换 active 指针并从布局重建 NestingResult，
历史版本不被覆盖。
"""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shapely import wkt as shapely_wkt

from src.artifact_store import ArtifactStore
from src.models import NestingResult, Part, Sheet
from src.nesting_contracts import (
    NestingRequest,
    NestingRevision,
    ProcessPolicy,
    SheetSpec,
)
from src.workflow import GroupNestingOutcome

if TYPE_CHECKING:
    from src.job_context import JobContext


def _environment_fingerprint() -> dict[str, str]:
    """认证血缘：布局只在同一算法版本 + 同一依赖环境下保证逐位一致。"""
    import ezdxf
    import shapely

    from src.nesting import ALGORITHM_VERSION

    return {
        "algorithm_version": ALGORITHM_VERSION,
        "python": platform.python_version(),
        "shapely": shapely.__version__,
        "ezdxf": ezdxf.__version__,
    }


def _drawing_profile_digest(ctx: "JobContext") -> str | None:
    """图纸画像内容摘要（含项目级覆盖）：画像变化 = 不同的排板血缘。"""
    profile = ctx.drawing_profile
    if profile is None:
        return None
    payload = {
        "name": getattr(profile, "name", None),
        "profile": (
            profile.to_dict() if hasattr(profile, "to_dict") else str(profile)
        ),
        "override": ctx.drawing_profile_override,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def request_content_digest(request: NestingRequest) -> str:
    """请求的内容摘要：只承诺内容血缘，运行期字段不进身份。

    `Contract.digest` 的 exclude 只作用于顶层字段，嵌套的
    ConfirmedDrawing 里的 `confirmed_at`（确认时刻）与
    `artifact_path`（本次落盘路径）会泄漏进摘要，导致同一输入
    每次运行都产生不同 request_id。这里显式剔除这两个运行期字段——
    内容由 `confirmed_digest` 内容寻址字段承载，身份不丢信息。
    """
    payload = request.model_dump(
        mode="json", exclude={"request_id"}, exclude_none=True
    )
    drawing = payload.get("confirmed_drawing")
    if isinstance(drawing, dict):
        drawing.pop("confirmed_at", None)
        drawing.pop("artifact_path", None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_process_policy(ctx: "JobContext") -> ProcessPolicy:
    """旧路径（无契约）下从 NestingProfile 推导工艺策略，如实记录。"""
    profile = ctx.nesting_profile
    if profile is not None and profile.uses_deepnest:
        return ProcessPolicy.waterjet(
            min_gap_mm=float(profile.min_gap or 0.0),
        )
    rotation = [
        float(angle)
        for angle in (profile.rotation if profile is not None else [0, 90, 180, 270])
    ]
    return ProcessPolicy.bridge(
        kerf_mm=0.0,
        min_gap_mm=float(profile.min_gap or 0.0) if profile is not None else 0.0,
        rotation_degrees=rotation,
    )


def build_nesting_request(ctx: "JobContext") -> NestingRequest:
    """以 ConfirmedDrawing 为唯一输入来源构建完整排板请求快照。"""
    if ctx.confirmed_drawing is None:
        raise ValueError(
            "build_nesting_request requires a confirmed drawing snapshot"
        )
    sheet = ctx.sheet_spec or SheetSpec.from_legacy(
        ctx.width_mm, ctx.height_mm, ctx.thickness_mm
    )
    process = ctx.process_policy or derive_process_policy(ctx)
    feedback = {
        "optimizer": {
            "trials": ctx.trials,
            "quick": ctx.quick,
            "certified": bool(getattr(ctx, "certified", False)),
            "pairing": ctx.pairing,
            "beam": ctx.beam,
            "beam_width": ctx.beam_width,
            "compress": ctx.compress,
            "rect_fast": ctx.rect_fast,
            "low_fill_rework": ctx.low_fill_rework,
            "low_fill_threshold": ctx.low_fill_threshold,
            "candidate_mode": ctx.candidate_mode,
        },
        # 认证血缘：算法版本 + 依赖环境 + 图纸画像摘要全部进 digest，
        # 任何一项变化都会产生不同的 request_id —— 结果只在同一
        # 血缘下承诺逐位一致。
        "environment": _environment_fingerprint(),
        "drawing_profile_digest": _drawing_profile_digest(ctx),
    }
    kwargs = {
        "request_id": "pending",
        "confirmed_drawing": ctx.confirmed_drawing,
        "sheet": sheet,
        "process": process,
        "feedback": feedback,
        "seed": ctx.seed,
        "budget_seconds": ctx.budget,
    }
    try:
        request = NestingRequest(**kwargs)
    except ValueError:
        # 记录用途：旧路径（无 ProcessPolicy，桥切 kerf=0）不满足契约的
        # 可执行校验，但排板已在旧口径下发生——如实记录，跳过 readiness。
        request = NestingRequest.model_construct(**kwargs)
    digest = request_content_digest(request)
    request.request_id = f"nreq-{digest[:16]}"
    return request


def layout_from_result(
    result: NestingResult,
    outcome: GroupNestingOutcome,
    *,
    template: bool,
) -> dict[str, Any]:
    """把 NestingResult 序列化为可完整重建的布局载荷。"""
    sheets = []
    for sheet in result.sheets:
        parts = []
        for part in sheet.parts:
            parts.append(
                {
                    "number": part.number,
                    "polygon_wkt": part.polygon.wkt,
                    "outer_polygon_wkt": part.outer_polygon.wkt,
                    "holes_wkt": [hole.wkt for hole in part.holes],
                    "material_group": part.material_group,
                    "packing_group": part.packing_group,
                    "material_column": part.material_column,
                    "original_number": part.original_number,
                    "group_id": part.group_id,
                    # 源曲线谱：回滚交付必须仍能写出真圆弧（机床直接加工）。
                    "outer_shape": part.outer_shape,
                    "outer_arcs": list(part.outer_arcs or []),
                    "hole_shapes": list(part.hole_shapes or []),
                    "outer_polyline": part.outer_polyline,
                    "outer_curve": part.outer_curve,
                    "source_outer_wkt": (
                        part.source_outer_polygon.wkt
                        if part.source_outer_polygon is not None
                        else None
                    ),
                    "placement_rot": float(
                        getattr(part, "placement_rot", 0.0) or 0.0
                    ),
                }
            )
        sheets.append(
            {
                "index": sheet.index,
                "width": sheet.width,
                "height": sheet.height,
                "thickness": sheet.thickness,
                "parts": parts,
            }
        )
    return {
        "unit": result.unit,
        "template": template,
        "sheets": sheets,
        "outcome": {
            "material_summary": outcome.material_summary,
            "total_min_sheets": outcome.total_min_sheets,
            "total_normal_parts": outcome.total_normal_parts,
            "total_special_parts": outcome.total_special_parts,
            "total_special_pair_min_sheets": (
                outcome.total_special_pair_min_sheets
            ),
            "elapsed_seconds": outcome.elapsed_seconds,
        },
    }


def _layout_digest(layout: dict[str, Any]) -> str:
    canonical = json.dumps(layout["sheets"], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_nesting_revision(
    ctx: "JobContext",
    request: NestingRequest,
) -> NestingRevision:
    """从当前 ctx.result/outcome 构建不可变排板版本。"""
    if ctx.result is None or ctx.nesting_outcome is None:
        raise ValueError("build_nesting_revision requires a nesting result")
    result = ctx.result
    outcome = ctx.nesting_outcome
    layout = layout_from_result(result, outcome, template=ctx.template_result)
    metrics = {
        "total_sheets": float(result.total_sheets),
        "total_parts": float(result.total_parts),
        "yield_rate": round(result.yield_rate, 4),
        "elapsed_seconds": round(float(outcome.elapsed_seconds), 3),
    }
    process = request.process
    # use_enum_values 下是 str；model_construct 兜底路径仍是枚举——两种都接。
    process_value = getattr(process.process, "value", process.process)
    algorithm = (
        f"template:{ctx.template_name or 'unknown'}" if ctx.template_result else
        f"{process_value}"
        f"|candidate={ctx.candidate_mode}"
        f"|pairing={ctx.pairing}"
        f"|beam={ctx.beam}"
        f"|rect_fast={ctx.rect_fast}"
        f"|low_fill_rework={ctx.low_fill_rework}"
    )
    identity = hashlib.sha256(
        (
            request_content_digest(request)
            + _layout_digest(layout)
            + algorithm
        ).encode("utf-8")
    ).hexdigest()
    kwargs = {
        "revision_id": f"nr-{identity[:16]}",
        "request": request,
        "algorithm": algorithm,
        "layout": layout,
        "metrics": metrics,
        "validation_errors": list(ctx.validation_errors),
    }
    try:
        return NestingRevision(**kwargs)
    except ValueError:
        # 与 build_nesting_request 同理：旧口径请求做嵌套校验时会触发
        # readiness 校验，记录用途跳过（内容已如实生成）。
        return NestingRevision.model_construct(**kwargs)


def result_from_revision(revision: NestingRevision) -> NestingResult:
    """从布局载荷重建 NestingResult（回滚交付用）。"""
    layout = revision.layout
    sheets: list[Sheet] = []
    for sheet_payload in layout["sheets"]:
        sheet = Sheet(
            index=int(sheet_payload["index"]),
            width=float(sheet_payload["width"]),
            height=float(sheet_payload["height"]),
            thickness=float(sheet_payload["thickness"]),
        )
        for part_payload in sheet_payload["parts"]:
            polygon = shapely_wkt.loads(part_payload["polygon_wkt"])
            part = Part(
                id=len(sheet.parts) + 1,
                number=part_payload["number"],
                polygon=polygon,
                outer_polygon=shapely_wkt.loads(
                    part_payload["outer_polygon_wkt"]
                ),
                holes=[
                    shapely_wkt.loads(hole_wkt)
                    for hole_wkt in part_payload["holes_wkt"]
                ],
                original_number=part_payload.get("original_number"),
                material_group=part_payload.get("material_group"),
                packing_group=part_payload.get("packing_group"),
                material_column=part_payload.get("material_column"),
                group_id=part_payload.get("group_id"),
                outer_shape=part_payload.get("outer_shape"),
                outer_arcs=list(part_payload.get("outer_arcs") or []),
                hole_shapes=list(part_payload.get("hole_shapes") or []),
                outer_polyline=part_payload.get("outer_polyline"),
                outer_curve=part_payload.get("outer_curve"),
                source_outer_polygon=(
                    shapely_wkt.loads(part_payload["source_outer_wkt"])
                    if part_payload.get("source_outer_wkt")
                    else None
                ),
                placement_rot=float(
                    part_payload.get("placement_rot", 0.0) or 0.0
                ),
            )
            sheet.parts.append(part)
        sheets.append(sheet)
    result = NestingResult(
        sheets=sheets,
        unit=layout["unit"],
        total_parts=sum(len(sheet.parts) for sheet in sheets),
        total_sheets=len(sheets),
        total_part_area=sum(
            part.area for sheet in sheets for part in sheet.parts
        ),
        total_sheet_area=sum(sheet.total_area for sheet in sheets),
    )
    return result


def outcome_from_revision(
    revision: NestingRevision,
    result: NestingResult,
) -> GroupNestingOutcome:
    """从布局载荷恢复报告所需的统计口径。"""
    outcome = revision.layout.get("outcome") or {}
    return GroupNestingOutcome(
        result=result,
        material_summary=outcome.get("material_summary", []),
        total_min_sheets=int(outcome.get("total_min_sheets", 0)),
        total_normal_parts=int(outcome.get("total_normal_parts", result.total_parts)),
        total_special_parts=int(outcome.get("total_special_parts", 0)),
        total_special_pair_min_sheets=outcome.get(
            "total_special_pair_min_sheets"
        ),
        elapsed_seconds=float(outcome.get("elapsed_seconds", 0.0)),
    )


def persist_nesting_revision(
    ctx: "JobContext",
    revision: NestingRevision,
) -> Path | None:
    """内容寻址落盘；无 output_dir 时跳过（仅内存版本链）。"""
    if ctx.output_dir is None:
        return None
    store = ArtifactStore(Path(ctx.output_dir) / "artifacts")
    ref = store.put_json(
        ctx.project_id or "adhoc",
        "nested_reported",
        revision.revision_id,
        revision.model_dump(mode="json"),
        stage_version=f"r{len(ctx.nesting_revisions)}",
    )
    return ref.path


def update_revision_after_postprocess(
    ctx: "JobContext",
    revision: NestingRevision,
) -> None:
    """后处理（推边/对齐）会原地移动几何：把交付态布局与校验结果回写
    到 active 版本，使版本记录始终等于"可交付的内容"。

    revision_id 不变——它标识这次排板运行；布局载荷在校验后更新为
    交付态，存储层按内容寻址另存新文件。
    """
    revision.layout = layout_from_result(
        ctx.result,
        ctx.nesting_outcome,
        template=ctx.template_result,
    )
    revision.layout["postprocess"] = {
        "postprocessed": True,
        "manufacturing_metrics": ctx.manufacturing_metrics,
        "kerf_metrics": ctx.kerf_metrics,
    }
    revision.validation_errors = list(ctx.validation_errors)
