"""Retention and archival policy for global recognition feedback."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _created_at(path: Path) -> datetime:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload.get("created_at")
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, TypeError):
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def plan_feedback_retention(
    feedback_dir: str | Path,
    *,
    max_age_days: int = 180,
    max_files_per_tenant: int = 500,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Plan archival of expired or excessive feedback files."""
    root = Path(feedback_dir).resolve()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    archive: list[dict[str, Any]] = []
    tenant_counts: dict[str, int] = {}

    if not root.exists():
        return {"feedback_dir": str(root), "archive": [], "tenant_counts": {}}

    for tenant_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        if tenant_dir.name in {"archive", "monitoring"}:
            continue
        files = sorted(
            (path for path in tenant_dir.rglob("*.json") if path.is_file()),
            key=_created_at,
        )
        tenant_counts[tenant_dir.name] = len(files)
        expired = {
            path.resolve()
            for path in files
            if _created_at(path) < cutoff
        }
        excess = max(0, len(files) - max_files_per_tenant)
        excess_paths = {path.resolve() for path in files[:excess]}
        for path in files:
            resolved = path.resolve()
            if resolved not in expired and resolved not in excess_paths:
                continue
            reasons = []
            if resolved in expired:
                reasons.append("expired")
            if resolved in excess_paths:
                reasons.append("over_limit")
            archive.append(
                {
                    "path": str(resolved),
                    "tenant": tenant_dir.name,
                    "created_at": _created_at(path).isoformat(),
                    "reasons": reasons,
                }
            )
    return {
        "feedback_dir": str(root),
        "archive": archive,
        "tenant_counts": tenant_counts,
        "max_age_days": max_age_days,
        "max_files_per_tenant": max_files_per_tenant,
    }


def apply_feedback_retention(
    plan: dict[str, Any],
    *,
    archive_root: str | Path,
) -> list[dict[str, str]]:
    """Move planned feedback files into a tenant/month archive tree."""
    feedback_root = Path(plan["feedback_dir"]).resolve()
    archive_root = Path(archive_root).resolve()
    moved: list[dict[str, str]] = []
    for item in plan.get("archive", []):
        source = Path(item["path"]).resolve()
        try:
            source.relative_to(feedback_root)
        except ValueError as exc:
            raise ValueError(
                f"retention source is outside feedback root: {source}"
            ) from exc
        if source.suffix.lower() != ".json" or not source.exists():
            continue
        created_at = datetime.fromisoformat(item["created_at"])
        month = created_at.strftime("%Y%m")
        target = archive_root / month / str(item["tenant"]) / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = 2
        final_target = target
        while final_target.exists():
            final_target = target.with_name(
                f"{target.stem}_{suffix}{target.suffix}"
            )
            suffix += 1
        shutil.move(str(source), str(final_target))
        moved.append(
            {
                "source": str(source),
                "target": str(final_target),
                "tenant": str(item["tenant"]),
            }
        )
    return moved
