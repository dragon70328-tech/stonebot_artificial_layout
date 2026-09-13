"""Object storage boundary for SaaS artifacts and inputs.

The HTTP layer should not assume files live on local disk forever. This module
defines a small key/value contract and a local implementation that can later
be replaced by an S3-compatible backend without changing callers.
"""

from __future__ import annotations

import os
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


class S3ObjectStore:
    """S3-compatible object store using the optional ``boto3`` dependency."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        prefix: str = "",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: object | None = None,
        client_error: type[Exception] | None = None,
    ) -> None:
        if client is not None:
            self._client_error = client_error or Exception
        else:
            try:
                import boto3
                from botocore.exceptions import ClientError
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "S3ObjectStore requires boto3; install boto3 to use "
                    "STONEBOT_OBJECT_STORE=s3"
                ) from exc
            self._client_error = ClientError
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is not None:
            self.client = client
        else:
            self.client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
            )

    def _key(self, key: str) -> str:
        clean = key.replace("\\", "/").lstrip("/")
        if (
            not clean
            or clean == ".."
            or clean.startswith("../")
            or "/../" in clean
        ):
            raise ValueError(f"object key escapes store prefix: {key!r}")
        return f"{self.prefix}/{clean}" if self.prefix else clean

    def put_bytes(self, key: str, data: bytes) -> dict[str, object]:
        object_key = self._key(key)
        self.client.put_object(Bucket=self.bucket, Key=object_key, Body=data)
        return {
            "key": object_key,
            "size": len(data),
            "store": "s3",
            "bucket": self.bucket,
        }

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(
            Bucket=self.bucket,
            Key=self._key(key),
        )
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except self._client_error as exc:
            response = getattr(exc, "response", {})
            error_code = str(response.get("Error", {}).get("Code", ""))
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


def _s3_options(mode: str) -> dict[str, object]:
    """Resolve S3 connection options, with local MinIO/LocalStack defaults."""
    endpoint_url = os.environ.get("STONEBOT_S3_ENDPOINT_URL")
    access_key_id = os.environ.get("STONEBOT_S3_ACCESS_KEY_ID")
    secret_access_key = os.environ.get("STONEBOT_S3_SECRET_ACCESS_KEY")
    if mode == "minio":
        endpoint_url = endpoint_url or "http://localhost:9000"
        access_key_id = access_key_id or "minioadmin"
        secret_access_key = secret_access_key or "minioadmin"
    elif mode == "localstack":
        endpoint_url = endpoint_url or "http://localhost:4566"
        access_key_id = access_key_id or "test"
        secret_access_key = secret_access_key or "test"
    return {
        "endpoint_url": endpoint_url,
        "region": os.environ.get("STONEBOT_S3_REGION"),
        "prefix": os.environ.get("STONEBOT_S3_PREFIX", ""),
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
    }


def build_object_store(root: str | Path | None = None) -> ObjectStore:
    """Create the configured object store.

    Defaults to a local filesystem store. Set ``STONEBOT_OBJECT_STORE=s3``,
    ``minio``, or ``localstack`` (or ``STONEBOT_S3_BUCKET``) to select the
    S3-compatible adapter. MinIO/LocalStack presets supply local endpoint and
    credential defaults while still allowing ``STONEBOT_S3_*`` overrides.
    """
    mode = os.environ.get("STONEBOT_OBJECT_STORE", "local").strip().lower()
    bucket = os.environ.get("STONEBOT_S3_BUCKET")
    if mode in {"s3", "minio", "localstack"} or bucket:
        if not bucket:
            raise RuntimeError(
                f"STONEBOT_S3_BUCKET is required when STONEBOT_OBJECT_STORE={mode}"
            )
        return S3ObjectStore(
            bucket,
            **_s3_options(mode),
        )
    return LocalObjectStore(root or Path("output") / "object_store")
