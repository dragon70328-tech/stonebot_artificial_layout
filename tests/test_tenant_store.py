import pytest

from src.tenant_store import (
    SqliteTenantRepository,
    TenantError,
    TenantQuota,
    TenantStore,
    build_tenant_store,
)


def test_project_directories_are_tenant_scoped(tmp_path):
    store = TenantStore(tmp_path / "tenants")

    path_a = store.create_project("tenant-a", "project-1")
    path_b = store.create_project("tenant-b", "project-1")

    assert path_a != path_b
    assert "tenant-a" in path_a.parts
    assert "tenant-b" in path_b.parts


def test_list_projects_only_returns_same_tenant(tmp_path):
    store = TenantStore(tmp_path / "tenants")
    store.create_project("tenant-a", "project-1")
    store.create_project("tenant-a", "project-2")
    store.create_project("tenant-b", "project-3")

    assert store.list_projects("tenant-a") == ["project-1", "project-2"]


def test_project_quota_is_enforced(tmp_path):
    store = TenantStore(
        tmp_path / "tenants",
        quota_by_tenant={"tenant-a": TenantQuota(max_projects=1)},
    )
    store.create_project("tenant-a", "project-1")

    with pytest.raises(TenantError):
        store.create_project("tenant-a", "project-2")


def test_invalid_ids_are_rejected(tmp_path):
    store = TenantStore(tmp_path / "tenants")

    with pytest.raises(TenantError):
        store.create_project("../bad", "project-1")


def test_artifact_store_is_project_scoped(tmp_path):
    store = TenantStore(tmp_path / "tenants")
    store.create_project("tenant-a", "project-1")
    artifacts = store.artifact_store("tenant-a", "project-1")
    ref = artifacts.put_json("tenant-a", "read", "recognized", {"ok": True})

    assert "tenant-a" in ref.path.parts
    assert "project-1" in ref.path.parts


def test_sqlite_tenant_repository_persists_projects_and_audit(tmp_path):
    repo = SqliteTenantRepository(tmp_path / "tenants.sqlite3")
    repo.save_project("tenant-a", "project-1", str(tmp_path / "tenant-a" / "project-1"))
    repo.record_audit(
        "tenant-a",
        "create_project",
        project_id="project-1",
        payload={"path": "tenant-a/project-1"},
    )

    reopened = SqliteTenantRepository(tmp_path / "tenants.sqlite3")

    assert reopened.list_projects("tenant-a") == ["project-1"]
    events = reopened.list_audit("tenant-a")
    assert len(events) == 1
    assert events[0]["event"] == "create_project"
    assert events[0]["project_id"] == "project-1"
    assert events[0]["path"] == "tenant-a/project-1"


def test_build_tenant_store_selects_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("STONEBOT_TENANT_STORE", "sqlite")

    store = build_tenant_store(tmp_path / "tenants")
    store.set_quota("tenant-a", TenantQuota(max_projects=1))
    store.create_project("tenant-a", "project-1")

    reopened = build_tenant_store(tmp_path / "tenants")

    assert reopened.list_projects("tenant-a") == ["project-1"]
    assert reopened._quota("tenant-a").max_projects == 1
    assert reopened.repository.list_audit("tenant-a")[0]["event"] == "create_project"

    with pytest.raises(TenantError):
        reopened.create_project("tenant-a", "project-2")
