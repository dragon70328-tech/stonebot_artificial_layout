"""Unified SQLite state database with versioned schema migrations.

The SaaS deployment can collapse job manifests, task queues, workflow sessions,
and tenant metadata into one file. Split-file mode remains the default for
backward compatibility with existing deployments.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            request_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created
        ON jobs(tenant_id, created_at);

        CREATE TABLE IF NOT EXISTS workflow_sessions (
            session_id TEXT PRIMARY KEY,
            session_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_sessions_updated
        ON workflow_sessions(updated_at);

        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            timeout_s REAL NOT NULL,
            retry_delay_s REAL NOT NULL,
            sequence INTEGER NOT NULL,
            result_json TEXT,
            error TEXT,
            audit_log_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status_sequence
        ON tasks(status, sequence, created_at);

        CREATE TABLE IF NOT EXISTS tenant_projects (
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, project_id)
        );

        CREATE TABLE IF NOT EXISTS tenant_quotas (
            tenant_id TEXT PRIMARY KEY,
            max_projects INTEGER NOT NULL,
            max_artifacts_per_project INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tenant_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            project_id TEXT,
            event TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tenant_audit_tenant_created
        ON tenant_audit(tenant_id, created_at);
        """
    ),
    (
        2,
        """
        ALTER TABLE tasks ADD COLUMN lease_owner TEXT;
        ALTER TABLE tasks ADD COLUMN lease_expires_at REAL;
        """
    ),
]


class StateDatabase:
    """Own one SQLite connection and keep its schema at the latest version."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    @property
    def schema_version(self) -> int:
        row = self.conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def _migrate(self) -> None:
        current = self.schema_version
        for target, script in sorted(_MIGRATIONS):
            if target <= current:
                continue
            with self.conn:
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version = {target}")

    def close(self) -> None:
        self.conn.close()

    def table_names(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return [str(row["name"]) for row in rows]
