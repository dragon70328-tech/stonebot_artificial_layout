"""Signed release manifests for the local StoneBot updater.

Release manifests use Ed25519 detached signatures over canonical JSON.  The
manifest never contains a private key, executable update script or arbitrary
command to run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")


class ReleaseManifestError(ValueError):
    """Raised when a release manifest or signature is invalid."""


class ReleaseVerificationError(ReleaseManifestError):
    """Raised when a release signature cannot establish publisher trust."""


class ReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _safe_relative_path(value: str) -> str:
    pure = PurePosixPath(value.replace("\\", "/"))
    if pure.is_absolute():
        raise ValueError("release path must be relative")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"unsafe release path: {value}")
    if ":" in pure.parts[0]:
        raise ValueError(f"drive-qualified release path is not allowed: {value}")
    return pure.as_posix()


class ReleaseFile(ReleaseModel):
    path: str
    sha256: str
    bytes: int = Field(ge=0)
    executable: bool = False

    @model_validator(mode="after")
    def validate_file(self) -> "ReleaseFile":
        self.path = _safe_relative_path(self.path)
        self.sha256 = self.sha256.lower()
        if not SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError(f"invalid sha256 for {self.path}")
        return self


class ReleaseArtifact(ReleaseModel):
    filename: str
    sha256: str
    bytes: int = Field(gt=0)
    format: Literal["zip"] = "zip"

    @model_validator(mode="after")
    def validate_artifact(self) -> "ReleaseArtifact":
        self.filename = PurePosixPath(
            self.filename.replace("\\", "/")
        ).name
        if self.filename in {"", ".", ".."}:
            raise ValueError("artifact filename is required")
        self.sha256 = self.sha256.lower()
        if not SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("invalid artifact sha256")
        return self


class ReleaseManifest(ReleaseModel):
    manifest_version: Literal[1] = 1
    product: str = Field(min_length=1, max_length=100)
    channel: Literal["stable", "beta", "offline"] = "stable"
    version: str = Field(min_length=1, max_length=64)
    published_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    minimum_version: str | None = None
    minimum_data_version: int = Field(default=1, ge=1)
    key_id: str
    signature_algorithm: Literal["ed25519"] = "ed25519"
    artifact: ReleaseArtifact
    files: list[ReleaseFile] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None

    @model_validator(mode="after")
    def validate_manifest(self) -> "ReleaseManifest":
        if not SAFE_KEY_ID.fullmatch(self.key_id):
            raise ValueError("invalid release key_id")
        if (
            not SAFE_VERSION.fullmatch(self.version)
            or ".." in self.version
        ):
            raise ValueError("invalid release version")
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("release manifest contains duplicate file paths")
        return self

    def signing_payload(self) -> bytes:
        payload = self.model_dump(
            mode="json",
            exclude={"signature"},
            exclude_none=True,
        )
        return _canonical_json(payload)

    def digest(self) -> str:
        return hashlib.sha256(self.signing_payload()).hexdigest()

    def sign(self, private_key: Ed25519PrivateKey) -> "ReleaseManifest":
        signature = private_key.sign(self.signing_payload())
        self.signature = base64.b64encode(signature).decode("ascii")
        return self

    @property
    def signed(self) -> bool:
        return bool(self.signature)


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def build_release_inventory(root: str | Path) -> list[ReleaseFile]:
    """Hash every regular file in a staged release directory."""
    release_root = Path(root).resolve()
    if not release_root.is_dir():
        raise ReleaseManifestError(f"release root is not a directory: {root}")
    entries: list[ReleaseFile] = []
    for path in sorted(release_root.rglob("*")):
        if path.is_symlink():
            raise ReleaseManifestError(
                f"release inventory does not allow symlinks: {path}"
            )
        if not path.is_file():
            continue
        digest, size = _sha256_file(path)
        entries.append(
            ReleaseFile(
                path=path.relative_to(release_root).as_posix(),
                sha256=digest,
                bytes=size,
                executable=bool(path.stat().st_mode & 0o111),
            )
        )
    if not entries:
        raise ReleaseManifestError("release directory contains no files")
    return entries


def describe_artifact(path: str | Path) -> ReleaseArtifact:
    artifact = Path(path)
    if not artifact.is_file():
        raise ReleaseManifestError(f"artifact does not exist: {artifact}")
    digest, size = _sha256_file(artifact)
    return ReleaseArtifact(
        filename=artifact.name,
        sha256=digest,
        bytes=size,
    )


def build_release_manifest(
    release_directory: str | Path,
    artifact_path: str | Path,
    *,
    product: str,
    version: str,
    key_id: str,
    channel: Literal["stable", "beta", "offline"] = "stable",
    minimum_version: str | None = None,
    minimum_data_version: int = 1,
    metadata: dict[str, Any] | None = None,
) -> ReleaseManifest:
    return ReleaseManifest(
        product=product,
        channel=channel,
        version=version,
        key_id=key_id,
        minimum_version=minimum_version,
        minimum_data_version=minimum_data_version,
        artifact=describe_artifact(artifact_path),
        files=build_release_inventory(release_directory),
        metadata=metadata or {},
    )


def public_key_from_base64(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ReleaseManifestError("invalid Ed25519 public key encoding") from exc
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise ReleaseManifestError("invalid Ed25519 public key") from exc


def private_key_from_base64(value: str) -> Ed25519PrivateKey:
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ReleaseManifestError("invalid Ed25519 private key encoding") from exc
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise ReleaseManifestError("invalid Ed25519 private key") from exc


def public_key_base64(key: Ed25519PublicKey) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def verify_release_manifest(
    manifest: ReleaseManifest,
    public_key: Ed25519PublicKey,
    *,
    expected_product: str | None = None,
    expected_channel: str | None = None,
) -> None:
    if not manifest.signature:
        raise ReleaseVerificationError("release manifest is not signed")
    if expected_product and manifest.product != expected_product:
        raise ReleaseVerificationError(
            f"release product mismatch: {manifest.product}"
        )
    if expected_channel and manifest.channel != expected_channel:
        raise ReleaseVerificationError(
            f"release channel mismatch: {manifest.channel}"
        )
    try:
        signature = base64.b64decode(manifest.signature, validate=True)
    except ValueError as exc:
        raise ReleaseVerificationError("invalid manifest signature encoding") from exc
    try:
        public_key.verify(signature, manifest.signing_payload())
    except InvalidSignature as exc:
        raise ReleaseVerificationError(
            "release manifest signature verification failed"
        ) from exc


def load_release_manifest(path: str | Path) -> ReleaseManifest:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(f"cannot read release manifest: {path}") from exc
    try:
        return ReleaseManifest.model_validate(payload)
    except Exception as exc:
        raise ReleaseManifestError(f"invalid release manifest: {exc}") from exc


def write_release_manifest(
    manifest: ReleaseManifest,
    path: str | Path,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination
