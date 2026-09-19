"""Build compact learning feedback from an applied DXF correction revision."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from src.correction_contracts import CorrectionPlan
from src.job_context import JobContext
from src.recognition_contracts import (
    CandidateSnapshot,
    FeedbackScope,
    RecognitionFeedback,
)
from src.recognition_memory import save_recognition_memory, update_recognition_memory


def _digest_file(path: str | Path) -> str:
    candidate = Path(path)
    if candidate.exists() and candidate.is_file():
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    return hashlib.sha256(str(candidate).encode("utf-8")).hexdigest()


def _data_by_id(parts_data: list[dict[str, Any]] | None) -> dict[int, dict[str, Any]]:
    return {
        int(item["index"]): item
        for item in (parts_data or [])
        if item.get("index") is not None
    }


def _part_signature(part: Any) -> tuple[Any, ...]:
    return (
        part.number,
        part.original_number,
        part.material_group,
        part.outer_polygon.wkt,
        tuple(hole.wkt for hole in part.holes),
    )


def _panel_snapshot(
    part: Any,
    data: dict[str, Any] | None,
) -> CandidateSnapshot:
    data = data or {}
    return CandidateSnapshot(
        target_id=str(part.id),
        kind="panel",
        entity_handle=part.outer_handle,
        layer=data.get("layer"),
        number_layer=data.get("number_layer"),
        entity_type=data.get("entity_type") or data.get("source"),
        number=part.number,
        material_group=part.material_group,
        area=float(part.area),
        bounds=[float(value) for value in part.outer_polygon.bounds],
        geometry_wkt=part.outer_polygon.wkt,
        classification=data.get("classification"),
        source=data.get("source"),
    )


def _closed_entities_by_handle(ctx: JobContext) -> dict[str, dict[str, Any]]:
    return {
        str(item["handle"]): item
        for item in getattr(ctx, "closed_entities", [])
        if item.get("handle")
    }


def _hole_snapshot(
    part: Any,
    hole_index: int,
    data: dict[str, Any] | None,
    closed_entities: dict[str, dict[str, Any]],
) -> CandidateSnapshot:
    data = data or {}
    hole = part.holes[hole_index]
    handle = (
        part.hole_handles[hole_index]
        if hole_index < len(part.hole_handles)
        else None
    )
    target_id = f"handle:{handle}" if handle else f"p{part.id}:h{hole_index}"
    candidate = closed_entities.get(str(handle)) if handle else None
    return CandidateSnapshot(
        target_id=target_id,
        kind="hole",
        entity_handle=handle,
        layer=(candidate or {}).get("layer"),
        number_layer=data.get("number_layer"),
        entity_type=(candidate or {}).get("entity_type"),
        number=part.number,
        material_group=part.material_group,
        area=float(hole.area),
        bounds=[float(value) for value in hole.bounds],
        geometry_wkt=hole.wkt,
        classification=None,
        source="hole",
    )


def _collect_hole_snapshots(
    parts: list[Any],
    data_by_id: dict[int, dict[str, Any]],
    closed_entities: dict[str, dict[str, Any]],
) -> dict[str, CandidateSnapshot]:
    snapshots: dict[str, CandidateSnapshot] = {}
    for part in parts:
        data = data_by_id.get(int(part.id))
        for hole_index in range(len(part.holes)):
            snapshot = _hole_snapshot(
                part,
                hole_index,
                data,
                closed_entities,
            )
            snapshots[snapshot.target_id] = snapshot
    return snapshots


def build_recognition_feedback(
    ctx: JobContext,
    plan: CorrectionPlan,
    *,
    before_parts: list[Any],
    after_parts: list[Any],
    before_data: list[dict[str, Any]] | None,
    after_data: list[dict[str, Any]] | None,
    revision: int,
) -> RecognitionFeedback:
    """Return the compact delta learned from one correction revision."""
    before_data_by_id = _data_by_id(before_data)
    after_data_by_id = _data_by_id(after_data)
    before_by_id = {int(part.id): part for part in before_parts}
    after_by_id = {int(part.id): part for part in after_parts}

    before_panels: list[CandidateSnapshot] = []
    after_panels: list[CandidateSnapshot] = []
    for part_id in sorted(set(before_by_id) | set(after_by_id)):
        before = before_by_id.get(part_id)
        after = after_by_id.get(part_id)
        if before is None and after is not None:
            after_panels.append(
                _panel_snapshot(after, after_data_by_id.get(part_id))
            )
        elif before is not None and after is None:
            before_panels.append(
                _panel_snapshot(before, before_data_by_id.get(part_id))
            )
        elif before is not None and after is not None:
            if _part_signature(before) != _part_signature(after):
                before_panels.append(
                    _panel_snapshot(before, before_data_by_id.get(part_id))
                )
                after_panels.append(
                    _panel_snapshot(after, after_data_by_id.get(part_id))
                )

    closed_entities = _closed_entities_by_handle(ctx)
    before_hole_map = _collect_hole_snapshots(
        before_parts,
        before_data_by_id,
        closed_entities,
    )
    after_hole_map = _collect_hole_snapshots(
        after_parts,
        after_data_by_id,
        closed_entities,
    )
    before_holes: list[CandidateSnapshot] = []
    after_holes: list[CandidateSnapshot] = []
    for target_id in sorted(set(before_hole_map) | set(after_hole_map)):
        before_hole = before_hole_map.get(target_id)
        after_hole = after_hole_map.get(target_id)
        if before_hole is None and after_hole is not None:
            after_holes.append(after_hole)
        elif before_hole is not None and after_hole is None:
            before_holes.append(before_hole)
        elif before_hole is not None and after_hole is not None:
            if before_hole.geometry_wkt != after_hole.geometry_wkt:
                before_holes.append(before_hole)
                after_holes.append(after_hole)

    return RecognitionFeedback(
        job_id=getattr(ctx, "job_id", None) or None,
        project_id=ctx.project_id or Path(ctx.dxf_path).stem,
        drawing_digest=_digest_file(ctx.dxf_path),
        revision=revision,
        instruction=plan.instruction,
        scope=plan.feedback_scope,
        operations=[operation.model_dump(mode="json") for operation in plan.operations],
        before_panels=before_panels,
        after_panels=after_panels,
        before_holes=before_holes,
        after_holes=after_holes,
    )


def write_recognition_feedback(
    ctx: JobContext,
    feedback: RecognitionFeedback,
) -> Path:
    output_dir = ctx.output_dir
    if output_dir is None and ctx.numbered_dxf_path is not None:
        output_dir = Path(ctx.numbered_dxf_path).parent
    output_dir = output_dir or Path.cwd()
    stem = Path(ctx.dxf_path).stem or "drawing"
    path = output_dir / f"{stem}_recognition_feedback_r{feedback.revision}.json"
    path.write_text(feedback.to_json(indent=2), encoding="utf-8")
    return path


def record_recognition_feedback(
    ctx: JobContext,
    plan: CorrectionPlan,
    *,
    revision: int,
    before_parts: list[Any],
    after_parts: list[Any],
    before_data: list[dict[str, Any]] | None,
    after_data: list[dict[str, Any]] | None,
) -> tuple[RecognitionFeedback, Path]:
    """Persist feedback and update project memory for project-scoped learning."""
    feedback = build_recognition_feedback(
        ctx,
        plan,
        before_parts=before_parts,
        after_parts=after_parts,
        before_data=before_data,
        after_data=after_data,
        revision=revision,
    )
    path = write_recognition_feedback(ctx, feedback)
    if (
        str(feedback.scope) == FeedbackScope.GLOBAL.value
        and ctx.global_feedback_dir is not None
    ):
        ctx.global_feedback_dir.mkdir(parents=True, exist_ok=True)
        (ctx.global_feedback_dir / path.name).write_text(
            path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    if (
        str(feedback.scope) == FeedbackScope.PROJECT.value
        and ctx.recognition_memory_path is not None
    ):
        memory = update_recognition_memory(
            ctx.recognition_memory or {},
            feedback,
        )
        save_recognition_memory(ctx.recognition_memory_path, memory)
        ctx.recognition_memory = memory
    ctx.recognition_feedback.append(feedback.model_dump(mode="json"))
    return feedback, path
