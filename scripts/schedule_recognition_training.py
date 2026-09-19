"""Run a scheduled recognition-model retraining cycle."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.recognition_model import (
    build_training_dataset,
    compare_recognition_models,
    detect_feature_drift,
    discover_feedback_files,
    evaluate_model_promotion,
    load_recognition_model,
    save_recognition_model,
    train_recognition_model,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--feedback-dir",
        default=str(PROJECT_ROOT / "recognition_models" / "global_feedback"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "recognition_models"),
    )
    parser.add_argument("--min-new-feedback", type=int, default=20)
    parser.add_argument("--min-total-feedback", type=int, default=20)
    parser.add_argument("--max-interval-days", type=int, default=30)
    parser.add_argument("--min-drift-feedback", type=int, default=5)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--epochs", type=int, default=800)
    parser.add_argument("--learning-rate", type=float, default=0.08)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    state_path = output_dir / "training_state.json"
    current_path = output_dir / "current.json"
    monitoring_path = output_dir / "monitoring" / "latest.json"
    current_model = load_recognition_model(current_path)
    state = load_training_state(state_path)
    monitoring_report = (
        json.loads(monitoring_path.read_text(encoding="utf-8"))
        if monitoring_path.exists()
        else None
    )
    feedback_files = discover_feedback_files(args.feedback_dir)
    decision = evaluate_training_trigger(
        current_model=current_model,
        feedback_files=feedback_files,
        last_training_at=last_training_time(state, current_model),
        monitoring_report=monitoring_report,
        min_new_feedback=args.min_new_feedback,
        min_total_samples=args.min_total_feedback,
        max_interval_days=args.max_interval_days,
        min_drift_feedback=args.min_drift_feedback,
        force=args.force,
    )
    if not decision["should_train"] or args.dry_run:
        if not args.dry_run:
            state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
            state["last_decision"] = decision
            save_training_state(state_path, state)
        print(json.dumps({"decision": decision, "trained": False}, ensure_ascii=False, indent=2))
        return 0

    lock_path = output_dir / "training.lock"
    if not _acquire_lock(lock_path):
        print(json.dumps({"decision": decision, "trained": False, "locked": True}, ensure_ascii=False, indent=2))
        return 3

    try:
        samples = build_training_dataset(feedback_files)
        candidate = train_recognition_model(
            samples,
            min_samples=args.min_samples,
            epochs=args.epochs,
            learning_rate=args.learning_rate,
        )
        version_path = (
            output_dir / "versions" / f"{candidate['model_version']}.json"
        )
        save_recognition_model(version_path, candidate)
        promotion = evaluate_model_promotion(
            candidate,
            min_samples=args.min_samples,
        )
        comparison = compare_recognition_models(
            current_model,
            candidate,
            samples,
        )
        drift = detect_feature_drift(current_model, samples)
        approved = promotion["approved"] and comparison["approved"]
        activated = bool(args.activate) and (approved or args.force)
        if activated:
            save_recognition_model(current_path, candidate)
        now = datetime.now(timezone.utc).isoformat()
        state.update(
            {
                "last_checked_at": now,
                "last_training_at": now,
                "last_training_decision": decision,
                "last_candidate_version": candidate["model_version"],
                "last_activated_version": (
                    candidate["model_version"]
                    if activated
                    else state.get("last_activated_version")
                ),
                "last_promotion": promotion,
                "last_comparison": comparison,
                "last_drift": drift,
                "last_approved": approved,
            }
        )
        save_training_state(state_path, state)
        result = {
            "decision": decision,
            "trained": True,
            "candidate_version": candidate["model_version"],
            "version_path": str(version_path),
            "activated": activated,
            "approved": approved,
            "promotion": promotion,
            "comparison": comparison,
            "drift": drift,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "scheduled_training_latest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
