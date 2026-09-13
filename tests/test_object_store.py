import io

import pytest

from src.object_store import LocalObjectStore, S3ObjectStore, build_object_store


class FakeS3Error(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise FakeS3Error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise FakeS3Error("404")
        return {}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def test_local_object_store_put_and_get(tmp_path):
    store = LocalObjectStore(tmp_path)

    ref = store.put_bytes("tenant-a/project-1/input.dxf", b"DXF")

    assert store.exists("tenant-a/project-1/input.dxf") is True
    assert store.get_bytes("tenant-a/project-1/input.dxf") == b"DXF"
    assert ref["key"] == "tenant-a/project-1/input.dxf"


def test_local_object_store_rejects_path_traversal(tmp_path):
    store = LocalObjectStore(tmp_path)

    with pytest.raises(ValueError):
        store.put_bytes("../escape.dxf", b"bad")


def test_local_object_store_delete(tmp_path):
    store = LocalObjectStore(tmp_path)
    store.put_bytes("a/b.dxf", b"data")

    store.delete("a/b.dxf")

    assert store.exists("a/b.dxf") is False
    store.delete("a/b.dxf")


def test_build_object_store_defaults_to_local(tmp_path, monkeypatch):
    monkeypatch.delenv("STONEBOT_OBJECT_STORE", raising=False)
    monkeypatch.delenv("STONEBOT_S3_BUCKET", raising=False)

    store = build_object_store(tmp_path)

    assert isinstance(store, LocalObjectStore)
    assert store.root == tmp_path


def test_build_object_store_s3_requires_bucket(monkeypatch):
    monkeypatch.setenv("STONEBOT_OBJECT_STORE", "s3")
    monkeypatch.delenv("STONEBOT_S3_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="STONEBOT_S3_BUCKET"):
        build_object_store()


def test_s3_object_store_put_get_exists_delete_with_injected_client():
    client = FakeS3Client()
    store = S3ObjectStore(
        "local-bucket",
        endpoint_url="http://localhost:9000",
        client=client,
        client_error=FakeS3Error,
    )

    ref = store.put_bytes("tenant-a/project-1/input.dxf", b"DXF")

    assert ref["key"] == "tenant-a/project-1/input.dxf"
    assert store.exists("tenant-a/project-1/input.dxf") is True
    assert store.get_bytes("tenant-a/project-1/input.dxf") == b"DXF"
    assert store.exists("tenant-a/project-1/missing.dxf") is False

    store.delete("tenant-a/project-1/input.dxf")

    assert store.exists("tenant-a/project-1/input.dxf") is False


def test_s3_object_store_applies_prefix():
    store = S3ObjectStore(
        "local-bucket",
        prefix="stonebot",
        client=FakeS3Client(),
        client_error=FakeS3Error,
    )

    ref = store.put_bytes("tenant-a/input.dxf", b"DXF")

    assert ref["key"] == "stonebot/tenant-a/input.dxf"
    assert store.exists("tenant-a/input.dxf") is True


def test_build_object_store_minio_preset(monkeypatch):
    monkeypatch.setenv("STONEBOT_OBJECT_STORE", "minio")
    monkeypatch.setenv("STONEBOT_S3_BUCKET", "local-bucket")
    captured = {}

    def fake_s3(bucket, **kwargs):
        captured["bucket"] = bucket
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("src.object_store.S3ObjectStore", fake_s3)
    build_object_store()

    assert captured["bucket"] == "local-bucket"
    assert captured["endpoint_url"] == "http://localhost:9000"
    assert captured["access_key_id"] == "minioadmin"
    assert captured["secret_access_key"] == "minioadmin"


def test_build_object_store_localstack_preset(monkeypatch):
    monkeypatch.setenv("STONEBOT_OBJECT_STORE", "localstack")
    monkeypatch.setenv("STONEBOT_S3_BUCKET", "local-bucket")
    captured = {}

    def fake_s3(bucket, **kwargs):
        captured["bucket"] = bucket
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("src.object_store.S3ObjectStore", fake_s3)
    build_object_store()

    assert captured["bucket"] == "local-bucket"
    assert captured["endpoint_url"] == "http://localhost:4566"
    assert captured["access_key_id"] == "test"
    assert captured["secret_access_key"] == "test"
