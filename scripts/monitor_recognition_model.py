"""Evaluate the active recognition model and report drift/regression."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.recognition_model import (
    build_training_dataset,
    compare_recognition_models,
    detect_feature_drift,
    discover_feedback_files,
    evaluate_model_on_samples,
    load_recognition_model,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--feedback-dir",
        default=str(PROJECT_ROOT / "recognition_models" / "global_feedback"),
    )
    parser.add_argument(
        "--current",
        default=str(PROJECT_ROOT / "recognition_models" / "current.json"),
    )
    parser.add_argument("--candidate")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "recognition_models" / "monitoring" / "latest.json"),
    )
    parser.add_argument("--drift-threshold", type=float, default=1.0)
    args = parser.parse_args()

    feedback_files = discover_feedback_files(args.feedback_dir)
    samples = build_training_dataset(feedback_files)
    current_model = load_recognition_model(args.current)
    current = evaluate_model_on_samples(current_model, samples)
    candidate = (
        load_recognition_model(args.candidate)
        if args.candidate
        else {}
    )
    comparison = (
        compare_recognition_models(current_model, candidate, samples)
        if candidate
        else None
    )
    drift = detect_feature_drift(
        current_model,
        samples,
        z_threshold=args.drift_threshold,
    )
    report = {
        "current_model": current_model.get("model_version"),
        "candidate_model": candidate.get("model_version") if candidate else None,
        "feedback_file_count": len(feedback_files),
        "sample_count": len(samples),
        "current_metrics": current.get("metrics") or {},
        "candidate_metrics": (comparison or {}).get("candidate_metrics", {}),
        "comparison": comparison,
        "drift": drift,
        "status": (
            "drift_detected"
            if drift["drift_detected"]
            else "ok"
        ),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not drift["drift_detected"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
