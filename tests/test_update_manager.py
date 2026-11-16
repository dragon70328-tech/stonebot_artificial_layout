"""Secure release staging, activation and rollback tests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from src.local_runtime import LocalRuntimeLayout
from src.release_manifest import (
    build_release_manifest,
    verify_release_manifest,
)
from src.update_manager import (
    UpdateInstaller,
    UpdateVerificationError,
    candidate_for_update,
    compare_versions,
    fetch_release_manifest,
)


def test_update_stage_activate_and_rollback(tmp_path):
    layout = LocalRuntimeLayout.resolve(tmp_path / "runtime-root")
    installer = UpdateInstaller(layout)
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    release_1 = tmp_path / "release-v1"
    release_1.mkdir()
    (release_1 / "stonebot.exe").write_bytes(b"exe-v1")
    (release_1 / "core.bin").write_bytes(b"core-v1")
    artifact_1 = tmp_path / "v1.zip"
    with zipfile.ZipFile(artifact_1, "w") as archive:
        archive.write(release_1 / "stonebot.exe", "stonebot.exe")
        archive.write(release_1 / "core.bin", "core.bin")
    manifest_1 = build_release_manifest(
        release_1,
        artifact_1,
        product="stonebot",
        version="1.0.0",
        key_id="test-key",
    ).sign(private)

    staged_1 = installer.stage_release(manifest_1, artifact_1, public)
    assert (staged_1 / "core.bin").read_bytes() == b"core-v1"
    pointer_1 = installer.activate(manifest_1, public)
    assert pointer_1["active"]["version"] == "1.0.0"

    release_2 = tmp_path / "release-v2"
    release_2.mkdir()
    (release_2 / "stonebot.exe").write_bytes(b"exe-v2")
    (release_2 / "core.bin").write_bytes(b"core-v2")
    artifact_2 = tmp_path / "v2.zip"
    with zipfile.ZipFile(artifact_2, "w") as archive:
        archive.write(release_2 / "stonebot.exe", "stonebot.exe")
        archive.write(release_2 / "core.bin", "core.bin")
    manifest_2 = build_release_manifest(
        release_2,
        artifact_2,
        product="stonebot",
        version="1.1.0",
        key_id="test-key",
    ).sign(private)
    installer.stage_release(manifest_2, artifact_2, public)
    pointer_2 = installer.activate(manifest_2, public)
    assert pointer_2["active"]["version"] == "1.1.0"
    assert pointer_2["previous"]["version"] == "1.0.0"

    rolled_back = installer.rollback()
    assert rolled_back["active"]["version"] == "1.0.0"
    assert rolled_back["previous"]["version"] == "1.1.0"
    assert json.loads(
        layout.current_release_path.read_text(encoding="utf-8")
    )["active"]["version"] == "1.0.0"


def test_update_rejects_tampered_artifact(tmp_path):
    layout = LocalRuntimeLayout.resolve(tmp_path / "runtime-root")
    installer = UpdateInstaller(layout)
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    release = tmp_path / "release"
    release.mkdir()
    (release / "app.bin").write_bytes(b"app")
    artifact = tmp_path / "release.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.write(release / "app.bin", "app.bin")
    manifest = build_release_manifest(
        release,
        artifact,
        product="stonebot",
        version="2.0.0",
        key_id="test-key",
    ).sign(private)
    artifact.write_bytes(artifact.read_bytes() + b"tamper")

    with pytest.raises(UpdateVerificationError, match="size mismatch|hash mismatch"):
        installer.stage_release(manifest, artifact, public)


def test_update_rejects_unsafe_archive_path(tmp_path):
    layout = LocalRuntimeLayout.resolve(tmp_path / "runtime-root")
    installer = UpdateInstaller(layout)
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    release = tmp_path / "release"
    release.mkdir()
    (release / "app.bin").write_bytes(b"app")
    artifact = tmp_path / "malicious.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.write(release / "app.bin", "app.bin")
        archive.writestr("../escape.txt", "bad")
    manifest = build_release_manifest(
        release,
        artifact,
        product="stonebot",
        version="2.0.0",
        key_id="test-key",
    ).sign(private)

    with pytest.raises(UpdateVerificationError, match="unsafe artifact path"):
        installer.stage_release(manifest, artifact, public)
    assert not (layout.versions_dir / "escape.txt").exists()
    assert not (layout.root / "escape.txt").exists()


def test_update_candidate_and_version_comparison(tmp_path):
    private = Ed25519PrivateKey.generate()
    release = tmp_path / "release"
    release.mkdir()
    (release / "app.bin").write_bytes(b"app")
    artifact = tmp_path / "release.zip"
    artifact.write_bytes(b"artifact")
    manifest = build_release_manifest(
        release,
        artifact,
        product="stonebot",
        version="1.1.0",
        key_id="test-key",
        minimum_version="1.0.0",
        minimum_data_version=2,
    ).sign(private)

    assert compare_versions("1.0.0", "1.0.0-beta.1") > 0
    assert compare_versions("1.10.0", "1.2.0") > 0
    candidate = candidate_for_update(
        manifest,
        current_version="1.0.0",
        current_data_version=2,
    )
    assert candidate.update_available is True
    blocked = candidate_for_update(
        manifest,
        current_version="0.9.0",
        current_data_version=2,
    )
    assert blocked.update_available is False
    assert "requires at least version" in str(blocked.reason)


def test_update_manifest_and_artifact_require_https():
    private = Ed25519PrivateKey.generate()
    with pytest.raises(UpdateVerificationError, match="must use HTTPS"):
        fetch_release_manifest(
            "http://updates.example.com/release.json",
            private.public_key(),
        )
