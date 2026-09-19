"""Append-only audit log for recognition-model administration."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def audit_log_path(root: str | Path) -> Path:
    return Path(root) / "recognition_models" / "operations" / "audit.jsonl"


def append_admin_audit(
    root: str | Path,
    *,
    actor_tenant: str,
    action: str,
    status: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actor_tenant": actor_tenant,
        "action": action,
        "status": status,
        "details": details or {},
    }
    path = audit_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
    return event


def list_admin_audit(
    root: str | Path,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    path = audit_log_path(root)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    except OSError:
        return []
    return list(reversed(events[-max(1, limit):]))
