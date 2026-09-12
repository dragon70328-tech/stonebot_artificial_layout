from src.session_store import WorkflowSessionStore
from src.workflow_session import WorkflowSession, WorkflowStage


def test_workflow_session_roundtrip_preserves_stage_history_and_artifacts(tmp_path):
    session = WorkflowSession(session_id="agent-20260912-001")
    session.transition(WorkflowStage.ANALYZED, summary={"dxf": "a.dxf"})
    session.transition(WorkflowStage.PROFILE_MATCHED, summary={"profile": "cgr45"})
    session.record_artifact(
        "read:abc123",
        {"path": "output/a_numbered_原位.dxf", "digest": "abc123"},
    )

    store = WorkflowSessionStore(tmp_path)
    path = store.save(session)
    loaded = store.load(session.session_id)

    assert path.exists()
    assert loaded.session_id == session.session_id
    assert loaded.stage == WorkflowStage.PROFILE_MATCHED
    assert [record.stage for record in loaded.history] == [
        "analyzed",
        "profile_matched",
    ]
    assert loaded.artifacts == session.artifacts


def test_workflow_session_store_sanitizes_invalid_session_id(tmp_path):
    store = WorkflowSessionStore(tmp_path)

    path = store.path_for("../escape")

    assert path.parent == tmp_path
    assert path.name == "escape.json"


def test_workflow_session_store_lists_saved_sessions(tmp_path):
    store = WorkflowSessionStore(tmp_path)
    store.save(WorkflowSession(session_id="job-a"))
    store.save(WorkflowSession(session_id="job-b"))

    assert store.list_sessions() == ["job-a", "job-b"]
