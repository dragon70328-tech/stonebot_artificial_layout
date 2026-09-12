import time

from fastapi.testclient import TestClient

from src.http_api import (
    JobCreateRequest,
    JobManager,
    JobStateError,
    app,
)
from src.task_queue import Task, TaskStatus
from src.workflow_session import WorkflowStage


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


class SyncJobManager(JobManager):
    def create(self, request, *, run_in_background=True):
        runtime = super().create(request, run_in_background=False)
        runtime.run_next()
        return runtime

    def confirm(self, job_id, decision, *, run_in_background=True):
        return super().confirm(job_id, decision, run_in_background=False)

    def run(self, job_id, *, run_in_background=True):
        return super().run(job_id, run_in_background=False)


def _job_request(tmp_path):
    return JobCreateRequest(
        dxf_path="demo.dxf",
        width_mm=3200,
        height_mm=1600,
        thickness_mm=20,
        profile="min_sheets",
        provider="queue",
        trials=1,
        seed=0,
        budget=0,
        quick=True,
        pairing=False,
        skip_unnumbered=True,
    )


def test_job_manager_confirm_without_gate_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = JobManager(tmp_path)
    runtime = manager.create(_job_request(tmp_path), run_in_background=False)

    try:
        manager.confirm(runtime.session_id, "yes", run_in_background=False)
    except JobStateError as exc:
        assert "no confirmation gate" in str(exc)
    else:
        raise AssertionError("JobStateError was not raised")


def test_http_job_flow_reaches_three_confirmations(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setattr("src.http_api.manager", SyncJobManager(tmp_path))
    client = TestClient(app)

    created = client.post("/jobs", json=_job_request(tmp_path).model_dump())
    assert created.status_code == 202
    body = created.json()
    job_id = body["job_id"]
    assert body["stage"] == "audited"
    assert body["confirmation_required"] is True

    nested = client.post(
        f"/jobs/{job_id}/confirm",
        json={"decision": "yes"},
    )
    assert nested.status_code == 202
    assert nested.json()["stage"] == "nested_reported"

    postprocess = client.post(
        f"/jobs/{job_id}/confirm",
        json={"decision": "confirm"},
    )
    assert postprocess.status_code == 202
    assert postprocess.json()["stage"] == "postprocess_confirmed"

    completed = client.post(
        f"/jobs/{job_id}/confirm",
        json={"decision": "y"},
    )
    assert completed.status_code == 202
    assert completed.json()["completed"] is True


def test_http_health(tmp_path, monkeypatch):
    monkeypatch.setattr("src.http_api.manager", SyncJobManager(tmp_path))
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_upload_input_accepts_dxf_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr("src.http_api.manager", SyncJobManager(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/inputs/demo.dxf",
        content=b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n",
        headers={"Content-Type": "application/octet-stream"},
        params={"tenant_id": "tenant-a", "project_id": "project-1"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["dxf_path"].endswith(".dxf")
    assert body["bytes"] > 0
    assert body["digest"]
    assert "tenant-a" in body["dxf_path"]
    assert "project-1" in body["dxf_path"]


def test_upload_input_rejects_non_dxf(tmp_path, monkeypatch):
    monkeypatch.setattr("src.http_api.manager", SyncJobManager(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/inputs/demo.pdf",
        content=b"not a dxf",
        headers={"Content-Type": "application/octet-stream"},
        params={"tenant_id": "tenant-a", "project_id": "project-1"},
    )

    assert response.status_code == 400


def test_job_manager_recovers_session_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    root = tmp_path / "manager-root"
    request = _job_request(tmp_path)

    first_manager = JobManager(root)
    first_runtime = first_manager.create(request, run_in_background=False)
    first_runtime.run_next()
    job_id = first_runtime.session_id

    second_manager = JobManager(root)
    recovered = second_manager.get(job_id)

    assert recovered.session.current_stage == "audited"
    assert recovered.last_result.confirmation_required is True

    second_manager.confirm(job_id, "yes", run_in_background=False)
    assert recovered.session.current_stage == "nested_reported"


def test_download_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    runtime = manager.create(_job_request(tmp_path), run_in_background=False)
    artifact_path = tmp_path / "report.json"
    artifact_path.write_text('{"sheets": 2}', encoding="utf-8")
    runtime.session.artifacts["report"] = {
        "path": str(artifact_path),
        "digest": "test-digest",
        "artifact_id": "report:test",
    }

    response = client.get(
        f"/jobs/{runtime.session_id}/artifacts/report",
    )

    assert response.status_code == 200
    assert response.content == b'{"sheets": 2}'


def test_run_endpoint_resumes_non_gate_job(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    runtime = manager.create(_job_request(tmp_path), run_in_background=False)
    runtime.session.stage = WorkflowStage.NUMBERING_CONFIRMED
    runtime.last_result = None

    response = client.post(f"/jobs/{runtime.session_id}/run")

    assert response.status_code == 202
    assert response.json()["stage"] == "nested_reported"
    assert response.json()["confirmation_required"] is True


def test_background_job_is_driven_by_file_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = JobManager(tmp_path)

    runtime = manager.create(_job_request(tmp_path), run_in_background=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        if manager.get(runtime.session_id).session.current_stage == "audited":
            break
        time.sleep(0.05)

    assert manager.get(runtime.session_id).session.current_stage == "audited"
    assert manager.task_queue.has_pending() is False
    manager._stop_event.set()


def test_list_job_tasks_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = JobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    runtime = manager.create(_job_request(tmp_path), run_in_background=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        if manager.get(runtime.session_id).session.current_stage == "audited":
            break
        time.sleep(0.05)

    response = client.get(f"/jobs/{runtime.session_id}/tasks")

    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == "run_job"
    assert tasks[0]["status"] == "succeeded"
    manager._stop_event.set()


def test_retry_dead_letter_task_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    monkeypatch.setattr(manager, "_ensure_worker", lambda: None)
    client = TestClient(app)

    runtime = manager.create(_job_request(tmp_path), run_in_background=False)
    task = Task(
        task_type="run_job",
        payload={"job_id": runtime.session_id},
        status=TaskStatus.DEAD_LETTER,
    )
    manager.task_queue.enqueue(task)

    response = client.post(
        f"/jobs/{runtime.session_id}/tasks/{task.task_id}/retry",
    )

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["attempts"] == 0


def test_job_manager_shutdown_stops_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = JobManager(tmp_path)
    runtime = manager.create(_job_request(tmp_path), run_in_background=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        if manager.get(runtime.session_id).session.current_stage == "audited":
            break
        time.sleep(0.05)

    manager.shutdown()

    assert manager._worker_thread is not None
    assert manager._worker_thread.is_alive() is False
