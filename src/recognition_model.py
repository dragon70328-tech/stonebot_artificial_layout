"""Offline training and versioned loading for global recognition models."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from shapely import wkt
from shapely.geometry import Polygon

from src.recognition_contracts import CandidateSnapshot, RecognitionFeedback


FEATURE_NAMES = [
    "log_area",
    "aspect_ratio",
    "vertex_count",
    "compactness",
    "is_circle",
]


def _polygon_features(
    polygon: Polygon | None,
    entity_type: str | None,
) -> list[float]:
    if polygon is None or polygon.is_empty or polygon.area <= 0.0:
        return [0.0, 0.0, 0.0, 0.0, 1.0 if entity_type == "CIRCLE" else 0.0]
    bounds = polygon.bounds
    width = max(float(bounds[2] - bounds[0]), 0.0)
    height = max(float(bounds[3] - bounds[1]), 0.0)
    short_side = min(width, height)
    aspect_ratio = max(width, height) / short_side if short_side > 0 else 0.0
    perimeter = float(polygon.length)
    compactness = (
        4.0 * math.pi * float(polygon.area) / (perimeter * perimeter)
        if perimeter > 0.0
        else 0.0
    )
    vertex_count = len(polygon.exterior.coords)
    return [
        math.log1p(float(polygon.area)),
        min(aspect_ratio, 100.0),
        float(vertex_count),
        compactness,
        1.0 if entity_type == "CIRCLE" else 0.0,
    ]


def snapshot_features(snapshot: CandidateSnapshot) -> list[float]:
    try:
        polygon = wkt.loads(snapshot.geometry_wkt)
    except Exception:
        polygon = None
    return _polygon_features(polygon, snapshot.entity_type)


def _sample_from_snapshot(
    snapshot: CandidateSnapshot,
    *,
    label: int,
    feedback: RecognitionFeedback,
) -> dict[str, Any]:
    return {
        "drawing_digest": feedback.drawing_digest,
        "project_id": feedback.project_id,
        "source": snapshot.source or snapshot.kind,
        "label": int(label),
        "features": snapshot_features(snapshot),
    }


def feedback_to_samples(feedback: RecognitionFeedback) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for snapshot in feedback.after_panels:
        samples.append(_sample_from_snapshot(snapshot, label=1, feedback=feedback))
    for snapshot in feedback.before_panels:
        samples.append(_sample_from_snapshot(snapshot, label=0, feedback=feedback))
    for snapshot in feedback.after_holes:
        samples.append(_sample_from_snapshot(snapshot, label=0, feedback=feedback))
    for snapshot in feedback.before_holes:
        samples.append(_sample_from_snapshot(snapshot, label=1, feedback=feedback))
    return samples


def load_feedback_file(path: str | Path) -> RecognitionFeedback:
    return RecognitionFeedback.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def build_training_dataset(feedback_paths: list[str | Path]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for path in sorted(Path(item) for item in feedback_paths):
        if path.suffix.lower() != ".json":
            continue
        if path.name == "current.json":
            continue
        try:
            feedback = load_feedback_file(path)
        except Exception:
            continue
        samples.extend(feedback_to_samples(feedback))
    return samples


def discover_feedback_files(directory: str | Path) -> list[Path]:
    root = Path(directory)
    if not root.exists():
        return []
    return sorted(root.rglob("*.json"))


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -40.0, 40.0)))


def _metrics(labels: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    predicted = (probabilities >= 0.5).astype(int)
    true_positive = int(((predicted == 1) & (labels == 1)).sum())
    false_positive = int(((predicted == 1) & (labels == 0)).sum())
    false_negative = int(((predicted == 0) & (labels == 1)).sum())
    accuracy = float((predicted == labels).mean()) if len(labels) else 0.0
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0
    )
    return {
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "positive_samples": int((labels == 1).sum()),
        "negative_samples": int((labels == 0).sum()),
    }


def train_recognition_model(
    samples: list[dict[str, Any]],
    *,
    min_samples: int = 8,
    epochs: int = 800,
    learning_rate: float = 0.08,
) -> dict[str, Any]:
    """Train a deterministic logistic model from feedback samples."""
    if len(samples) < min_samples:
        raise ValueError(
            f"need at least {min_samples} samples, got {len(samples)}"
        )
    labels = np.asarray([int(item["label"]) for item in samples], dtype=float)
    if len(set(labels.tolist())) < 2:
        raise ValueError("training samples must contain both panel and hole labels")

    raw_x = np.asarray([item["features"] for item in samples], dtype=float)
    mean = raw_x.mean(axis=0)
    scale = raw_x.std(axis=0)
    scale[scale < 1e-9] = 1.0
    normalized_x = (raw_x - mean) / scale
    positions = max(int((labels == 1).sum()), 1)
    negatives = max(int((labels == 0).sum()), 1)
    sample_weights = np.where(labels == 1, 0.5 / positions, 0.5 / negatives)

    drawing_ids = sorted(
        {str(item.get("drawing_digest") or "") for item in samples}
    )
    test_drawings: set[str] = set()
    if len(drawing_ids) >= 5:
        test_drawings = set(drawing_ids[::5])
    train_mask = np.asarray(
        [
            str(item.get("drawing_digest") or "") not in test_drawings
            for item in samples
        ],
        dtype=bool,
    )
    test_mask = ~train_mask
    if not train_mask.any() or len(set(labels[train_mask].tolist())) < 2:
        train_mask = np.ones(len(samples), dtype=bool)
        test_mask = np.ones(len(samples), dtype=bool)

    train_x = normalized_x[train_mask]
    train_y = labels[train_mask]
    train_w = sample_weights[train_mask]
    weights = np.zeros(train_x.shape[1], dtype=float)
    bias = float(math.log((train_y.sum() + 1.0) / (len(train_y) - train_y.sum() + 1.0)))
    for _ in range(epochs):
        probabilities = _sigmoid(train_x @ weights + bias)
        residual = (probabilities - train_y) * train_w
        gradient = train_x.T @ residual + 0.01 * weights
        bias_gradient = float(residual.sum())
        weights -= learning_rate * gradient
        bias -= learning_rate * bias_gradient

    metrics = _metrics(
        labels[test_mask],
        _sigmoid(normalized_x[test_mask] @ weights + bias),
    )
    created_at = datetime.now(timezone.utc).isoformat()
    model = {
        "model_version": f"recognition-model-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "created_at": created_at,
        "feature_names": FEATURE_NAMES,
        "mean": [float(value) for value in mean],
        "scale": [float(value) for value in scale],
        "weights": [float(value) for value in weights],
        "bias": float(bias),
        "metrics": metrics,
        "sample_count": len(samples),
        "drawing_count": len(drawing_ids),
        "source_projects": sorted(
            {str(item.get("project_id") or "") for item in samples}
        ),
        "classifier_weight": 0.36,
    }
    return model


def save_recognition_model(path: str | Path, model: dict[str, Any]) -> None:
    model_path = Path(path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(
        json.dumps(model, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_recognition_model(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    model_path = Path(path)
    if not model_path.exists():
        return {}
    try:
        payload = json.loads(model_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def predict_panel_probability(
    model: dict[str, Any] | None,
    polygon: Polygon | None,
    entity_type: str | None,
) -> float | None:
    if not model or not model.get("feature_names"):
        return None
    try:
        features = np.asarray(
            _polygon_features(polygon, entity_type),
            dtype=float,
        )
        mean = np.asarray(model["mean"], dtype=float)
        scale = np.asarray(model["scale"], dtype=float)
        weights = np.asarray(model["weights"], dtype=float)
        scale[scale < 1e-9] = 1.0
        probability = float(
            _sigmoid(
                np.asarray(
                    [((features - mean) / scale) @ weights + float(model["bias"])]
                )
            )[0]
        )
        return min(max(probability, 0.0), 1.0)
    except Exception:
        return None
