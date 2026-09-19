"""Project-local learning memory built from confirmed DXF corrections.

Memory is intentionally conservative: it stores layer-role evidence, number
layer evidence and simple geometry statistics. Global model training is handled
separately and is never triggered by a single user correction.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.recognition_contracts import FeedbackScope, RecognitionFeedback


MEMORY_VERSION = 1


def new_recognition_memory(project_id: str = "") -> dict[str, Any]:
    return {
        "version": MEMORY_VERSION,
        "project_id": project_id,
        "feedback_count": 0,
        "layer_roles": {},
        "number_layer_counts": {},
        "number_samples": {},
        "geometry": {
            "panel_area_sum": 0.0,
            "panel_count": 0,
            "hole_area_sum": 0.0,
            "hole_count": 0,
        },
        "updated_at": None,
    }


def load_recognition_memory(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return new_recognition_memory()
    memory_path = Path(path)
    if not memory_path.exists():
        return new_recognition_memory()
    try:
        payload = json.loads(memory_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return new_recognition_memory()
    if not isinstance(payload, dict):
        return new_recognition_memory()
    base = new_recognition_memory(str(payload.get("project_id") or ""))
    base.update(payload)
    base.setdefault("layer_roles", {})
    base.setdefault("number_layer_counts", {})
    base.setdefault("number_samples", {})
    base.setdefault("geometry", {})
    return base


def save_recognition_memory(
    path: str | Path,
    memory: dict[str, Any],
) -> None:
    memory_path = Path(path)
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(
        json.dumps(memory, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _layer_record(memory: dict[str, Any], layer: str) -> dict[str, int]:
    records = memory.setdefault("layer_roles", {})
    record = records.setdefault(
        layer,
        {
            "panel_positive": 0,
            "panel_negative": 0,
            "hole_positive": 0,
            "hole_negative": 0,
        },
    )
    for key in (
        "panel_positive",
        "panel_negative",
        "hole_positive",
        "hole_negative",
    ):
        record.setdefault(key, 0)
    return record


def _increment_layer_role(
    memory: dict[str, Any],
    layer: str | None,
    field: str,
) -> None:
    value = str(layer or "").strip()
    if not value:
        return
    record = _layer_record(memory, value)
    record[field] = int(record.get(field, 0)) + 1


def _increment_counter(
    container: dict[str, int],
    key: str | None,
) -> None:
    value = str(key or "").strip()
    if not value:
        return
    container[value] = int(container.get(value, 0)) + 1


def update_recognition_memory(
    memory: dict[str, Any],
    feedback: RecognitionFeedback,
) -> dict[str, Any]:
    """Apply one project-scoped feedback revision to local memory."""
    if str(feedback.scope) != FeedbackScope.PROJECT.value:
        return memory

    geometry = memory.setdefault("geometry", {})
    for snapshot in feedback.after_panels:
        _increment_layer_role(memory, snapshot.layer, "panel_positive")
        geometry["panel_area_sum"] = float(
            geometry.get("panel_area_sum", 0.0)
        ) + float(snapshot.area)
        geometry["panel_count"] = int(geometry.get("panel_count", 0)) + 1
        _increment_counter(
            memory.setdefault("number_layer_counts", {}),
            snapshot.number_layer,
        )
        _increment_counter(
            memory.setdefault("number_samples", {}),
            snapshot.number,
        )

    for snapshot in feedback.before_panels:
        _increment_layer_role(memory, snapshot.layer, "panel_negative")

    for snapshot in feedback.after_holes:
        _increment_layer_role(memory, snapshot.layer, "hole_positive")
        geometry["hole_area_sum"] = float(
            geometry.get("hole_area_sum", 0.0)
        ) + float(snapshot.area)
        geometry["hole_count"] = int(geometry.get("hole_count", 0)) + 1

    for snapshot in feedback.before_holes:
        _increment_layer_role(memory, snapshot.layer, "hole_negative")

    memory["feedback_count"] = int(memory.get("feedback_count", 0)) + 1
    memory["updated_at"] = datetime.now(timezone.utc).isoformat()
    return memory


def recognition_memory_hints(memory: dict[str, Any] | None) -> dict[str, Any]:
    """Return conservative learned layer hints and geometry statistics."""
    if not memory:
        return {
            "panel_layers": [],
            "hole_layers": [],
            "number_layers": [],
            "panel_area_mean": 0.0,
            "hole_area_mean": 0.0,
        }

    panel_layers: list[str] = []
    hole_layers: list[str] = []
    for layer, record in memory.get("layer_roles", {}).items():
        panel_positive = int(record.get("panel_positive", 0))
        panel_negative = int(record.get("panel_negative", 0))
        hole_positive = int(record.get("hole_positive", 0))
        hole_negative = int(record.get("hole_negative", 0))
        if panel_positive > panel_negative and panel_positive >= 1:
            panel_layers.append(layer)
        if hole_positive > hole_negative and hole_positive >= 1:
            hole_layers.append(layer)

    number_layers = [
        layer
        for layer, count in memory.get("number_layer_counts", {}).items()
        if int(count) > 0
    ]
    geometry = memory.get("geometry", {})
    panel_count = int(geometry.get("panel_count", 0))
    hole_count = int(geometry.get("hole_count", 0))
    return {
        "panel_layers": sorted(panel_layers),
        "hole_layers": sorted(hole_layers),
        "number_layers": sorted(number_layers),
        "panel_area_mean": (
            float(geometry.get("panel_area_sum", 0.0)) / panel_count
            if panel_count > 0
            else 0.0
        ),
        "hole_area_mean": (
            float(geometry.get("hole_area_sum", 0.0)) / hole_count
            if hole_count > 0
            else 0.0
        ),
    }
