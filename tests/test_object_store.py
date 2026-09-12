import pytest

from src.object_store import LocalObjectStore


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
