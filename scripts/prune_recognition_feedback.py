"""Archive expired global recognition feedback."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.recognition_retention import (
    apply_feedback_retention,
    plan_feedback_retention,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--feedback-dir",
        default=str(PROJECT_ROOT / "recognition_models" / "global_feedback"),
    )
    parser.add_argument("--max-age-days", type=int, default=180)
    parser.add_argument("--max-files-per-tenant", type=int, default=500)
    parser.add_argument("--archive-root")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    plan = plan_feedback_retention(
        args.feedback_dir,
        max_age_days=args.max_age_days,
        max_files_per_tenant=args.max_files_per_tenant,
    )
    moved = []
    if args.apply and plan["archive"]:
        archive_root = args.archive_root or (
            Path(args.feedback_dir) / "archive"
        )
        moved = apply_feedback_retention(plan, archive_root=archive_root)
    result = {"dry_run": not args.apply, "plan": plan, "moved": moved}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
