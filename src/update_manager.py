"""Secure staging, activation and rollback for local Runtime releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from src.local_runtime import LocalRuntimeLayout
from src.release_manifest import (
    ReleaseManifest,
    verify_release_manifest,
)


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
_VERSION_PATTERN = re.compile(
    r"^(?P<main>\d+(?:\.\d+)*)(?:[-+](?P<pre>[0-9A-Za-z.-]+))?$"
)


class UpdateError(RuntimeError):
    """Base error for update staging, activation and rollback."""


class UpdateVerificationError(UpdateError):
    """Raised when an artifact or installed release fails verification."""


@dataclass(frozen=True)
class UpdateCandidate:
    manifest: ReleaseManifest
    current_version: str
    update_available: bool
    reason: str | None = None


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


def version_key(value: str) -> tuple[tuple[int, ...], int, tuple]:
    """Deterministic SemVer-like key; stable releases sort after prereleases."""
    match = _VERSION_PATTERN.fullmatch(value.strip().lower())
    if match is None:
        raise UpdateError(f"invalid version: {value!r}")
    main = tuple(int(part) for part in match.group("main").split("."))
    prerelease = match.group("pre") or ""
    tokens = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"[.-]", prerelease)
        if part
    )
    return main, 0 if prerelease else 1, tokens


def compare_versions(left: str, right: str) -> int:
    left_key = version_key(left)
    right_key = version_key(right)
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


def candidate_for_update(
    manifest: ReleaseManifest,
    *,
    current_version: str,
    current_data_version: int,
    product: str = "stonebot",
    channel: str = "stable",
) -> UpdateCandidate:
    if manifest.product != product:
        raise UpdateVerificationError(
            f"release product mismatch: {manifest.product}"
        )
    if manifest.channel != channel:
        raise UpdateVerificationError(
            f"release channel mismatch: {manifest.channel}"
        )
    if current_data_version < 1:
        raise UpdateError("current_data_version must be positive")
    available = compare_versions(manifest.version, current_version) > 0
    reason = None
    if not available:
        reason = "already-current-or-newer"
    elif (
        manifest.minimum_version
        and compare_versions(current_version, manifest.minimum_version) < 0
    ):
        available = False
        reason = (
            f"release requires at least version {manifest.minimum_version}, "
            f"current is {current_version}"
        )
    elif manifest.minimum_data_version > current_data_version:
        available = False
        reason = (
            "release requires data version "
            f"{manifest.minimum_data_version}, current is {current_data_version}"
        )
    return UpdateCandidate(
        manifest=manifest,
        current_version=current_version,
        update_available=available,
        reason=reason,
    )


def verify_release_artifact(
    manifest: ReleaseManifest,
    artifact_path: str | Path,
) -> Path:
    path = Path(artifact_path)
    if not path.is_file():
        raise UpdateVerificationError(f"release artifact is missing: {path}")
    digest, size = _sha256_file(path)
    if size != manifest.artifact.bytes:
        raise UpdateVerificationError("release artifact size mismatch")
    if digest != manifest.artifact.sha256:
        raise UpdateVerificationError("release artifact hash mismatch")
    return path


def download_release_artifact(
    manifest: ReleaseManifest,
    url: str,
    layout: LocalRuntimeLayout,
    *,
    timeout_seconds: float = 30.0,
) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UpdateVerificationError(
            "release artifact URL must use HTTPS"
        )
    layout.ensure()
    destination = layout.downloads_dir / manifest.artifact.filename
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = Request(url, headers={"User-Agent": "StoneBot-Updater"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if urlparse(response.geturl()).scheme != "https":
                raise UpdateVerificationError(
                    "release artifact redirected to a non-HTTPS URL"
                )
            with temporary.open("wb") as sink:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    sink.write(chunk)
                    if sink.tell() > MAX_ARTIFACT_BYTES:
                        raise UpdateVerificationError(
                            "release artifact exceeds size limit"
                        )
        verify_release_artifact(manifest, temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def fetch_release_manifest(
    url: str,
    public_key: Ed25519PublicKey,
    *,
    expected_product: str = "stonebot",
    expected_channel: str = "stable",
    timeout_seconds: float = 20.0,
) -> ReleaseManifest:
    """Fetch and verify one HTTPS release manifest."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UpdateVerificationError(
            "release manifest URL must use HTTPS"
        )
    request = Request(url, headers={"User-Agent": "StoneBot-Updater"})
    with urlopen(request, timeout=timeout_seconds) as response:
        if urlparse(response.geturl()).scheme != "https":
            raise UpdateVerificationError(
                "release manifest redirected to a non-HTTPS URL"
            )
        data = response.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise UpdateVerificationError("release manifest exceeds size limit")
    try:
        payload = json.loads(data.decode("utf-8"))
        manifest = ReleaseManifest.model_validate(payload)
    except Exception as exc:
        raise UpdateVerificationError(
            f"release manifest is invalid: {exc}"
        ) from exc
    verify_release_manifest(
        manifest,
        public_key,
        expected_product=expected_product,
        expected_channel=expected_channel,
    )
    return manifest


