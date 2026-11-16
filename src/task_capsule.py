"""Portable, checksum-verified task capsules for local job transfer.

The capsule is deliberately transport-neutral.  It can be copied to another
computer, uploaded to an object store or encrypted by a higher sync layer.  It
contains only JSON state and explicitly supplied files; it never serializes
Python objects or hosts an executable payload.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


TASK_CAPSULE_VERSION = 1
CAPSULE_MANIFEST_NAME = "capsule.json"
CAPSULE_STATE_DIR = "state"
CAPSULE_FILES_DIR = "files"
MAX_CAPSULE_FILES = 10_000
MAX_CAPSULE_BYTES = 2 * 1024 * 1024 * 1024


class TaskCapsuleError(ValueError):
    """Raised when a capsule is malformed, unsafe or fails verification."""


@dataclass(frozen=True)
class CapsuleFile:
    role: str
    path: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class TaskCapsuleInfo:
    capsule_path: Path
    manifest: dict[str, Any]
    files: dict[str, CapsuleFile]

    @property
    def job_id(self) -> str:
        return str((self.manifest.get("job") or {}).get("job_id") or "")

    @property
    def stage(self) -> str:
        return str((self.manifest.get("job") or {}).get("stage") or "")


@dataclass(frozen=True)
class ImportedTaskCapsule:
    root: Path
    manifest_path: Path
    state: dict[str, Path]
    files: dict[str, Path]
    info: TaskCapsuleInfo


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_component(value: str, fallback: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in "-_." else "_"
        for char in value
    ).strip("._")
    return cleaned or fallback


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _manifest_digest(manifest: dict[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("capsule_digest", None)
    return _sha256(_json_bytes(payload))


def _archive_path(prefix: str, role: str, filename: str) -> str:
    safe_role = _safe_component(role, "item")
    safe_name = _safe_component(Path(filename).name, "payload")
    return f"{prefix}/{safe_role}/{safe_name}"


def _validate_archive_name(name: str) -> PurePosixPath:
    pure = PurePosixPath(name)
    if pure.is_absolute():
        raise TaskCapsuleError(f"absolute capsule path is not allowed: {name}")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise TaskCapsuleError(f"unsafe capsule path: {name}")
    if ":" in pure.parts[0]:
        raise TaskCapsuleError(f"drive-qualified capsule path is not allowed: {name}")
    return pure


def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def create_task_capsule(
    destination: str | Path,
    *,
    job_manifest: Mapping[str, Any],
    context_snapshot: Mapping[str, Any],
    files: Mapping[str, str | Path] | None = None,
    state_payloads: Mapping[str, Any] | None = None,
    runtime_metadata: Mapping[str, Any] | None = None,
) -> Path:
    """Create a checksum-verified task capsule archive."""
    output_path = Path(destination)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    job_payload = dict(job_manifest)
    job_id = str(job_payload.get("job_id") or "").strip()
    if not job_id:
        raise TaskCapsuleError("job_manifest.job_id is required")

    entries: list[CapsuleFile] = []
    file_zips: list[tuple[str, bytes]] = []
    state_zips: list[tuple[str, bytes]] = []
    archive_roles: set[str] = set()
    archive_paths: set[str] = set()

    def add_bytes(
        *,
        role: str,
        archive_name: str,
        data: bytes,
    ) -> None:
        if not role:
            raise TaskCapsuleError("capsule entry role is required")
        if role in archive_roles:
            raise TaskCapsuleError(f"duplicate capsule role: {role}")
        if archive_name in archive_paths:
            raise TaskCapsuleError(f"duplicate capsule path: {archive_name}")
        archive_roles.add(role)
        archive_paths.add(archive_name)
        entry = CapsuleFile(
            role=role,
            path=archive_name,
            sha256=_sha256(data),
            bytes=len(data),
        )
        entries.append(entry)

    add_bytes(
        role="job_manifest",
        archive_name=f"{CAPSULE_STATE_DIR}/job_manifest.json",
        data=_json_bytes(job_payload),
    )
    state_zips.append(
        (f"{CAPSULE_STATE_DIR}/job_manifest.json", _json_bytes(job_payload))
    )
    add_bytes(
        role="context_snapshot",
        archive_name=f"{CAPSULE_STATE_DIR}/context_snapshot.json",
        data=_json_bytes(dict(context_snapshot)),
    )
    state_zips.append(
        (
            f"{CAPSULE_STATE_DIR}/context_snapshot.json",
            _json_bytes(dict(context_snapshot)),
        )
    )
    for role, payload in sorted((state_payloads or {}).items()):
        add_bytes(
            role=f"state:{role}",
            archive_name=_archive_path(
                CAPSULE_STATE_DIR,
                f"state_{role}",
                f"{_safe_component(role, 'payload')}.json",
            ),
            data=_json_bytes(payload),
        )
        state_zips.append(
            (
                _archive_path(
                    CAPSULE_STATE_DIR,
                    f"state_{role}",
                    f"{_safe_component(role, 'payload')}.json",
                ),
                _json_bytes(payload),
            )
        )

    binary_zips: list[tuple[str, bytes]] = []
    for role, raw_path in sorted((files or {}).items()):
        source = Path(raw_path)
        if not source.is_file():
            raise TaskCapsuleError(
                f"capsule file does not exist or is not a file: {source}"
            )
        data = source.read_bytes()
        archive_name = _archive_path(
            CAPSULE_FILES_DIR,
            role,
            source.name,
        )
        add_bytes(
            role=f"file:{role}",
            archive_name=archive_name,
            data=data,
        )
        binary_zips.append((archive_name, data))

    if len(entries) > MAX_CAPSULE_FILES:
        raise TaskCapsuleError(
            f"capsule has too many files: {len(entries)}"
        )
    total_bytes = sum(entry.bytes for entry in entries)
    if total_bytes > MAX_CAPSULE_BYTES:
        raise TaskCapsuleError(
            f"capsule payload exceeds {MAX_CAPSULE_BYTES} bytes"
        )

    manifest: dict[str, Any] = {
        "capsule_version": TASK_CAPSULE_VERSION,
        "capsule_id": f"tc-{uuid.uuid4().hex}",
        "created_at": _utc_now(),
        "job": {
            "job_id": job_id,
            "project_id": job_payload.get("project_id"),
            "tenant_id": job_payload.get("tenant_id"),
            "stage": job_payload.get("stage"),
        },
        "runtime": dict(runtime_metadata or {}),
        "entries": [entry.to_dict() for entry in entries],
        "total_bytes": total_bytes,
    }
    manifest["capsule_digest"] = _manifest_digest(manifest)

    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.writestr(CAPSULE_MANIFEST_NAME, _json_bytes(manifest))
            for archive_name, data in state_zips:
                archive.writestr(archive_name, data)
            for archive_name, data in binary_zips:
                archive.writestr(archive_name, data)
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output_path


def inspect_task_capsule(
    path: str | Path,
    *,
    verify_hashes: bool = True,
) -> TaskCapsuleInfo:
    """Validate structure, paths, sizes and optional content hashes."""
    capsule_path = Path(path)
    try:
        archive = zipfile.ZipFile(capsule_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise TaskCapsuleError(f"invalid task capsule: {capsule_path}") from exc

    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > MAX_CAPSULE_FILES + 1:
            raise TaskCapsuleError("capsule contains too many entries")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise TaskCapsuleError("capsule contains duplicate archive paths")
        for info in infos:
            _validate_archive_name(info.filename)
            if _zip_entry_is_symlink(info):
                raise TaskCapsuleError(
                    f"capsule symbolic links are not allowed: {info.filename}"
                )
        if CAPSULE_MANIFEST_NAME not in names:
            raise TaskCapsuleError("capsule.json is missing")

        try:
            manifest = json.loads(
                archive.read(CAPSULE_MANIFEST_NAME).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaskCapsuleError("capsule.json is invalid") from exc
        if int(manifest.get("capsule_version", 0)) != TASK_CAPSULE_VERSION:
            raise TaskCapsuleError(
                "unsupported task capsule version: "
                f"{manifest.get('capsule_version')}"
            )
        expected_digest = str(manifest.get("capsule_digest") or "")
        if not expected_digest or expected_digest != _manifest_digest(manifest):
            raise TaskCapsuleError("capsule manifest digest mismatch")

        raw_entries = manifest.get("entries")
        if not isinstance(raw_entries, list):
            raise TaskCapsuleError("capsule manifest entries are missing")
        files: dict[str, CapsuleFile] = {}
        total_bytes = 0
        for raw in raw_entries:
            try:
                entry = CapsuleFile(
                    role=str(raw["role"]),
                    path=str(raw["path"]),
                    sha256=str(raw["sha256"]).lower(),
                    bytes=int(raw["bytes"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise TaskCapsuleError("capsule entry is invalid") from exc
            _validate_archive_name(entry.path)
            if entry.role in files:
                raise TaskCapsuleError(
                    f"duplicate capsule role: {entry.role}"
                )
            if entry.path not in names:
                raise TaskCapsuleError(
                    f"capsule entry file is missing: {entry.path}"
                )
            if entry.bytes < 0:
                raise TaskCapsuleError(
                    f"capsule entry has invalid size: {entry.path}"
                )
            total_bytes += entry.bytes
            if verify_hashes:
                data = archive.read(entry.path)
                if len(data) != entry.bytes:
                    raise TaskCapsuleError(
                        f"capsule entry size mismatch: {entry.path}"
                    )
                if _sha256(data) != entry.sha256:
                    raise TaskCapsuleError(
                        f"capsule entry hash mismatch: {entry.path}"
                    )
            files[entry.role] = entry
        if total_bytes != int(manifest.get("total_bytes", -1)):
            raise TaskCapsuleError("capsule total size mismatch")

        expected_paths = {
            CAPSULE_MANIFEST_NAME,
            *(entry.path for entry in files.values()),
        }
        unexpected = sorted(set(names) - expected_paths)
        if unexpected:
            raise TaskCapsuleError(
                f"capsule contains unlisted files: {unexpected[:5]}"
            )
    return TaskCapsuleInfo(
        capsule_path=capsule_path,
        manifest=manifest,
        files=files,
    )


def extract_task_capsule(
    path: str | Path,
    destination: str | Path,
    *,
    verify_hashes: bool = True,
) -> ImportedTaskCapsule:
    """Verify and atomically extract a capsule into a new directory."""
    info = inspect_task_capsule(path, verify_hashes=verify_hashes)
    output = Path(destination)
    if output.exists():
        raise FileExistsError(f"task capsule destination already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.staging-",
            dir=output.parent,
        )
    )
    try:
        with zipfile.ZipFile(info.capsule_path, "r") as archive:
            for entry in info.files.values():
                target = (staging / entry.path).resolve()
                if staging.resolve() not in target.parents:
                    raise TaskCapsuleError(
                        f"capsule extraction escaped root: {entry.path}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry.path) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
            shutil.copy2(info.capsule_path, staging / "source.capsule")
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    state: dict[str, Path] = {}
    files: dict[str, Path] = {}
    for role, entry in info.files.items():
        if role.startswith("state:"):
            state[role.removeprefix("state:")] = output / entry.path
        elif role.startswith("file:"):
            files[role.removeprefix("file:")] = output / entry.path
    return ImportedTaskCapsule(
        root=output,
        manifest_path=output / CAPSULE_STATE_DIR / "job_manifest.json",
        state=state,
        files=files,
        info=info,
    )


def remap_json_paths(
    payload: Any,
    replacements: Mapping[str, str | Path],
) -> Any:
    """Return a copy of JSON-safe data with exact path strings remapped."""
    mapping = {
        str(old): str(new)
        for old, new in replacements.items()
        if str(old)
    }
    if isinstance(payload, dict):
        return {
            key: remap_json_paths(value, mapping)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [remap_json_paths(value, mapping) for value in payload]
    if isinstance(payload, str) and payload in mapping:
        return mapping[payload]
    return payload
