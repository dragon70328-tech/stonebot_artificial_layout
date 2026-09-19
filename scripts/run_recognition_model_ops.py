"""Run the integrated recognition-learning operations cycle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0,str(PROJECT_ROOT))

from src.recognition_operations import run_recognition_operations


def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--feedback-dir',default=str(PROJECT_ROOT/'recognition_models'/'global_feedback'))
    parser.add_argument('--output-dir',default=str(PROJECT_ROOT/'recognition_models'))
    parser.add_argument('--tenant-id')
    parser.add_argument('--min-new-feedback',type=int,default=20)
    parser.add_argument('--min-total-feedback',type=int,default=20)
    parser.add_argument('--max-interval-days',type=int,default=30)
    parser.add_argument('--min-drift-feedback',type=int,default=5)
    parser.add_argument('--min-samples',type=int,default=20)
    parser.add_argument('--epochs',type=int,default=800)
    parser.add_argument('--learning-rate',type=float,default=0.08)
    parser.add_argument('--no-activate',action='store_true')
    parser.add_argument('--force',action='store_true')
    parser.add_argument('--retention-max-age-days',type=int,default=180)
    parser.add_argument('--retention-max-files-per-tenant',type=int,default=500)
    parser.add_argument('--apply-retention',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    report=run_recognition_operations(
        feedback_dir=args.feedback_dir,
        output_dir=args.output_dir,
        tenant_id=args.tenant_id,
        min_new_feedback=args.min_new_feedback,
        min_total_feedback=args.min_total_feedback,
        max_interval_days=args.max_interval_days,
        min_drift_feedback=args.min_drift_feedback,
        min_samples=args.min_samples,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        activate=not args.no_activate,
        force=args.force,
        retention_max_age_days=args.retention_max_age_days,
        retention_max_files_per_tenant=args.retention_max_files_per_tenant,
        apply_retention=args.apply_retention,
        dry_run=args.dry_run,
    )
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