class UpdateInstaller:
    """Stage and activate one signed release without replacing live files."""

    def __init__(
        self,
        layout: LocalRuntimeLayout,
        *,
        product: str = "stonebot",
        channel: str = "stable",
    ) -> None:
        self.layout = layout.ensure()
        self.product = product
        self.channel = channel

    def stage_release(
        self,
        manifest: ReleaseManifest,
        artifact_path: str | Path,
        public_key: Ed25519PublicKey,
        *,
        self_test: Callable[[Path], bool | None] | None = None,
    ) -> Path:
        verify_release_manifest(
            manifest,
            public_key,
            expected_product=self.product,
            expected_channel=self.channel,
        )
        artifact = verify_release_artifact(manifest, artifact_path)
        final_dir = self.layout.versions_dir / manifest.version
        if final_dir.exists():
            raise UpdateError(
                f"release version already staged: {manifest.version}"
            )

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{manifest.version}.staging-",
                dir=self.layout.update_staging_dir,
            )
        )
        try:
            self._extract_archive(artifact, staging)
            self._verify_installed_inventory(manifest, staging)
            if self_test is not None and self_test(staging) is False:
                raise UpdateVerificationError(
                    "release self-test returned false"
                )
            os.replace(staging, final_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return final_dir

    def verify_staged(
        self,
        manifest: ReleaseManifest,
        public_key: Ed25519PublicKey,
    ) -> Path:
        verify_release_manifest(
            manifest,
            public_key,
            expected_product=self.product,
            expected_channel=self.channel,
        )
        version_dir = self.layout.versions_dir / manifest.version
        if not version_dir.is_dir():
            raise UpdateVerificationError(
                f"release version is not staged: {manifest.version}"
            )
        self._verify_installed_inventory(manifest, version_dir)
        return version_dir

    def activate(
        self,
        manifest: ReleaseManifest,
        public_key: Ed25519PublicKey,
    ) -> dict:
        version_dir = self.verify_staged(manifest, public_key)
        current = self.read_current()
        active = {
            "version": manifest.version,
            "manifest_digest": manifest.digest(),
            "activated_at": _utc_now(),
        }
        pointer = {
            "schema_version": 1,
            "active": active,
            "previous": (
                current.get("active")
                if current.get("active", {}).get("version")
                != manifest.version
                else current.get("previous")
            ),
            "release_directory": str(version_dir),
        }
        self._write_current(pointer)
        return pointer

    def rollback(self) -> dict:
        current = self.read_current()
        active = current.get("active") or {}
        previous = current.get("previous") or {}
        version = str(previous.get("version") or "")
        if not version:
            raise UpdateError("no previous release is available")
        version_dir = self.layout.versions_dir / version
        if not version_dir.is_dir():
            raise UpdateError(
                f"previous release directory is missing: {version_dir}"
            )
        pointer = {
            "schema_version": 1,
            "active": {
                **previous,
                "activated_at": _utc_now(),
            },
            "previous": active or None,
            "release_directory": str(version_dir),
        }
        self._write_current(pointer)
        return pointer

    def read_current(self) -> dict:
        path = self.layout.current_release_path
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError(f"invalid current release pointer: {exc}") from exc
        if int(payload.get("schema_version", 0)) != 1:
            raise UpdateError("unsupported current release pointer version")
        return payload

    def _write_current(self, pointer: dict) -> None:
        path = self.layout.current_release_path
        temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @staticmethod
    def _safe_archive_path(name: str) -> PurePosixPath:
        pure = PurePosixPath(name)
        if pure.is_absolute():
            raise UpdateVerificationError(
                f"absolute artifact path is not allowed: {name}"
            )
        if any(part in {"", ".", ".."} for part in pure.parts):
            raise UpdateVerificationError(f"unsafe artifact path: {name}")
        if ":" in pure.parts[0]:
            raise UpdateVerificationError(
                f"drive-qualified artifact path is not allowed: {name}"
            )
        return pure

    def _extract_archive(self, artifact: Path, staging: Path) -> None:
        try:
            archive = zipfile.ZipFile(artifact, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise UpdateVerificationError("release artifact is not a ZIP") from exc
        with archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > MAX_ARCHIVE_FILES:
                raise UpdateVerificationError(
                    "release artifact contains too many files"
                )
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise UpdateVerificationError(
                    "release artifact contains duplicate paths"
                )
            for info in infos:
                pure = self._safe_archive_path(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise UpdateVerificationError(
                        f"release artifact contains a symlink: {pure}"
                    )
                target = (staging / pure).resolve()
                if staging.resolve() not in target.parents:
                    raise UpdateVerificationError(
                        f"release artifact escaped staging root: {pure}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)

    @staticmethod
    def _verify_installed_inventory(
        manifest: ReleaseManifest,
        version_dir: Path,
    ) -> None:
        expected = {entry.path: entry for entry in manifest.files}
        actual: set[str] = set()
        for path in version_dir.rglob("*"):
            if path.is_symlink():
                raise UpdateVerificationError(
                    f"staged release contains a symlink: {path}"
                )
            if not path.is_file():
                continue
            relative = path.relative_to(version_dir).as_posix()
            actual.add(relative)
            entry = expected.get(relative)
            if entry is None:
                raise UpdateVerificationError(
                    f"staged release contains an unexpected file: {relative}"
                )
            digest, size = _sha256_file(path)
            if size != entry.bytes or digest != entry.sha256:
                raise UpdateVerificationError(
                    f"staged release file failed verification: {relative}"
                )
        missing = sorted(set(expected) - actual)
        if missing:
            raise UpdateVerificationError(
                f"staged release is missing files: {missing[:5]}"
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
