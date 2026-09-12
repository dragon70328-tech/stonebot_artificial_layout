"""File-backed persistence for WorkflowSession.

This is the first persistence boundary for the agent loop. It keeps the same
JSON shape already written by the CLI, so existing ``*_workflow_session.json``
files remain loadable after a restart.
"""

from __future__ import annotations

import json
import re
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
