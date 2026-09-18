"""Minimal file-backed conversation store for the SaaS chat layer."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConversationMessage:
    role: str
    content: str
    created_at: str = field(default_factory=_utc_now)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class Conversation:
    id: str
    tenant_id: str
    created_at: str = field(default_factory=_utc_now)
    job_id: str | None = None
    messages: list[ConversationMessage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Conversation":
        messages = [
            ConversationMessage(**message)
            for message in payload.get("messages", [])
        ]
        return cls(
            id=payload["id"],
            tenant_id=payload["tenant_id"],
            created_at=payload.get("created_at", _utc_now()),
            job_id=payload.get("job_id"),
            messages=messages,
        )


class ConversationStore:
    """JSON-file conversation storage with tenant isolation."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, tenant_id: str, conversation_id: str) -> Path:
        safe_tenant = _safe_part(tenant_id)
        safe_id = _safe_part(conversation_id)
        return self.root / safe_tenant / f"{safe_id}.json"

    def create(self, tenant_id: str) -> Conversation:
        conversation = Conversation(
            id=f"conv-{uuid.uuid4().hex}",
            tenant_id=tenant_id,
        )
        with self._lock:
            self._save(conversation)
        return conversation

    def get(self, tenant_id: str, conversation_id: str) -> Conversation | None:
        path = self._path(tenant_id, conversation_id)
        if not path.exists():
            return None
        return Conversation.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self, tenant_id: str) -> list[Conversation]:
        tenant_dir = self.root / _safe_part(tenant_id)
        if not tenant_dir.exists():
            return []
        conversations = []
        for path in sorted(tenant_dir.glob("*.json")):
            conversations.append(
                Conversation.from_dict(json.loads(path.read_text(encoding="utf-8")))
            )
        return conversations

    def add_message(
        self,
        tenant_id: str,
        conversation_id: str,
        *,
        role: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> ConversationMessage | None:
        with self._lock:
            conversation = self.get(tenant_id, conversation_id)
            if conversation is None:
                return None
            message = ConversationMessage(
                role=role,
                content=content,
                attachments=attachments or [],
                parameters=parameters or {},
            )
            conversation.messages.append(message)
            self._save(conversation)
            return message

    def set_job_id(
        self,
        tenant_id: str,
        conversation_id: str,
        job_id: str,
    ) -> Conversation | None:
        with self._lock:
            conversation = self.get(tenant_id, conversation_id)
            if conversation is None:
                return None
            conversation.job_id = job_id
            self._save(conversation)
            return conversation

    def _save(self, conversation: Conversation) -> None:
        path = self._path(conversation.tenant_id, conversation.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(conversation.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _safe_part(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in ("-", "_") else "_"
        for char in value
    ).strip("_") or "default"
