from src.artifact_store import ArtifactStore, content_digest


def test_artifact_store_writes_content_addressed_json(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_json(
        "project-1",
        "read",
        "recognized",
        {"panels": 2},
        stage_version="v1",
    )

    assert ref.digest == content_digest(
        b'{\n  "panels": 2\n}'
    )
    assert "read:" in ref.artifact_id
    assert ref.path.exists()
    assert store.read_json(ref) == {"panels": 2}


def test_artifact_store_same_version_is_idempotent(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_text(
        "project-1",
        "audit",
        "report",
        "same",
        extension="txt",
        stage_version="v1",
    )
    second = store.put_text(
        "project-1",
        "audit",
        "report",
        "same",
        extension="txt",
        stage_version="v1",
    )

    assert first.path == second.path
    assert first.digest == second.digest


def test_artifact_store_different_versions_do_not_overwrite(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_text(
        "project-1",
        "nest",
        "layout",
        "v1",
        extension="txt",
        stage_version="v1",
    )
    second = store.put_text(
        "project-1",
        "nest",
        "layout",
        "v2",
        extension="txt",
        stage_version="v2",
    )

    assert first.path != second.path
    assert first.path.exists()
    assert second.path.exists()
