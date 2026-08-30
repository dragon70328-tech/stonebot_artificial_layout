"""Tenant-aware project storage and quotas.

The long-term service must keep every project under its own tenant namespace
and prevent cross-tenant access. This module provides the local directory
enforcement that can later be backed by object storage and IAM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.artifact_store import ArtifactStore


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class TenantError(ValueError):
    """Raised for invalid tenant/project access."""


@dataclass
class TenantQuota:
    max_projects: int = 100
    max_artifacts_per_project: int = 10_000


@dataclass
class TenantStore:
    """Local-first tenant namespace with simple quota checks."""

    root: str | Path
    quota_by_tenant: dict[str, TenantQuota] = field(default_factory=dict)
    audit_log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _check_id(self, value: str, label: str) -> str:
        if not value or not _SAFE_ID.fullmatch(value):
            raise TenantError(f"invalid {label}: {value!r}")
        return value

    def _quota(self, tenant_id: str) -> TenantQuota:
        return self.quota_by_tenant.get(tenant_id, TenantQuota())

    def tenant_dir(self, tenant_id: str) -> Path:
        tenant_id = self._check_id(tenant_id, "tenant_id")
        return self.root / tenant_id

    def project_dir(self, tenant_id: str, project_id: str) -> Path:
        self._check_id(project_id, "project_id")
        return self.tenant_dir(tenant_id) / project_id

    def list_projects(self, tenant_id: str) -> list[str]:
        """List project ids visible to one tenant only."""
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
            raise TenantError(
                f"tenant {tenant_id} exceeds max_projects "
                f"{self._quota(tenant_id).max_projects}"
            )
        project_dir = tenant_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        self.audit_log.append(
            {
                "event": "create_project",
                "tenant_id": tenant_id,
                "project_id": project_id,
                "path": str(project_dir),
            }
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
