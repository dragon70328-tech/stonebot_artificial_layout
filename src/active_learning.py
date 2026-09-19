"""Active-learning ranking for candidates that need human confirmation."""

from __future__ import annotations

import re
from typing import Any

from src.job_context import JobContext


_MODEL_PROBABILITY = re.compile(r"model:panel_probability=([0-9.]+)")


def _target_maps(ctx: JobContext) -> tuple[dict[str, str], dict[str, str]]:
    panels: dict[str, str] = {}
    holes: dict[str, str] = {}
    recognized_panels = (
        ctx.recognized_drawing.panels
        if ctx.recognized_drawing is not None
        else []
    )
    for index, part in enumerate(ctx.parts or []):
        panel_id = (
            recognized_panels[index].panel_id
            if index < len(recognized_panels)
            else None
        )
        target_id = str(panel_id or part.id)
        if part.outer_handle:
            panels[str(part.outer_handle)] = target_id
        for handle in part.hole_handles:
            if handle:
                holes[str(handle)] = f"handle:{handle}"
    return panels, holes


def _classification_priority(
    candidate: dict[str, Any],
) -> tuple[float, list[str]]:
    classification = candidate.get("classification") or {}
    role = str(classification.get("role") or "unknown")
    confidence = float(classification.get("confidence") or 0.0)
    panel_score = float(classification.get("panel_score") or 0.0)
    hole_score = float(classification.get("hole_score") or 0.0)
    reasons = list(classification.get("reasons") or [])
    features = classification.get("features") or {}

    priority = 0.0
    attention: list[str] = []
    if role == "unknown":
        priority += 0.75
        attention.append("未识别或角色不确定")
    if confidence < 0.75:
        priority += (0.75 - confidence) * 0.9
        attention.append(f"分类置信度低：{confidence:.2f}")

    score_gap = abs(panel_score - hole_score)
    if score_gap < 0.16:
        priority += (0.16 - score_gap) * 2.0
        attention.append("面板/孔洞得分接近")

    model_probability = None
    for reason in reasons:
        match = _MODEL_PROBABILITY.search(str(reason))
        if match:
            model_probability = float(match.group(1))
            break
    if model_probability is not None:
        rule_role = "panel" if panel_score >= hole_score else "hole"
        model_role = "panel" if model_probability >= 0.5 else "hole"
        if rule_role != model_role or role != model_role:
            priority += 0.35
            attention.append("模型与规则判断冲突")

    if not features.get("has_number_text") and role == "panel":
        priority += 0.10
        attention.append("轮廓内未发现编号文本")
    if "layer:unknown_hint" in reasons:
        priority += 0.08
        attention.append("图层没有已知角色")

    return priority, attention


def build_active_learning_queue(
    ctx: JobContext,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Return the most uncertain candidates, ordered by review priority."""
    panel_targets, hole_targets = _target_maps(ctx)
    part_data_by_id = {
        int(item["index"]): item
        for item in (getattr(ctx, "parts_data", None) or [])
        if item.get("index") is not None
    }
    queue: list[dict[str, Any]] = []

    for candidate in getattr(ctx, "classified_candidates", []):
        handle = str(candidate.get("handle") or "")
        if not handle:
            continue
        classification = candidate.get("classification") or {}
        role = str(classification.get("role") or "unknown")
        if handle in panel_targets:
            target_id = panel_targets[handle]
            kind = "panel"
        elif handle in hole_targets:
            target_id = hole_targets[handle]
            kind = "hole"
        else:
            target_id = f"handle:{handle}"
            kind = "unrecognized"
        priority, attention = _classification_priority(candidate)
        if role == "unknown" and kind == "unrecognized":
            priority += 0.25
        if priority < 0.12:
            continue
        polygon = candidate.get("polygon")
        queue.append(
            {
                "target_id": target_id,
                "entity_handle": handle,
                "kind": kind,
                "role": role,
                "priority": round(priority, 4),
                "confidence": classification.get("confidence"),
                "layer": candidate.get("layer"),
                "area": (
                    round(float(polygon.area), 3)
                    if polygon is not None and not polygon.is_empty
                    else 0.0
                ),
                "reasons": attention or ["建议复核"],
            }
        )

    if not queue:
        for part in ctx.parts or []:
            data = part_data_by_id.get(int(part.id), {})
            confidence = float(data.get("confidence", 1.0))
            if confidence >= 0.75:
                continue
            queue.append(
                {
                    "target_id": str(part.id),
                    "entity_handle": part.outer_handle,
                    "kind": "panel",
                    "role": "panel",
                    "priority": round((0.75 - confidence) * 0.9, 4),
                    "confidence": confidence,
                    "layer": data.get("layer"),
                    "area": round(float(part.area), 3),
                    "reasons": ["历史识别置信度较低"],
                }
            )

    queue.sort(key=lambda item: item["priority"], reverse=True)
    return queue[:limit]
