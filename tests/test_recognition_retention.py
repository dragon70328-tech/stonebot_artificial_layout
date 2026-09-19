import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.recognition_retention import (
    apply_feedback_retention,
    plan_feedback_retention,
)


def _write_feedback(path: Path, created_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"created_at": created_at.isoformat()}),
        encoding="utf-8",
    )


def test_retention_archives_expired_and_excess_feedback(tmp_path):
    root = tmp_path / "global_feedback"
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    old = root / "tenant-a" / "old.json"
    newest = root / "tenant-a" / "newest.json"
    middle = root / "tenant-a" / "middle.json"
    _write_feedback(old, now - timedelta(days=90))
    _write_feedback(middle, now - timedelta(days=20))
    _write_feedback(newest, now - timedelta(days=1))

    plan = plan_feedback_retention(
        root,
        max_age_days=30,
        max_files_per_tenant=2,
        now=now,
    )
    moved = apply_feedback_retention(plan, archive_root=root / "archive")

    assert len(plan["archive"]) == 1
    assert plan["archive"][0]["path"] == str(old.resolve())
    assert old.exists() is False
    assert middle.exists() is True
    assert newest.exists() is True
    assert len(moved) == 1
    assert Path(moved[0]["target"]).exists()


def test_retention_rejects_path_outside_feedback_root(tmp_path):
    root = tmp_path / "feedback"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    plan = {
        "feedback_dir": str(root),
        "archive": [
            {
                "path": str(outside),
                "tenant": "tenant-a",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reasons": ["expired"],
            }
        ],
    }

    try:
        apply_feedback_retention(plan, archive_root=tmp_path / "archive")
    except ValueError:
        pass
    else:
        raise AssertionError("outside feedback path must be rejected")
