"""Content-addressed cache for audit issues.

The cache key combines the source DXF digest, the resolved drawing profile,
and an audit-cache version. This lets identical files reuse the expensive
geometric audit step while still allowing CLI issue statuses to be applied
on top of the cached raw issues.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .drawing_profile import DrawingIssue, DrawingProfile


AUDIT_CACHE_VERSION = 5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = PROJECT_ROOT / "output" / ".cache" / "audit"


def profile_digest(profile: DrawingProfile) -> str:
    try:
        profile_data = asdict(profile)
    except TypeError:
        profile_data = getattr(profile, "__dict__", profile)
    payload = json.dumps(
        profile_data,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def audit_cache_key(file_sha256: str, profile: DrawingProfile) -> str:
    return (
        f"{file_sha256[:16]}_{profile_digest(profile)}"
        f"_v{AUDIT_CACHE_VERSION}"
    )


def load_cached_issues(
    cache_dir: str | Path,
    cache_key: str,
) -> list[DrawingIssue] | None:
    path = Path(cache_dir) / f"{cache_key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [DrawingIssue.from_dict(item) for item in data]
    except Exception:
        return None


def save_cached_issues(
    cache_dir: str | Path,
    cache_key: str,
    issues: list[DrawingIssue],
) -> Path:
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cache_key}.json"
    payload = [issue.to_dict() for issue in issues]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
