"""Signed release manifest tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from src.release_manifest import (
    ReleaseVerificationError,
    build_release_inventory,
    build_release_manifest,
    load_release_manifest,
    private_key_from_base64,
    public_key_base64,
    public_key_from_base64,
    verify_release_manifest,
    write_release_manifest,
)


def _key_pair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return private, public


def test_release_manifest_sign_verify_and_round_trip(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "stonebot.exe").write_bytes(b"runtime")
    core = release / "runtime" / "core.pyd"
    core.parent.mkdir()
    core.write_bytes(b"native-core")
    artifact = tmp_path / "stonebot-1.2.3.zip"
    artifact.write_bytes(b"artifact")
    private, public = _key_pair()

    manifest = build_release_manifest(
        release,
        artifact,
        product="stonebot",
        version="1.2.3",
        key_id="stable-2026",
        minimum_version="1.0.0",
        minimum_data_version=3,
        metadata={"notes": "test release"},
    ).sign(private)
    path = write_release_manifest(manifest, tmp_path / "manifest.json")
    loaded = load_release_manifest(path)

    verify_release_manifest(
        loaded,
        public,
        expected_product="stonebot",
        expected_channel="stable",
    )
    assert loaded.digest() == manifest.digest()
    assert {entry.path for entry in loaded.files} == {
        "stonebot.exe",
        "runtime/core.pyd",
    }


def test_release_manifest_tampering_is_rejected(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "stonebot.exe").write_bytes(b"runtime")
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact")
    private, public = _key_pair()
    manifest = build_release_manifest(
        release,
        artifact,
        product="stonebot",
        version="1.0.0",
        key_id="test-key",
    ).sign(private)

    manifest.version = "9.9.9"

    with pytest.raises(ReleaseVerificationError, match="verification failed"):
        verify_release_manifest(manifest, public)


def test_release_manifest_rejects_unsafe_version_and_paths(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "ok.txt").write_text("ok", encoding="utf-8")
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact")

    with pytest.raises(ValueError, match="invalid release version"):
        build_release_manifest(
            release,
            artifact,
            product="stonebot",
            version="../../escape",
            key_id="test-key",
        )

    (release / "ok.txt").unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = release / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="does not allow symlinks"):
        build_release_inventory(release)


def test_key_base64_round_trip():
    private, public = _key_pair()
    from cryptography.hazmat.primitives import serialization

    private_b64 = base64.b64encode(
        private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode("ascii")
    public_b64 = public_key_base64(public)

    assert public_key_from_base64(public_b64).public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ) == public.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    assert private_key_from_base64(private_b64).public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ) == public.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
