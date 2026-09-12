"""File-backed persistence for WorkflowSession.

This is the first persistence boundary for the agent loop. It keeps the same
JSON shape already written by the CLI, so existing ``*_workflow_session.json``
files remain loadable after a restart.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.workflow_session import WorkflowSession


_SAFE_SESSION_ID = re.compile(r"[^A-Za-z0-9._-]+")


class WorkflowSessionStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_id(self, session_id: str) -> str:
        safe = _SAFE_SESSION_ID.sub("_", session_id).strip("._")
        if not safe:
            raise ValueError(f"invalid session_id: {session_id!r}")
        return safe

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{self._safe_id(session_id)}.json"

    def save(
        self,
        session: WorkflowSession,
        *,
        artifacts: dict[str, Any] | None = None,
    ) -> Path:
        if artifacts:
            session.artifacts.update(artifacts)
        path = self.path_for(session.session_id)
        path.write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, session_id: str) -> WorkflowSession:
        path = self.path_for(session_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return WorkflowSession.from_dict(payload)

    def exists(self, session_id: str) -> bool:
        return self.path_for(session_id).exists()

    def list_sessions(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.json"))


class SqliteWorkflowSessionStore:
    """SQLite-backed WorkflowSession persistence for the DB boundary."""

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
                CREATE TABLE IF NOT EXISTS workflow_sessions (
                    session_id TEXT PRIMARY KEY,
                    session_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_workflow_sessions_updated
                ON workflow_sessions(updated_at)
                """
            )

    def save(
        self,
        session: WorkflowSession,
        *,
        artifacts: dict[str, Any] | None = None,
    ) -> Path:
        if artifacts:
            session.artifacts.update(artifacts)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO workflow_sessions (session_id, session_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    session_json=excluded.session_json,
                    updated_at=excluded.updated_at
                """,
                (
                    session.session_id,
                    json.dumps(session.to_dict(), ensure_ascii=False),
                    time.time(),
                ),
            )
        return self.db_path

    def load(self, session_id: str) -> WorkflowSession:
        row = self.conn.execute(
            "SELECT session_json FROM workflow_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"session not found: {session_id}")
        return WorkflowSession.from_dict(json.loads(row["session_json"]))

    def exists(self, session_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM workflow_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row is not None

    def list_sessions(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT session_id FROM workflow_sessions ORDER BY updated_at"
        ).fetchall()
        return [str(row["session_id"]) for row in rows]


def build_session_store(root: str | Path) -> WorkflowSessionStore | SqliteWorkflowSessionStore:
    """Select session persistence from ``STONEBOT_SESSION_STORE``."""
    if os.environ.get("STONEBOT_SESSION_STORE", "file").strip().lower() == "sqlite":
        return SqliteWorkflowSessionStore(Path(root) / "sessions.sqlite3")
    return WorkflowSessionStore(root)
