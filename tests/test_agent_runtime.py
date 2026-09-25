from src.agent_runtime import AgentRuntime
from src.constraints import PROFILE_MIN_SHEETS
from src.object_store import LocalObjectStore
from src.units import UnitSystem
from src.workflow_session import WorkflowSession, WorkflowStage


def _ok_handlers():
    def ok(arguments):
        return {"ok": True}

    return {
        "analyze_drawing": ok,
        "match_profile": ok,
        "read_dxf": ok,
        "audit_drawing": ok,
        "assign_numbers": ok,
        "write_numbered_dxf": ok,
        "confirm_numbering": ok,
        "nest_parts": ok,
        "postprocess": ok,
        "write_dxf": ok,
        "write_report": ok,
    }


def _make_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    return AgentRuntime(
        "demo.dxf",
        3200,
        1600,
        20,
        UnitSystem.METRIC,
        PROFILE_MIN_SHEETS,
        provider_name="queue",
        trials=1,
        seed=0,
        budget=0,
        quick=True,
        pairing=False,
        skip_unnumbered=True,
        output_dir=tmp_path,
        session_id="runtime-1",
    )


def test_agent_runtime_runs_to_first_confirmation(tmp_path, monkeypatch):
    runtime = _make_runtime(tmp_path, monkeypatch)

    result = runtime.run_next()

    assert result.confirmation_required is True
    assert result.stage == WorkflowStage.AUDITED.value
    assert runtime.status()["stage"] == WorkflowStage.AUDITED.value


def test_agent_runtime_confirm_advances_through_all_gates(tmp_path, monkeypatch):
    runtime = _make_runtime(tmp_path, monkeypatch)
    runtime.run_next()

    nested = runtime.confirm("yes")
    assert nested.stage == WorkflowStage.NESTED_REPORTED.value
    assert nested.confirmation_required is True

    postprocess = runtime.confirm("confirm")
    assert postprocess.stage == WorkflowStage.POSTPROCESS_CONFIRMED.value
    assert postprocess.confirmation_required is True

    completed = runtime.confirm("y")
    assert completed.completed is True
    assert runtime.status()["completed"] is True


def test_agent_runtime_rejects_confirmation(tmp_path, monkeypatch):
    runtime = _make_runtime(tmp_path, monkeypatch)
    runtime.run_next()

    result = runtime.confirm("no")

    assert result.completed is False
    assert result.confirmation_required is False
    assert runtime.status()["stage"] == WorkflowStage.CANCELLED.value


def test_agent_runtime_confirm_without_gate_raises(tmp_path, monkeypatch):
    runtime = _make_runtime(tmp_path, monkeypatch)

    try:
        runtime.confirm("yes")
    except RuntimeError as exc:
        assert "no confirmation gate" in str(exc)
    else:
        raise AssertionError("RuntimeError was not raised")


def test_agent_runtime_records_existing_artifacts(tmp_path, monkeypatch):
    runtime = _make_runtime(tmp_path, monkeypatch)
    artifact_path = tmp_path / "numbered.dxf"
    artifact_path.write_text("fake dxf", encoding="utf-8")
    runtime.context.numbered_dxf_path = artifact_path

    runtime._record_artifacts()

    ref = runtime.session.artifacts["numbered_dxf"]
    assert ref["path"] == str(artifact_path)
    assert ref["digest"]
    assert "session_artifacts" in runtime.status()


def test_agent_runtime_records_object_key_for_artifact(tmp_path, monkeypatch):
    runtime = _make_runtime(tmp_path, monkeypatch)
    artifact_path = tmp_path / "numbered.dxf"
    artifact_path.write_text("fake dxf", encoding="utf-8")
    runtime.context.numbered_dxf_path = artifact_path
    runtime.object_store = LocalObjectStore(tmp_path / "objects")

    runtime._record_artifacts()

    ref = runtime.session.artifacts["numbered_dxf"]
    assert ref["object_key"].startswith("jobs/runtime-1/artifacts/")
    assert runtime.object_store.get_bytes(ref["object_key"]) == b"fake dxf"


def test_agent_runtime_resume_from_non_gate_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    session = WorkflowSession(session_id="runtime-resume")
    session.stage = WorkflowStage.NUMBERING_CONFIRMED
    runtime = AgentRuntime(
        "demo.dxf",
        3200,
        1600,
        20,
        UnitSystem.METRIC,
        PROFILE_MIN_SHEETS,
        provider_name="queue",
        trials=1,
        seed=0,
        budget=0,
        quick=True,
        pairing=False,
        skip_unnumbered=True,
        output_dir=tmp_path,
        session_id="runtime-resume",
        restore_session=session,
    )

    result = runtime.resume()

    assert result.stage == WorkflowStage.NESTED_REPORTED.value
    assert result.confirmation_required is True


class _FailingObjectStore:
    """Object-store double that always fails."""

    def put_bytes(self, key, data):
        raise RuntimeError("s3 unavailable")

    def get_bytes(self, key):
        raise KeyError(key)

    def exists(self, key):
        return False

    def delete(self, key):
        return None


def test_agent_runtime_surfaces_object_store_failures(tmp_path, monkeypatch):
    runtime = _make_runtime(tmp_path, monkeypatch)
    artifact_path = tmp_path / "numbered.dxf"
    artifact_path.write_text("fake dxf", encoding="utf-8")
    runtime.context.numbered_dxf_path = artifact_path
    runtime.object_store = _FailingObjectStore()

    runtime._record_artifacts()

    ref = runtime.session.artifacts["numbered_dxf"]
    assert "object_key" not in ref
    assert ref["object_store_error"] == "RuntimeError: s3 unavailable"
    assert runtime.status()["artifact_sync_errors"] == [
        {
            "artifact": "numbered_dxf",
            "object_key": "jobs/runtime-1/artifacts/numbered_dxf/numbered.dxf",
            "error": "RuntimeError: s3 unavailable",
        }
    ]


def test_agent_runtime_clears_sync_errors_after_recovery(tmp_path, monkeypatch):
    runtime = _make_runtime(tmp_path, monkeypatch)
    artifact_path = tmp_path / "numbered.dxf"
    artifact_path.write_text("fake dxf", encoding="utf-8")
    runtime.context.numbered_dxf_path = artifact_path
    runtime.object_store = _FailingObjectStore()
    runtime._record_artifacts()
    assert runtime.status()["artifact_sync_errors"]

    runtime.object_store = LocalObjectStore(tmp_path / "objects")
    runtime._record_artifacts()

    assert runtime.status()["artifact_sync_errors"] == []
    ref = runtime.session.artifacts["numbered_dxf"]
    assert "object_store_error" not in ref
    assert ref["object_key"].startswith("jobs/runtime-1/artifacts/")
