"""Scheduling policy for global recognition-model retraining."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.recognition_model import load_recognition_model


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def feedback_file_created_at(path: str | Path) -> datetime:
    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        parsed = _parse_time(payload.get("created_at"))
        if parsed is not None:
            return parsed
    except (OSError, ValueError, TypeError):
        pass
    return datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)


def count_new_feedback(
    feedback_files: list[str | Path],
    since: datetime | None,
) -> int:
    if since is None:
        return len(feedback_files)
    return sum(
        1
        for path in feedback_files
        if feedback_file_created_at(path) > since
    )


def evaluate_training_trigger(
    *,
    current_model: dict[str, Any] | None,
    feedback_files: list[str | Path],
    last_training_at: datetime | None,
    monitoring_report: dict[str, Any] | None = None,
    min_new_feedback: int = 20,
    min_total_samples: int = 20,
    max_interval_days: int = 30,
    min_drift_feedback: int = 5,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Return a deterministic retraining decision and its reasons."""
    now = now or datetime.now(timezone.utc)
    total_feedback = len(feedback_files)
    new_feedback = count_new_feedback(feedback_files, last_training_at)
    days_since_training = (
        (now - last_training_at).total_seconds() / 86400.0
        if last_training_at is not None
        else None
    )
    drift = bool((monitoring_report or {}).get("drift", {}).get("drift_detected"))
    reasons: list[str] = []

    if not current_model:
        if total_feedback >= min_new_feedback:
            reasons.append("no_active_model")
    elif force:
        reasons.append("manual_force")
    else:
        if new_feedback >= min_new_feedback:
            reasons.append("new_feedback_threshold")
        if (
            days_since_training is not None
            and days_since_training >= max_interval_days
            and total_feedback >= min_total_samples
        ):
            reasons.append("scheduled_interval")
        if drift and new_feedback >= min_drift_feedback:
            reasons.append("drift_detected")

    if not current_model and total_feedback < min_new_feedback:
        reasons.append("insufficient_feedback")

    return {
        "should_train": bool(reasons) and "insufficient_feedback" not in reasons,
        "reasons": reasons,
        "force": force,
        "new_feedback": new_feedback,
        "total_feedback": total_feedback,
        "days_since_training": (
            round(days_since_training, 3)
            if days_since_training is not None
            else None
        ),
        "drift_detected": drift,
        "thresholds": {
            "min_new_feedback": min_new_feedback,
            "min_total_samples": min_total_samples,
            "max_interval_days": max_interval_days,
            "min_drift_feedback": min_drift_feedback,
        },
        "evaluated_at": now.isoformat(),
    }


def load_training_state(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    state_path = Path(path)
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_training_state(path: str | Path, state: dict[str, Any]) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def last_training_time(
    state: dict[str, Any],
    current_model: dict[str, Any] | None,
) -> datetime | None:
    return _parse_time(
        state.get("last_training_at")
        or (current_model or {}).get("created_at")
    )
