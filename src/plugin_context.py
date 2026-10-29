"""Versioned JSON/WKT persistence for resumable plugin jobs.

The normal Stonebot contracts avoid transmitting Shapely objects across service
boundaries. Within one local plugin job, we still need to resume the exact
geometry context after an MCP process restart. This module snapshots only
JSON-safe domain data and geometry WKT; it never uses pickle.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from shapely import wkt
from shapely.geometry.base import BaseGeometry

from src.contracts import RecognizedDrawing
from src.drawing_profile import DrawingIssue, load_profile
from src.job_context import JobContext
from src.models import NestingResult, Part, Sheet
from src.units import UnitSystem
from src.workflow import GroupNestingOutcome


SNAPSHOT_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseGeometry):
        return {"__geometry_wkt__": value.wkt}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_encode(item) for item in value]
    raise TypeError(
        f"unsupported context snapshot value: {type(value).__name__}"
    )


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"__geometry_wkt__"}:
            return wkt.loads(str(value["__geometry_wkt__"]))
        return {key: _decode(item) for key, item in value.items()}
    return value


def _encode_part(part: Part) -> dict[str, Any]:
    return {
        "id": part.id,
        "number": part.number,
        "polygon": _encode(part.polygon),
        "outer_polygon": _encode(part.outer_polygon),
        "holes": _encode(part.holes),
        "original_number": part.original_number,
        "material_group": part.material_group,
        "material_column": part.material_column,
        "group_id": part.group_id,
        "area": part.area,
        "label_position": list(part.label_position),
        "outer_handle": part.outer_handle,
        "hole_handles": list(part.hole_handles),
        "closure_gap": part.closure_gap,
        "outer_shape": _encode(part.outer_shape),
        "outer_arcs": _encode(part.outer_arcs),
        "hole_shapes": _encode(part.hole_shapes),
    }


def _decode_part(payload: dict[str, Any]) -> Part:
    return Part(
        id=int(payload["id"]),
        number=str(payload.get("number") or ""),
        polygon=_decode(payload["polygon"]),
        outer_polygon=_decode(payload["outer_polygon"]),
        holes=[_decode(item) for item in payload.get("holes", [])],
        original_number=payload.get("original_number"),
        material_group=payload.get("material_group"),
        material_column=payload.get("material_column"),
        group_id=payload.get("group_id"),
        area=float(payload.get("area", 0.0)),
        label_position=tuple(payload.get("label_position") or (0.0, 0.0)),
        outer_handle=payload.get("outer_handle"),
        hole_handles=list(payload.get("hole_handles") or []),
        closure_gap=float(payload.get("closure_gap", 0.0)),
        outer_shape=_decode(payload.get("outer_shape")),
        outer_arcs=_decode(payload.get("outer_arcs") or []),
        hole_shapes=_decode(payload.get("hole_shapes") or []),
    )


def _encode_result(result: NestingResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "sheets": [
            {
                "index": sheet.index,
                "width": sheet.width,
                "height": sheet.height,
                "thickness": sheet.thickness,
                "parts": [_encode_part(part) for part in sheet.parts],
                "remaining_area": sheet.remaining_area,
            }
            for sheet in result.sheets
        ],
        "unit": result.unit,
        "total_parts": result.total_parts,
        "total_sheets": result.total_sheets,
        "total_part_area": result.total_part_area,
        "total_sheet_area": result.total_sheet_area,
    }


def _decode_result(payload: dict[str, Any] | None) -> NestingResult | None:
    if payload is None:
        return None
    sheets = [
        Sheet(
            index=int(sheet["index"]),
            width=float(sheet["width"]),
            height=float(sheet["height"]),
            thickness=float(sheet["thickness"]),
            parts=[
                _decode_part(part)
                for part in sheet.get("parts", [])
            ],
            remaining_area=float(sheet.get("remaining_area", 0.0)),
        )
        for sheet in payload.get("sheets", [])
    ]
    return NestingResult(
        sheets=sheets,
        unit=str(payload["unit"]),
        total_parts=int(payload["total_parts"]),
        total_sheets=int(payload["total_sheets"]),
        total_part_area=float(payload["total_part_area"]),
        total_sheet_area=float(payload["total_sheet_area"]),
    )


def _encode_outcome(
    outcome: GroupNestingOutcome | None,
) -> dict[str, Any] | None:
    if outcome is None:
        return None
    return {
        "result": _encode_result(outcome.result),
        "material_summary": _encode(outcome.material_summary),
        "total_min_sheets": outcome.total_min_sheets,
        "total_normal_parts": outcome.total_normal_parts,
        "total_special_parts": outcome.total_special_parts,
        "total_special_pair_min_sheets": (
            outcome.total_special_pair_min_sheets
        ),
        "elapsed_seconds": outcome.elapsed_seconds,
    }


def _decode_outcome(
    payload: dict[str, Any] | None,
) -> GroupNestingOutcome | None:
    if payload is None:
        return None
    return GroupNestingOutcome(
        result=_decode_result(payload.get("result")),
        material_summary=payload.get("material_summary") or [],
        total_min_sheets=int(payload.get("total_min_sheets", 0)),
        total_normal_parts=int(payload.get("total_normal_parts", 0)),
        total_special_parts=int(payload.get("total_special_parts", 0)),
        total_special_pair_min_sheets=payload.get(
            "total_special_pair_min_sheets"
        ),
        elapsed_seconds=float(payload.get("elapsed_seconds", 0.0)),
    )


def context_snapshot(context: JobContext) -> dict[str, Any]:
    recognized = context.recognized_drawing
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "unit": context.unit.value,
        "profile_name": (
            context.drawing_profile.name
            if context.drawing_profile is not None
            else None
        ),
        "profile_source": context.drawing_profile_source,
        "profile_override": _encode(context.drawing_profile_override),
        "fingerprint": _encode(context.fingerprint),
        "parts_data": _encode(context.parts_data or []),
        "parts": [_encode_part(part) for part in (context.parts or [])],
        "closed_entities": _encode(context.closed_entities),
        "classified_candidates": _encode(
            context.classified_candidates
        ),
        "recognized_drawing": (
            recognized.model_dump(mode="json")
            if recognized is not None
            else None
        ),
        "audit_issues": [
            _encode(issue.to_dict())
            for issue in context.audit_issues
        ],
        "result": _encode_result(context.result),
        "nesting_outcome": _encode_outcome(context.nesting_outcome),
        "template_result": context.template_result,
        "manufacturing_metrics": _encode(
            context.manufacturing_metrics
        ),
        "validation_errors": list(context.validation_errors),
        "kerf_metrics": _encode(context.kerf_metrics),
        "correction_revision": context.correction_revision,
        "correction_history": _encode(context.correction_history),
        "recognition_feedback": _encode(
            context.recognition_feedback
        ),
        "recognition_memory": _encode(context.recognition_memory),
        "recognition_model": _encode(context.recognition_model),
        "artifacts": {
            "numbered_dxf": _encode(context.numbered_dxf_path),
            "nested_dxf": _encode(context.nested_dxf_path),
            "report": _encode(context.report_path),
            "check": _encode(context.check_path),
            "recognition_confirmation": _encode(
                context.recognition_confirmation_artifact_path
            ),
        },
    }


def save_context_snapshot(
    context: JobContext,
    path: str | Path,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            context_snapshot(context),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def restore_context_snapshot(
    context: JobContext,
    path: str | Path,
) -> None:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(payload.get("snapshot_version", 0)) != SNAPSHOT_VERSION:
        raise ValueError(
            "unsupported plugin context snapshot version: "
            f"{payload.get('snapshot_version')}"
        )

    context.unit = UnitSystem(payload.get("unit", UnitSystem.METRIC.value))
    profile_name = payload.get("profile_name")
    if profile_name:
        profile_path = PROJECT_ROOT / "drawing_profiles" / f"{profile_name}.json"
        if profile_path.exists():
            context.drawing_profile = load_profile(profile_path)
    context.drawing_profile_source = payload.get("profile_source")
    context.drawing_profile_override = _decode(
        payload.get("profile_override")
    )
    context.fingerprint = _decode(payload.get("fingerprint"))
    context.parts_data = _decode(payload.get("parts_data") or [])
    context.parts = [
        _decode_part(item)
        for item in (payload.get("parts") or [])
    ]
    context.closed_entities = _decode(
        payload.get("closed_entities") or []
    )
    context.classified_candidates = _decode(
        payload.get("classified_candidates") or []
    )
    recognized = payload.get("recognized_drawing")
    context.recognized_drawing = (
        RecognizedDrawing.model_validate(recognized)
        if recognized is not None
        else None
    )
    context.audit_issues = [
        DrawingIssue.from_dict(item)
        for item in (payload.get("audit_issues") or [])
    ]
    context.result = _decode_result(payload.get("result"))
    context.nesting_outcome = _decode_outcome(
        payload.get("nesting_outcome")
    )
    context.template_result = bool(payload.get("template_result", False))
    context.manufacturing_metrics = _decode(
        payload.get("manufacturing_metrics") or {}
    )
    context.validation_errors = list(
        payload.get("validation_errors") or []
    )
    context.kerf_metrics = _decode(payload.get("kerf_metrics"))
    context.correction_revision = int(
        payload.get("correction_revision", 0)
    )
    context.correction_history = _decode(
        payload.get("correction_history") or []
    )
    context.recognition_feedback = _decode(
        payload.get("recognition_feedback") or []
    )
    context.recognition_memory = _decode(
        payload.get("recognition_memory") or {}
    )
    context.recognition_model = _decode(
        payload.get("recognition_model") or {}
    )
    artifacts = payload.get("artifacts") or {}
    context.numbered_dxf_path = (
        Path(artifacts["numbered_dxf"])
        if artifacts.get("numbered_dxf")
        else None
    )
    context.nested_dxf_path = (
        Path(artifacts["nested_dxf"])
        if artifacts.get("nested_dxf")
        else None
    )
    context.report_path = (
        Path(artifacts["report"])
        if artifacts.get("report")
        else None
    )
    context.check_path = (
        Path(artifacts["check"])
        if artifacts.get("check")
        else None
    )
    context.recognition_confirmation_artifact_path = (
        Path(artifacts["recognition_confirmation"])
        if artifacts.get("recognition_confirmation")
        else None
    )
