"""Integrated operations cycle for recognition learning."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.recognition_model import (
    build_training_dataset,
    compare_recognition_models,
    detect_feature_drift,
    discover_feedback_files,
    evaluate_model_on_samples,
    evaluate_model_promotion,
    load_recognition_model,
    save_recognition_model,
    train_recognition_model,
)
from src.recognition_retention import (
    apply_feedback_retention,
    plan_feedback_retention,
)
from src.recognition_training_policy import (
    evaluate_training_trigger,
    last_training_time,
    load_training_state,
    save_training_state,
)


def _acquire_lock(path: Path, stale_minutes: int = 180) -> bool:
    now = datetime.now(timezone.utc)
    if path.exists():
        age_minutes = (now.timestamp() - path.stat().st_mtime) / 60.0
        if age_minutes < stale_minutes:
            return False
        path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": os.getpid(), "created_at": now.isoformat()}),
        encoding="utf-8",
    )
    return True


def run_recognition_operations(
    *,
    feedback_dir: str | Path,
    output_dir: str | Path,
    tenant_id: str | None = None,
    min_new_feedback: int = 20,
    min_total_feedback: int = 20,
    max_interval_days: int = 30,
    min_drift_feedback: int = 5,
    min_samples: int = 20,
    epochs: int = 800,
    learning_rate: float = 0.08,
    activate: bool = True,
    force: bool = False,
    retention_max_age_days: int = 180,
    retention_max_files_per_tenant: int = 500,
    apply_retention: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run monitor, optional retraining, and retention in one cycle."""
    output_dir = Path(output_dir)
    current_path = output_dir / "current.json"
    state_path = output_dir / "training_state.json"
    lock_path = output_dir / "operations.lock"
    current_model = load_recognition_model(current_path)
    state = load_training_state(state_path)
    feedback_files = discover_feedback_files(
        feedback_dir,
        tenant_id=tenant_id,
    )
    samples = build_training_dataset(
        feedback_files,
        tenant_id=tenant_id,
    )
    current_eval = evaluate_model_on_samples(current_model, samples)
    drift = detect_feature_drift(
        current_model,
        samples,
    )
    monitoring = {
        "current_model": current_model.get("model_version"),
        "sample_count": len(samples),
        "feedback_file_count": len(feedback_files),
        "metrics": current_eval.get("metrics") or {},
        "drift": drift,
    }
    monitoring_path = output_dir / "monitoring" / "latest.json"
    monitoring_path.parent.mkdir(parents=True, exist_ok=True)
    monitoring_path.write_text(
        json.dumps(monitoring, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    decision = evaluate_training_trigger(
        current_model=current_model,
        feedback_files=feedback_files,
        last_training_at=last_training_time(state, current_model),
        monitoring_report={"drift": drift},
        min_new_feedback=min_new_feedback,
        min_total_samples=min_total_feedback,
        max_interval_days=max_interval_days,
        min_drift_feedback=min_drift_feedback,
        now=now,
        force=force,
    )
    training_result: dict[str, Any] = {
        "triggered": bool(decision["should_train"]),
        "trained": False,
        "activated": False,
    }

    if decision["should_train"] and not dry_run:
        if not _acquire_lock(lock_path):
            training_result["locked"] = True
        else:
            try:
                candidate = train_recognition_model(
                    samples,
                    min_samples=min_samples,
                    epochs=epochs,
                    learning_rate=learning_rate,
                )
                version_path = (
                    output_dir
                    / "versions"
                    / f"{candidate['model_version']}.json"
                )
                save_recognition_model(version_path, candidate)
                promotion = evaluate_model_promotion(
                    candidate,
                    min_samples=min_samples,
                )
                comparison = compare_recognition_models(
                    current_model,
                    candidate,
                    samples,
                )
                approved = promotion["approved"] and comparison["approved"]
                activated = bool(activate) and (approved or force)
                if activated:
                    save_recognition_model(current_path, candidate)
                now_iso = (now or datetime.now(timezone.utc)).isoformat()
                state.update(
                    {
                        "last_checked_at": now_iso,
                        "last_training_at": now_iso,
                        "last_candidate_version": candidate["model_version"],
                        "last_activated_version": (
                            candidate["model_version"]
                            if activated
                            else state.get("last_activated_version")
                        ),
                        "last_promotion": promotion,
                        "last_comparison": comparison,
                        "last_approved": approved,
                    }
                )
                save_training_state(state_path, state)
                training_result = {
                    "triggered": True,
                    "trained": True,
                    "candidate_version": candidate["model_version"],
                    "version_path": str(version_path),
                    "approved": approved,
                    "activated": activated,
                    "promotion": promotion,
                    "comparison": comparison,
                }
            finally:
                lock_path.unlink(missing_ok=True)

    retention_plan = plan_feedback_retention(
        feedback_dir,
        max_age_days=retention_max_age_days,
        max_files_per_tenant=retention_max_files_per_tenant,
        now=now,
    )
    moved = []
    if apply_retention and not dry_run:
        moved = apply_feedback_retention(
            retention_plan,
            archive_root=Path(feedback_dir) / "archive",
        )
    if not dry_run:
        state["last_checked_at"] = (now or datetime.now(timezone.utc)).isoformat()
        state["last_operations_decision"] = decision
        save_training_state(state_path, state)

    report = {
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "dry_run": dry_run,
        "tenant_id": tenant_id,
        "feedback_file_count": len(feedback_files),
        "sample_count": len(samples),
        "monitoring": monitoring,
        "training_decision": decision,
        "training": training_result,
        "retention": {
            "planned": len(retention_plan.get("archive", [])),
            "moved": len(moved),
            "apply_requested": apply_retention,
        },
    }
    operations_dir = output_dir / "operations"
    operations_dir.mkdir(parents=True, exist_ok=True)
    (operations_dir / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report
