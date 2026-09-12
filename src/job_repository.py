"""Job manifest persistence boundary.

The service currently writes job manifests as JSON files. This module keeps
that file format as the default while providing a SQLite-backed repository
selected by ``STONEBOT_JOB_STORE``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Protocol


_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


class JobRepository(Protocol):
    def save(self, job_id: str, request: dict[str, Any], created_at: float) -> None:
        """Persist one job manifest."""

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Return a manifest or None when absent."""

    def list_job_ids(self) -> list[str]:
        """Return all persisted job ids."""


class FileJobRepository:
    """JSON-file job repository, compatible with the existing manifest format."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        safe_id = _SAFE_ID.sub("_", job_id).strip("_")
        return self.root / f"{safe_id}.json"

    def save(self, job_id: str, request: dict[str, Any], created_at: float) -> None:
        payload = {
            "job_id": job_id,
            "request": request,
            "created_at": created_at,
        }
        self._path(job_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, job_id: str) -> dict[str, Any] | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_job_ids(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.json"))


class SqliteJobRepository:
    """SQLite job repository for multi-instance SaaS deployments."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created
                ON jobs(tenant_id, created_at)
                """
            )

    def save(self, job_id: str, request: dict[str, Any], created_at: float) -> None:
        tenant_id = request.get("tenant_id", "default")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO jobs (job_id, tenant_id, request_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    tenant_id=excluded.tenant_id,
                    request_json=excluded.request_json,
                    created_at=excluded.created_at
                """,
                (
                    job_id,
                    tenant_id,
                    json.dumps(request, ensure_ascii=False),
                    created_at,
                ),
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "job_id": row["job_id"],
            "request": json.loads(row["request_json"]),
            "created_at": float(row["created_at"]),
        }

    def list_job_ids(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT job_id FROM jobs ORDER BY created_at, job_id"
        ).fetchall()
        return [str(row["job_id"]) for row in rows]


def build_job_repository(root: str | Path) -> JobRepository:
    """Select job persistence from ``STONEBOT_JOB_STORE``."""
    manager_root = Path(root)
    if os.environ.get("STONEBOT_JOB_STORE", "file").strip().lower() == "sqlite":
        return SqliteJobRepository(manager_root / "jobs.sqlite3")
    return FileJobRepository(manager_root / "jobs")
