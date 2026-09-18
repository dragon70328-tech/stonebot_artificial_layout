"""Deterministic correction engine for numbered in-place DXF revisions."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shapely.ops import unary_union

from src.correction_contracts import (
    CorrectionOperation,
    CorrectionOperationType,
    CorrectionPlan,
    CorrectionResult,
)
from src.dxf_writer import write_numbered_parts_dxf
from src.job_context import JobContext
from src.models import Part
from src.recognized_contract import parts_data_to_recognized_drawing


def build_correction_context(ctx: JobContext) -> dict[str, Any]:
    """Return a compact target list for the correction planner."""
    parts = ctx.parts or []
    recognized_panels = (
        ctx.recognized_drawing.panels
        if ctx.recognized_drawing is not None
        else []
    )
    items: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        panel_id = (
            recognized_panels[index].panel_id
            if index < len(recognized_panels)
            else None
        )
        holes = []
        for hole_index, hole in enumerate(part.holes):
            handle = (
                part.hole_handles[hole_index]
                if hole_index < len(part.hole_handles)
                else None
            )
            holes.append(
                {
                    "hole_id": _hole_id(part, hole_index),
                    "entity_handle": handle,
                    "area": round(float(hole.area), 3),
                    "bounds": [round(float(value), 3) for value in hole.bounds],
                }
            )
        items.append(
            {
                "panel_id": panel_id,
                "part_id": str(part.id),
                "number": part.number,
                "original_number": part.original_number,
                "outer_handle": part.outer_handle,
                "material_group": part.material_group,
                "hole_count": len(part.holes),
                "holes": holes,
                "area": round(float(part.area), 3),
            }
        )
    known_handles = {
        handle
        for part in parts
        for handle in [part.outer_handle, *part.hole_handles]
        if handle
    }
    unrecognized = []
    for candidate in getattr(ctx, "closed_entities", []):
        handle = candidate.get("handle")
        if not handle or handle in known_handles:
            continue
        polygon = candidate.get("polygon")
        if polygon is None or polygon.is_empty:
            continue
        unrecognized.append(
            {
                "target_id": f"handle:{handle}",
                "entity_handle": handle,
                "entity_type": candidate.get("entity_type"),
                "layer": candidate.get("layer"),
                "linetype": candidate.get("linetype"),
                "area": round(float(polygon.area), 3),
                "bounds": [round(float(value), 3) for value in polygon.bounds],
            }
        )

    return {
        "parts": items,
        "unrecognized_entities": unrecognized,
        "part_count": len(items),
        "correction_revision": ctx.correction_revision,
    }


def _target_key(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _panel_id_map(ctx: JobContext, parts: list[Any]) -> dict[str, int]:
    panels = (
        ctx.recognized_drawing.panels
        if ctx.recognized_drawing is not None
        else []
    )
    mapping: dict[str, int] = {}
    for index, panel in enumerate(panels):
        if index < len(parts):
            mapping[_target_key(panel.panel_id)] = parts[index].id
    return mapping


def _resolve_part(
    parts: list[Any],
    target: str,
    panel_ids: dict[str, int],
) -> Any:
    normalized = _target_key(target)
    target_part_id = panel_ids.get(normalized)
    for part in parts:
        values = {
            _target_key(str(part.id)),
            _target_key(part.number),
            _target_key(part.original_number),
            _target_key(part.outer_handle),
        }
        if normalized in values or target_part_id == part.id:
            return part
    raise ValueError(f"unknown correction target: {target!r}")


def _hole_id(part: Any, hole_index: int) -> str:
    if hole_index < len(part.hole_handles) and part.hole_handles[hole_index]:
        return f"handle:{part.hole_handles[hole_index]}"
    return f"p{part.id}:h{hole_index}"


def _resolve_hole(parts: list[Any], target: str) -> tuple[Any, int]:
    normalized = _target_key(target)
    for part in parts:
        for index, _hole in enumerate(part.holes):
            handle = (
                part.hole_handles[index]
                if index < len(part.hole_handles)
                else None
            )
            candidates = {
                _target_key(_hole_id(part, index)),
                _target_key(handle),
            }
            if normalized in candidates:
                return part, index
    raise ValueError(f"unknown correction target: {target!r}")


def _resolve_candidate(
    closed_entities: list[dict[str, Any]],
    target: str,
) -> dict[str, Any]:
    normalized = _target_key(target)
    for candidate in closed_entities:
        handle = candidate.get("handle")
        candidates = {
            _target_key(handle),
            _target_key(f"handle:{handle}" if handle else None),
        }
        if normalized in candidates:
            return candidate
    raise ValueError(f"unknown correction target: {target!r}")


def _part_data(parts_data: list[dict[str, Any]], part: Any) -> dict[str, Any] | None:
    for data in parts_data:
        if int(data.get("index", -1)) == int(part.id):
            return data
    return None


def _refresh_part_geometry(part: Any, parts_data: list[dict[str, Any]]) -> None:
    part.holes = [hole for hole in part.holes if not hole.is_empty]
    part.polygon = (
        part.outer_polygon.difference(unary_union(part.holes))
        if part.holes
        else part.outer_polygon
    )
    if not part.polygon.is_valid:
        part.polygon = part.polygon.buffer(0)
    part.area = float(part.polygon.area)
    data = _part_data(parts_data, part)
    if data is not None:
        data["polygon"] = part.polygon
        data["outer_polygon"] = part.outer_polygon
        data["holes"] = list(part.holes)
        data["hole_handles"] = list(part.hole_handles)
        data["area"] = part.area


def _parts_digest(parts: list[Any]) -> str:
    payload = [
        {
            "id": part.id,
            "number": part.number,
            "original_number": part.original_number,
            "material_group": part.material_group,
            "outer_handle": part.outer_handle,
            "geometry": part.polygon.wkt,
            "holes": [hole.wkt for hole in part.holes],
        }
        for part in parts
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _describe_operation(operation: CorrectionOperation) -> str:
    if operation.operation == CorrectionOperationType.RENAME_NUMBER:
        return f"{operation.target} -> {operation.value}"
    if operation.operation == CorrectionOperationType.SWAP_NUMBERS:
        return f"{operation.target} <-> {operation.target_b}"
    if operation.operation == CorrectionOperationType.SET_MATERIAL:
        return f"{operation.target} material -> {operation.value}"
    if operation.operation == CorrectionOperationType.REMOVE_PANEL:
        return f"remove {operation.target}"
    if operation.operation == CorrectionOperationType.HOLE_TO_PANEL:
        return f"hole {operation.target} -> panel {operation.value}"
    if operation.operation == CorrectionOperationType.PANEL_TO_HOLE:
        return f"panel {operation.target} -> hole of {operation.target_b}"
    if operation.operation == CorrectionOperationType.FILL_HOLE:
        return f"fill hole {operation.target}"
    if operation.operation == CorrectionOperationType.ADD_PANEL_FROM_ENTITY:
        return f"entity {operation.target} -> panel {operation.value}"
    if operation.operation == CorrectionOperationType.ADD_HOLE_FROM_ENTITY:
        return f"entity {operation.target} -> hole of {operation.target_b}"
    return f"ignore {operation.target}"


def _apply_operation(
    operation: CorrectionOperation,
    parts: list[Any],
    parts_data: list[dict[str, Any]],
    panel_ids: dict[str, int],
    removed_part_ids: list[int],
    closed_entities: list[dict[str, Any]],
) -> None:
    if operation.operation == CorrectionOperationType.RENAME_NUMBER:
        part = _resolve_part(parts, operation.target, panel_ids)
        value = str(operation.value or "").strip()
        if not value:
            raise ValueError("rename_number requires a non-empty number")
        part.number = value
        part.original_number = value
        data = _part_data(parts_data, part)
        if data is not None:
            data["original_number"] = value
        return

    if operation.operation == CorrectionOperationType.SWAP_NUMBERS:
        part = _resolve_part(parts, operation.target, panel_ids)
        other = _resolve_part(parts, str(operation.target_b), panel_ids)
        part.number, other.number = other.number, part.number
        for item in (part, other):
            data = _part_data(parts_data, item)
            if data is not None:
                data["original_number"] = item.number
        return

    if operation.operation == CorrectionOperationType.SET_MATERIAL:
        part = _resolve_part(parts, operation.target, panel_ids)
        value = str(operation.value or "").strip()
        if not value:
            raise ValueError("set_material requires a non-empty material")
        part.material_group = value
        data = _part_data(parts_data, part)
        if data is not None:
            data["material_group"] = value
        return

    if operation.operation == CorrectionOperationType.HOLE_TO_PANEL:
        source, hole_index = _resolve_hole(parts, operation.target)
        value = str(operation.value or "").strip()
        if not value:
            raise ValueError("hole_to_panel requires a non-empty number")
        hole = source.holes.pop(hole_index)
        handle = (
            source.hole_handles.pop(hole_index)
            if hole_index < len(source.hole_handles)
            else None
        )
        _refresh_part_geometry(source, parts_data)
        new_id = max(int(item.id) for item in parts) + 1
        new_part = Part(
            id=new_id,
            number=value,
            polygon=hole,
            outer_polygon=hole,
            holes=[],
            original_number=value,
            material_group=source.material_group,
            area=float(hole.area),
            label_position=tuple(hole.centroid.coords[0]),
            outer_handle=handle,
        )
        parts.append(new_part)
        parts_data.append(
            {
                "index": new_id,
                "polygon": hole,
                "outer_polygon": hole,
                "holes": [],
                "hole_handles": [],
                "original_number": value,
                "material_group": source.material_group,
                "area": float(hole.area),
                "centroid": tuple(hole.centroid.coords[0]),
                "outer_handle": handle,
            }
        )
        return

    if operation.operation == CorrectionOperationType.PANEL_TO_HOLE:
        part = _resolve_part(parts, operation.target, panel_ids)
        parent = _resolve_part(parts, str(operation.target_b), panel_ids)
        if part is parent:
            raise ValueError("panel_to_hole target and parent must be different")
        if part.holes:
            raise ValueError("panel_to_hole does not support panels with holes")
        hole = part.outer_polygon
        if not parent.outer_polygon.covers(hole):
            raise ValueError("panel is not fully contained by target parent")
        if any(hole.intersection(existing).area > 1e-6 for existing in parent.holes):
            raise ValueError("new hole overlaps an existing hole")
        parent.holes.append(hole)
        parent.hole_handles.append(part.outer_handle)
        removed_part_ids.append(int(part.id))
        parts.remove(part)
        data = _part_data(parts_data, part)
        if data is not None:
            parts_data.remove(data)
        _refresh_part_geometry(parent, parts_data)
        return

    if operation.operation == CorrectionOperationType.ADD_PANEL_FROM_ENTITY:
        candidate = _resolve_candidate(closed_entities, operation.target)
        value = str(operation.value or "").strip()
        if not value:
            raise ValueError("add_panel_from_entity requires a non-empty number")
        polygon = deepcopy(candidate["polygon"])
        new_id = max(int(item.id) for item in parts) + 1
        handle = candidate.get("handle")
        new_part = Part(
            id=new_id,
            number=value,
            polygon=polygon,
            outer_polygon=polygon,
            holes=[],
            original_number=value,
            area=float(polygon.area),
            label_position=tuple(polygon.centroid.coords[0]),
            outer_handle=handle,
        )
        parts.append(new_part)
        parts_data.append(
            {
                "index": new_id,
                "polygon": polygon,
                "outer_polygon": polygon,
                "holes": [],
                "hole_handles": [],
                "original_number": value,
                "material_group": None,
                "area": float(polygon.area),
                "centroid": tuple(polygon.centroid.coords[0]),
                "outer_handle": handle,
            }
        )
        return

    if operation.operation == CorrectionOperationType.ADD_HOLE_FROM_ENTITY:
        candidate = _resolve_candidate(closed_entities, operation.target)
        parent = _resolve_part(parts, str(operation.target_b), panel_ids)
        hole = deepcopy(candidate["polygon"])
        if not parent.outer_polygon.covers(hole):
            raise ValueError("candidate is not fully contained by target parent")
        if any(hole.intersection(existing).area > 1e-6 for existing in parent.holes):
            raise ValueError("candidate overlaps an existing hole")
        parent.holes.append(hole)
        parent.hole_handles.append(candidate.get("handle"))
        _refresh_part_geometry(parent, parts_data)
        return

    if operation.operation == CorrectionOperationType.FILL_HOLE:
        source, hole_index = _resolve_hole(parts, operation.target)
        source.holes.pop(hole_index)
        if hole_index < len(source.hole_handles):
            source.hole_handles.pop(hole_index)
        _refresh_part_geometry(source, parts_data)
        return

    if operation.operation in {
        CorrectionOperationType.REMOVE_PANEL,
        CorrectionOperationType.IGNORE_ENTITY,
    }:
        part = _resolve_part(parts, operation.target, panel_ids)
        removed_part_ids.append(int(part.id))
        parts.remove(part)
        data = _part_data(parts_data, part)
        if data is not None:
            parts_data.remove(data)
        return

    raise ValueError(f"unsupported correction operation: {operation.operation}")


def _next_revision_path(ctx: JobContext, revision: int) -> Path:
    output_dir = ctx.output_dir
    if output_dir is None and ctx.numbered_dxf_path is not None:
        output_dir = Path(ctx.numbered_dxf_path).parent
    output_dir = output_dir or Path.cwd()
    stem = Path(ctx.dxf_path).stem or "drawing"
    candidate = output_dir / f"{stem}_numbered_原位_r{revision}.dxf"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = output_dir / f"{stem}_numbered_原位_r{revision}_{suffix}.dxf"
    return candidate


def apply_correction_plan(
    ctx: JobContext,
    plan: CorrectionPlan,
) -> CorrectionResult:
    """Apply a correction plan atomically and regenerate numbered in-place DXF."""
    if not ctx.parts:
        raise ValueError("drawing corrections require an existing numbered drawing")

    working_parts = deepcopy(ctx.parts)
    working_data = deepcopy(ctx.parts_data or [])
    before_digest = _parts_digest(working_parts)
    panel_ids = _panel_id_map(ctx, working_parts)
    removed_part_ids: list[int] = []
    applied: list[str] = []

    for operation in plan.operations:
        _apply_operation(
            operation,
            working_parts,
            working_data,
            panel_ids,
            removed_part_ids,
            ctx.closed_entities,
        )
        applied.append(_describe_operation(operation))

    if not working_parts:
        raise ValueError("correction cannot remove every panel")

    numbers = [str(part.number).strip() for part in working_parts]
    if any(not number for number in numbers):
        raise ValueError("corrected drawing contains an empty part number")
    if len(numbers) != len(set(numbers)):
        duplicates = sorted(
            number for number in set(numbers) if numbers.count(number) > 1
        )
        raise ValueError(
            "corrected drawing contains duplicate numbers: "
            + ", ".join(duplicates)
        )

    revision = ctx.correction_revision + 1
    output_path = _next_revision_path(ctx, revision)
    after_digest = _parts_digest(working_parts)
    write_numbered_parts_dxf(
        working_parts,
        str(output_path),
        unit_system=ctx.unit.value,
    )

    ctx.parts = working_parts
    ctx.parts_data = working_data
    ctx.correction_revision = revision
    ctx.numbered_dxf_path = output_path
    ctx.result = None
    ctx.nesting_outcome = None
    ctx.template_result = False
    ctx.manufacturing_metrics = {}
    ctx.validation_errors = []
    ctx.nested_dxf_path = None
    ctx.report_path = None
    ctx.check_path = None

    profile_name = (
        ctx.recognized_drawing.profile_name
        if ctx.recognized_drawing is not None
        else None
    )
    ctx.recognized_drawing = parts_data_to_recognized_drawing(
        ctx.parts_data,
        ctx.dxf_path,
        profile_name=profile_name,
    )
    history_entry = {
        "revision": revision,
        "plan_id": plan.plan_id,
        "instruction": plan.instruction,
        "operations": [
            operation.model_dump(mode="json")
            for operation in plan.operations
        ],
        "artifact_path": str(output_path),
        "before_digest": before_digest,
        "after_digest": after_digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ctx.correction_history.append(history_entry)

    return CorrectionResult(
        plan_id=plan.plan_id,
        revision=revision,
        before_digest=before_digest,
        after_digest=after_digest,
        artifact_path=str(output_path),
        operations_applied=applied,
        removed_part_ids=removed_part_ids,
        part_numbers=numbers,
    )
