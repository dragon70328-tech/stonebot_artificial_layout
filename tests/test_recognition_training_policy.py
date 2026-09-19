from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.recognition_training_policy import evaluate_training_trigger


def _feedback_file(path: Path, created_at: datetime) -> Path:
    path.write_text(
        '{"created_at": "' + created_at.isoformat() + '"}',
        encoding="utf-8",
    )
    return path


def test_policy_bootstraps_when_no_current_model(tmp_path):
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    files = [
        _feedback_file(tmp_path / f"f-{index}.json", now)
        for index in range(2)
    ]

    decision = evaluate_training_trigger(
        current_model={},
        feedback_files=files,
        last_training_at=None,
        min_new_feedback=2,
        now=now,
    )

    assert decision["should_train"] is True
    assert "no_active_model" in decision["reasons"]


def test_policy_triggers_on_new_feedback_and_interval(tmp_path):
    now = datetime(2026, 1, 31, tzinfo=timezone.utc)
    files = [
        _feedback_file(tmp_path / f"f-{index}.json", now)
        for index in range(3)
    ]
    current = {"model_version": "m1", "created_at": (now - timedelta(days=40)).isoformat()}

    by_volume = evaluate_training_trigger(
        current_model=current,
        feedback_files=files,
        last_training_at=now - timedelta(days=1),
        min_new_feedback=3,
        max_interval_days=30,
        now=now,
    )
    by_interval = evaluate_training_trigger(
        current_model=current,
        feedback_files=files[:1],
        last_training_at=now - timedelta(days=40),
        min_new_feedback=10,
        min_total_samples=1,
        max_interval_days=30,
        now=now,
    )

    assert "new_feedback_threshold" in by_volume["reasons"]
    assert "scheduled_interval" in by_interval["reasons"]


def test_policy_uses_drift_when_enough_new_feedback(tmp_path):
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    files = [
        _feedback_file(tmp_path / f"f-{index}.json", now)
        for index in range(4)
    ]

    decision = evaluate_training_trigger(
        current_model={"model_version": "m1"},
        feedback_files=files,
        last_training_at=now - timedelta(days=1),
        monitoring_report={"drift": {"drift_detected": True}},
        min_new_feedback=20,
        min_drift_feedback=3,
        now=now,
    )

    assert decision["should_train"] is True
    assert "drift_detected" in decision["reasons"]


def test_policy_waits_when_feedback_is_insufficient(tmp_path):
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    files = [_feedback_file(tmp_path / "f.json", now)]

    decision = evaluate_training_trigger(
        current_model={},
        feedback_files=files,
        last_training_at=None,
        min_new_feedback=10,
        now=now,
    )

    assert decision["should_train"] is False
    assert "insufficient_feedback" in decision["reasons"]
