"""Deterministic scoring for closed DXF entity candidates.

Layer names are treated as weak hints, never as the sole source of truth.
Geometry, nesting and nearby number text carry the primary weight so drawings
with renamed, merged or non-standard layers can still be recognised.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from shapely.geometry import Polygon
from shapely.strtree import STRtree

from src.dxf_reader import _is_contained_as_hole


def _normalized_layers(values: Iterable[str] | None) -> set[str]:
    return {
        str(value).strip().casefold()
        for value in (values or [])
        if str(value).strip()
    }


def _polygon_features(polygon: Polygon) -> dict[str, Any]:
    bounds = polygon.bounds
    width = max(float(bounds[2] - bounds[0]), 0.0)
    height = max(float(bounds[3] - bounds[1]), 0.0)
    perimeter = float(polygon.length)
    area = float(polygon.area)
    compactness = (
        4.0 * math.pi * area / (perimeter * perimeter)
        if perimeter > 0.0
        else 0.0
    )
    try:
        vertex_count = len(polygon.exterior.coords)
    except Exception:
        vertex_count = 0
    return {
        "area": area,
        "perimeter": perimeter,
        "bounds": [float(value) for value in bounds],
        "width": width,
        "height": height,
        "aspect_ratio": (
            max(width, height) / min(width, height)
            if min(width, height) > 0.0
            else 0.0
        ),
        "vertex_count": vertex_count,
        "compactness": compactness,
    }


def _contains_text_box(
    polygon: Polygon,
    text_box: Polygon | None,
) -> bool:
    if text_box is None or text_box.is_empty:
        return False
    try:
        overlap = polygon.intersection(text_box).area
        return overlap >= text_box.area * 0.5
    except Exception:
        return False


@dataclass(frozen=True)
class CandidateClassification:
    """Auditable role decision for one closed entity candidate."""

    role: str
    confidence: float
    panel_score: float
    hole_score: float
    reasons: tuple[str, ...]
    features: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "confidence": round(self.confidence, 4),
            "panel_score": round(self.panel_score, 4),
            "hole_score": round(self.hole_score, 4),
            "reasons": list(self.reasons),
            "features": self.features,
        }


def classify_candidates(
    candidates: list[dict[str, Any]],
    *,
    panel_layers: Iterable[str] | None = None,
    hole_layers: Iterable[str] | None = None,
    exclude_layers: Iterable[str] | None = None,
    small_area_threshold: float = 100.0,
    label_boxes: Iterable[Polygon | None] | None = None,
) -> list[dict[str, Any]]:
    """Annotate candidates with role scores and evidence.

    The returned list is a shallow copy of each candidate. The original list
    is not mutated, and the classification is deterministic for the same
    geometry, layer hints and text boxes.
    """
    panel_layer_set = _normalized_layers(panel_layers)
    hole_layer_set = _normalized_layers(hole_layers)
    excluded_layer_set = _normalized_layers(exclude_layers)
    text_boxes = [box for box in (label_boxes or []) if box is not None]
    configured_layer_hints = bool(
        panel_layer_set or hole_layer_set or excluded_layer_set
    )

    features_by_index: list[dict[str, Any]] = []
    for candidate in candidates:
        polygon = candidate.get("polygon")
        features_by_index.append(
            _polygon_features(polygon)
            if isinstance(polygon, Polygon)
            else {
                "area": 0.0,
                "perimeter": 0.0,
                "bounds": [0.0, 0.0, 0.0, 0.0],
                "width": 0.0,
                "height": 0.0,
                "aspect_ratio": 0.0,
                "vertex_count": 0,
                "compactness": 0.0,
            }
        )

    sorted_indexes = sorted(
        range(len(candidates)),
        key=lambda index: features_by_index[index]["area"],
    )
    area_percentile: dict[int, float] = {}
    denominator = max(len(candidates) - 1, 1)
    for rank, index in enumerate(sorted_indexes):
        area_percentile[index] = rank / denominator if len(candidates) > 1 else 1.0

    contained_by: list[list[int]] = [[] for _ in candidates]
    spatial_polygons = [
        candidate.get("polygon")
        if isinstance(candidate.get("polygon"), Polygon)
        else Polygon()
        for candidate in candidates
    ]
    spatial_tree = STRtree(spatial_polygons)
    for index, candidate in enumerate(candidates):
        polygon = candidate.get("polygon")
        if not isinstance(polygon, Polygon) or polygon.is_empty:
            continue
        for raw_parent_index in spatial_tree.query(polygon):
            parent_index = int(raw_parent_index)
            parent_polygon = spatial_polygons[parent_index]
            if parent_index == index or parent_polygon.is_empty:
                continue
            if features_by_index[parent_index]["area"] <= polygon.area:
                continue
            if _is_contained_as_hole(parent_polygon, polygon):
                contained_by[index].append(parent_index)

    results: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        features = features_by_index[index]
        layer = str(candidate.get("layer") or "").strip().casefold()
        entity_type = str(candidate.get("entity_type") or "")
        is_circle = entity_type == "CIRCLE"
        panel_hint = layer in panel_layer_set
        hole_hint = layer in hole_layer_set
        excluded_hint = layer in excluded_layer_set
        has_parent = bool(contained_by[index])
        percentile = area_percentile[index]
        is_small = features["area"] < float(small_area_threshold)
        has_label = any(
            _contains_text_box(candidate.get("polygon"), box)
            for box in text_boxes
        )

        panel_score = 0.0
        hole_score = 0.0
        reasons: list[str] = []

        if panel_hint:
            panel_score += 0.55
            reasons.append("layer:panel_hint")
        if hole_hint:
            hole_score += 0.55
            panel_score -= 0.10
            reasons.append("layer:hole_hint")
        if excluded_hint:
            panel_score -= 0.25
            reasons.append("layer:exclude_hint")
        if configured_layer_hints and not (panel_hint or hole_hint or excluded_hint):
            reasons.append("layer:unknown_hint")
        elif not configured_layer_hints:
            panel_score += 0.08
            reasons.append("no_layer_hints")

        if is_circle:
            hole_score += 0.55
            panel_score -= 0.30
            reasons.append("geometry:circle")
        else:
            panel_score += 0.12
            reasons.append("geometry:closed_non_circle")

        if percentile >= 0.75:
            panel_score += 0.18
            reasons.append("area:large")
        elif percentile >= 0.50:
            panel_score += 0.10
            reasons.append("area:medium")
        else:
            hole_score += 0.05
            reasons.append("area:small")

        if is_small:
            panel_score -= 0.20
            hole_score += 0.06
            reasons.append("area:below_threshold")

        if has_parent:
            panel_score -= 0.20
            hole_score += 0.38
            reasons.append("topology:contained")
        else:
            panel_score += 0.05
            reasons.append("topology:outer")

        if has_label:
            panel_score += 0.35
            reasons.append("text:number_inside")
        elif text_boxes and features["area"] > float(small_area_threshold):
            panel_score -= 0.08
            reasons.append("text:number_absent")

        if 3 <= features["vertex_count"] <= 64 and not is_circle:
            panel_score += 0.05
            reasons.append("geometry:numeric_vertices")
        if features["compactness"] > 0.92 and not is_circle:
            hole_score += 0.05
            reasons.append("geometry:roundish")

        # Explicit hints are strong but not absolute; a small circle on a panel
        # layer remains a hole candidate, and a large unknown-layer outline can
        # still win on geometry plus text.
        if panel_hint and not is_circle and not is_small:
            role = "panel"
            confidence = max(0.78, panel_score)
        elif hole_hint and (is_circle or has_parent):
            role = "hole"
            confidence = max(0.78, hole_score)
        elif is_circle:
            role = "hole"
            confidence = max(0.70, hole_score)
        elif has_parent and not panel_hint:
            role = "hole"
            confidence = max(0.68, hole_score)
        elif panel_score >= 0.34 and panel_score >= hole_score:
            role = "panel"
            confidence = panel_score
        elif hole_score >= 0.44 and hole_score > panel_score:
            role = "hole"
            confidence = hole_score
        else:
            role = "unknown"
            confidence = min(max(panel_score, hole_score), 0.49)

        if excluded_hint and not panel_hint and not has_label:
            role = "unknown"
            reasons.append("decision:excluded_hint_only")

        classification = CandidateClassification(
            role=role,
            confidence=min(max(confidence, 0.0), 0.99),
            panel_score=min(max(panel_score, 0.0), 0.99),
            hole_score=min(max(hole_score, 0.0), 0.99),
            reasons=tuple(reasons),
            features={
                **features,
                "area_percentile": round(percentile, 4),
                "contained_by": contained_by[index],
                "has_number_text": has_label,
            },
        )
        annotated = dict(candidate)
        annotated["classification"] = classification.to_dict()
        results.append(annotated)

    return results


def panel_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        candidate
        for candidate in candidates
        if (candidate.get("classification") or {}).get("role") == "panel"
    ]


def hole_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        candidate
        for candidate in candidates
        if (candidate.get("classification") or {}).get("role") == "hole"
    ]
