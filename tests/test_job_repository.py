from src.job_repository import (
    FileJobRepository,
    SqliteJobRepository,
    build_job_repository,
)


def test_file_job_repository_round_trip(tmp_path):
    repo = FileJobRepository(tmp_path / "jobs")

    repo.save("job-1", {"tenant_id": "tenant-a", "dxf_path": "a.dxf"}, 123.0)

    assert repo.get("job-1")["request"]["tenant_id"] == "tenant-a"
    assert repo.list_job_ids() == ["job-1"]


def test_sqlite_job_repository_round_trip(tmp_path):
    repo = SqliteJobRepository(tmp_path / "jobs.sqlite3")

    repo.save("job-1", {"tenant_id": "tenant-a", "dxf_path": "a.dxf"}, 123.0)
    repo.save("job-2", {"tenant_id": "tenant-b", "dxf_path": "b.dxf"}, 124.0)

    assert repo.get("job-2")["request"]["tenant_id"] == "tenant-b"
    assert repo.list_job_ids() == ["job-1", "job-2"]


def test_build_job_repository_selects_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("STONEBOT_JOB_STORE", "sqlite")

    assert isinstance(build_job_repository(tmp_path), SqliteJobRepository)

    monkeypatch.delenv("STONEBOT_JOB_STORE", raising=False)
    assert isinstance(build_job_repository(tmp_path), FileJobRepository)
