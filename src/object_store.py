"""Object storage boundary for SaaS artifacts and inputs.

The HTTP layer should not assume files live on local disk forever. This module
defines a small key/value contract and a local implementation that can later
be replaced by an S3-compatible backend without changing callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> dict[str, object]:
        """Write bytes and return a JSON-safe reference."""

    def get_bytes(self, key: str) -> bytes:
        """Read bytes by key."""

    def exists(self, key: str) -> bool:
        """Return True when the key exists."""

    def delete(self, key: str) -> None:
        """Remove an object if present."""


class LocalObjectStore:
    """Filesystem-backed object store with key path traversal protection."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, key: str) -> Path:
        normalized = key.replace("\\", "/").lstrip("/")
        path = self.root / normalized
        resolved_root = self.root.resolve()
        if resolved_root not in path.resolve().parents and path.resolve() != resolved_root:
            raise ValueError(f"object key escapes store root: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes) -> dict[str, object]:
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {
            "key": key,
            "path": str(path),
            "size": len(data),
        }

    def get_bytes(self, key: str) -> bytes:
        path = self._safe_path(key)
        if not path.is_file():
            raise KeyError(f"object not found: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._safe_path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._safe_path(key)
        path.unlink(missing_ok=True)
