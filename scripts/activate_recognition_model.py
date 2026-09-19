"""Activate or roll back a versioned recognition model."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.recognition_model import load_recognition_model, save_recognition_model


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_path")
    parser.add_argument(
        "--current",
        default=str(PROJECT_ROOT / "recognition_models" / "current.json"),
    )
    parser.add_argument("--backup-dir")
    args = parser.parse_args()

    model = load_recognition_model(args.model_path)
    if not model or not model.get("feature_names") or not model.get("weights"):
        raise SystemExit(f"invalid recognition model: {args.model_path}")
    current = Path(args.current)
    if current.exists() and args.backup_dir:
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, backup_dir / current.name)
    save_recognition_model(current, model)
    print(f"activated {model.get('model_version')} -> {current}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
