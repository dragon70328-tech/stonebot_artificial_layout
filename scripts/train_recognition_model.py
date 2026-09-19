"""Train a versioned global recognition model from opted-in feedback."""

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
    discover_feedback_files,
    evaluate_model_promotion,
    save_recognition_model,
    train_recognition_model,
)


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
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--epochs", type=int, default=800)
    parser.add_argument("--learning-rate", type=float, default=0.08)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--force-activate", action="store_true")
    args = parser.parse_args()

    feedback_files = discover_feedback_files(args.feedback_dir)
    samples = build_training_dataset(feedback_files)
    model = train_recognition_model(
        samples,
        min_samples=args.min_samples,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
    )
    output_dir = Path(args.output_dir)
    version_path = output_dir / "versions" / f"{model['model_version']}.json"
    save_recognition_model(version_path, model)
    promotion = evaluate_model_promotion(
        model,
        min_samples=args.min_samples,
    )
    activate = bool(args.activate) and (
        promotion["approved"] or args.force_activate
    )
    if activate:
        save_recognition_model(output_dir / "current.json", model)
    manifest = {
        "feedback_files": [str(path) for path in feedback_files],
        "sample_count": len(samples),
        "drawing_count": model["drawing_count"],
        "version_path": str(version_path),
        "requested_activate": bool(args.activate),
        "active": activate,
        "forced": bool(args.force_activate),
        "promotion": promotion,
        "metrics": model["metrics"],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "latest_training.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
