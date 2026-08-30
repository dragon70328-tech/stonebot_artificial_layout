"""Content-addressed artifact storage.

Artifacts are written under ``project_id/stage/stage_version/artifacts`` and
include a content digest in the file name, so rerunning the same deterministic
stage never overwrites an earlier result by accident.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ArtifactRef:
    project_id: str
    stage: str
    stage_version: str
    artifact_id: str
    digest: str
    path: Path
    extension: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "stage": self.stage,
            "stage_version": self.stage_version,
            "artifact_id": self.artifact_id,
            "digest": self.digest,
            "path": str(self.path),
            "extension": self.extension,
        }


class ArtifactStore:
    """Small local-first store for deterministic artifacts."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def stage_dir(
        self,
        project_id: str,
        stage: str,
        stage_version: str,
    ) -> Path:
        return self.root / project_id / stage / stage_version / "artifacts"

    def put_bytes(
        self,
        project_id: str,
        stage: str,
        artifact_name: str,
        data: bytes,
        *,
        extension: str = "",
        stage_version: str | None = None,
    ) -> ArtifactRef:
        """Write bytes and return a content-addressed reference."""
        version = stage_version or datetime.now().strftime("%Y%m%d%H%M%S%f")
        digest = content_digest(data)
        artifact_id = f"{stage}:{digest[:24]}"
        safe_name = "".join(
            ch if ch.isalnum() or ch in "-_." else "_" for ch in artifact_name
        )
        suffix = f".{extension}" if extension else ""
        filename = f"{safe_name}.{digest[:12]}{suffix}"
        out_dir = self.stage_dir(project_id, stage, version)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        path.write_bytes(data)
        return ArtifactRef(
            project_id=project_id,
            stage=stage,
            stage_version=version,
            artifact_id=artifact_id,
            digest=digest,
            path=path,
            extension=extension,
        )

    def put_text(
        self,
        project_id: str,
        stage: str,
        artifact_name: str,
        text: str,
        *,
        extension: str = "txt",
        stage_version: str | None = None,
    ) -> ArtifactRef:
        return self.put_bytes(
            project_id,
            stage,
            artifact_name,
            text.encode("utf-8"),
            extension=extension,
            stage_version=stage_version,
        )

    def put_json(
        self,
        project_id: str,
        stage: str,
        artifact_name: str,
        payload: dict,
        *,
        stage_version: str | None = None,
    ) -> ArtifactRef:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        return self.put_text(
            project_id,
            stage,
            artifact_name,
            text,
            extension="json",
            stage_version=stage_version,
        )

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        return ref.path.read_bytes()

    def read_text(self, ref: ArtifactRef) -> str:
        return ref.path.read_text(encoding="utf-8")

    def read_json(self, ref: ArtifactRef) -> dict[str, Any]:
        return json.loads(ref.path.read_text(encoding="utf-8"))
