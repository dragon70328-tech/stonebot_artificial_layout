"""Tenant-aware project storage and quotas.

The long-term service must keep every project under its own tenant namespace
and prevent cross-tenant access. This module provides the local directory
enforcement that can later be backed by object storage and IAM.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from src.artifact_store import ArtifactStore


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class TenantError(ValueError):
    """Raised for invalid tenant/project access."""


@dataclass
class TenantQuota:
    max_projects: int = 100
    max_artifacts_per_project: int = 10_000


class TenantRepository(Protocol):
    """Persistence boundary for tenant projects, quotas, and audit events."""

    def save_project(self, tenant_id: str, project_id: str, path: str) -> None:
        ...

    def list_projects(self, tenant_id: str) -> list[str]:
        ...

    def count_projects(self, tenant_id: str) -> int:
        ...

    def get_quota(self, tenant_id: str) -> TenantQuota | None:
        ...

    def save_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        ...

    def record_audit(
        self,
        tenant_id: str,
        event: str,
        *,
        project_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ...

    def list_audit(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        ...


class SqliteTenantRepository:
    """SQLite-backed tenant metadata and audit log."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is not None:
            self.conn = conn
            self.conn.row_factory = sqlite3.Row
            self.db_path = Path(db_path) if db_path is not None else Path(":memory:")
            return
        if db_path is None:
            raise ValueError("db_path or conn is required")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_projects (
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, project_id)
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_quotas (
                    tenant_id TEXT PRIMARY KEY,
                    max_projects INTEGER NOT NULL,
                    max_artifacts_per_project INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tenant_audit_tenant_created
                ON tenant_audit(tenant_id, created_at)
                """
            )

    def save_project(self, tenant_id: str, project_id: str, path: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO tenant_projects (tenant_id, project_id, path, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id, project_id) DO UPDATE SET
                    path=excluded.path,
                    created_at=excluded.created_at
                """,
                (tenant_id, project_id, path, time.time()),
            )

    def list_projects(self, tenant_id: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT project_id FROM tenant_projects
            WHERE tenant_id = ?
            ORDER BY project_id
            """,
            (tenant_id,),
        ).fetchall()
        return [str(row["project_id"]) for row in rows]

    def count_projects(self, tenant_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM tenant_projects WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        return int(row["count"])

    def get_quota(self, tenant_id: str) -> TenantQuota | None:
        row = self.conn.execute(
            "SELECT * FROM tenant_quotas WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            return None
        return TenantQuota(
            max_projects=int(row["max_projects"]),
            max_artifacts_per_project=int(row["max_artifacts_per_project"]),
        )

    def save_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO tenant_quotas (
                    tenant_id,
                    max_projects,
                    max_artifacts_per_project,
                    updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    max_projects=excluded.max_projects,
                    max_artifacts_per_project=excluded.max_artifacts_per_project,
                    updated_at=excluded.updated_at
                """,
                (
                    tenant_id,
                    quota.max_projects,
                    quota.max_artifacts_per_project,
                    time.time(),
                ),
            )

    def record_audit(
        self,
        tenant_id: str,
        event: str,
        *,
        project_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO tenant_audit (
                    tenant_id,
                    project_id,
                    event,
                    payload_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    project_id,
                    event,
                    json.dumps(payload or {}, ensure_ascii=False),
                    time.time(),
                ),
            )

    def list_audit(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        if tenant_id is None:
            rows = self.conn.execute(
                "SELECT * FROM tenant_audit ORDER BY created_at, id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM tenant_audit
                WHERE tenant_id = ?
                ORDER BY created_at, id
                """,
                (tenant_id,),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = json.loads(row["payload_json"])
            event.update(
                {
                    "tenant_id": str(row["tenant_id"]),
                    "event": str(row["event"]),
                    "created_at": float(row["created_at"]),
                }
            )
            if row["project_id"] is not None:
                event["project_id"] = str(row["project_id"])
            events.append(event)
        return events


@dataclass
class TenantStore:
    """Local-first tenant namespace with simple quota checks."""

    root: str | Path
    quota_by_tenant: dict[str, TenantQuota] = field(default_factory=dict)
    audit_log: list[dict[str, Any]] = field(default_factory=list)
    repository: TenantRepository | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _check_id(self, value: str, label: str) -> str:
        if not value or not _SAFE_ID.fullmatch(value):
            raise TenantError(f"invalid {label}: {value!r}")
        return value

    def _quota(self, tenant_id: str) -> TenantQuota:
        if self.repository is not None:
            stored = self.repository.get_quota(tenant_id)
            if stored is not None:
                return stored
        return self.quota_by_tenant.get(tenant_id, TenantQuota())

    def set_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        """Update a tenant quota, persisting it when a repository is present."""
        self.quota_by_tenant[tenant_id] = quota
        if self.repository is not None:
            self.repository.save_quota(tenant_id, quota)

    def _record_audit(
        self,
        event: str,
        tenant_id: str,
        *,
        project_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.repository is not None:
            self.repository.record_audit(
                tenant_id,
                event,
                project_id=project_id,
                payload=payload,
            )
            return
        entry: dict[str, Any] = {"event": event, "tenant_id": tenant_id}
        if project_id is not None:
            entry["project_id"] = project_id
        if payload:
            entry.update(payload)
        self.audit_log.append(entry)

    def tenant_dir(self, tenant_id: str) -> Path:
        tenant_id = self._check_id(tenant_id, "tenant_id")
        return self.root / tenant_id

    def project_dir(self, tenant_id: str, project_id: str) -> Path:
        self._check_id(project_id, "project_id")
        return self.tenant_dir(tenant_id) / project_id

    def list_projects(self, tenant_id: str) -> list[str]:
        """List project ids visible to one tenant only."""
        self._check_id(tenant_id, "tenant_id")
        if self.repository is not None:
            return self.repository.list_projects(tenant_id)
        tenant_dir = self.tenant_dir(tenant_id)
        if not tenant_dir.exists():
            return []
        return sorted(
            path.name
            for path in tenant_dir.iterdir()
            if path.is_dir()
        )

    def create_project(self, tenant_id: str, project_id: str) -> Path:
        """Create a project namespace while enforcing tenant project quota."""
        self._check_id(project_id, "project_id")
        tenant_dir = self.tenant_dir(tenant_id)
        tenant_dir.mkdir(parents=True, exist_ok=True)
        existing = self.list_projects(tenant_id)
        if project_id not in existing and len(existing) >= self._quota(tenant_id).max_projects:
            self._record_audit(
                "project_quota_denied",
                tenant_id,
                project_id=project_id,
                payload={"max_projects": self._quota(tenant_id).max_projects},
            )
            raise TenantError(
                f"tenant {tenant_id} exceeds max_projects "
                f"{self._quota(tenant_id).max_projects}"
            )
        project_dir = tenant_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        if self.repository is not None:
            self.repository.save_project(tenant_id, project_id, str(project_dir))
        self._record_audit(
            "create_project",
            tenant_id,
            project_id=project_id,
            payload={"path": str(project_dir)},
        )
        return project_dir

    def artifact_store(self, tenant_id: str, project_id: str) -> ArtifactStore:
        """Return an artifact store scoped to one tenant project."""
        project_dir = self.project_dir(tenant_id, project_id)
        return ArtifactStore(project_dir / "artifacts")

    def assert_can_access(self, tenant_id: str, project_id: str) -> None:
        """Fail if the requested path would escape the tenant namespace."""
        project_dir = self.project_dir(tenant_id, project_id)
        tenant_root = self.tenant_dir(tenant_id).resolve()
        resolved = project_dir.resolve()
        if tenant_root not in resolved.parents and resolved != tenant_root:
            raise TenantError("cross-tenant path access denied")

    def project_artifact_count(self, tenant_id: str, project_id: str) -> int:
        store = self.artifact_store(tenant_id, project_id)
        return sum(1 for _ in store.root.rglob("*") if _.is_file())


def build_tenant_store(
    root: str | Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> TenantStore:
    """Select tenant persistence from ``STONEBOT_TENANT_STORE``."""
    if conn is not None:
        return TenantStore(root, repository=SqliteTenantRepository(conn=conn))
    if os.environ.get("STONEBOT_TENANT_STORE", "file").strip().lower() == "sqlite":
        repository = SqliteTenantRepository(Path(root) / "tenants.sqlite3")
        return TenantStore(root, repository=repository)
    return TenantStore(root)
