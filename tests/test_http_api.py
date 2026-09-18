import asyncio
import time
from types import SimpleNamespace

import httpx
from httpx import ASGITransport

from src.http_api import (
    JobCreateRequest,
    JobManager,
    JobStateError,
    app,
)
from src.task_queue import Task, TaskStatus
from src.workflow_session import WorkflowStage


class SyncASGIClient:
    def __init__(self, app):
        self._transport = ASGITransport(app=app)
        self._base_url = "http://testserver"

    def _request(self, method, path, **kwargs):
        async def run():
            async with httpx.AsyncClient(
                transport=self._transport,
                base_url=self._base_url,
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(run())

    def get(self, path, **kwargs):
        return self._request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self._request("POST", path, **kwargs)


def TestClient(app):
    """Synchronous ASGI test client that avoids fastapi.testclient's warning."""
    return SyncASGIClient(app)


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


def _tenant_request(tmp_path, tenant_id):
    payload = _job_request(tmp_path).model_dump()
    payload["tenant_id"] = tenant_id
    return JobCreateRequest(**payload)


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


def test_job_manager_can_disable_inline_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("STONEBOT_WORKER_ENABLED", "0")
    manager = JobManager(tmp_path)

    manager._ensure_worker()

    assert manager._worker_thread is None


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
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "stonebot-nesting-api"
    assert payload["data_root"] == str(tmp_path)


class FakeCorrectionRuntime:
    def __init__(self):
        self.context = SimpleNamespace(
            parts=[],
            recognized_drawing=None,
            correction_revision=0,
        )
        self.session = SimpleNamespace(current_stage="audited")

    def status(self):
        return {
            "job_id": "job-correction",
            "stage": "audited",
            "confirmation_required": True,
        }


class FakeCorrectionManager:
    def __init__(self, root):
        self.root = root
        self.runtime = FakeCorrectionRuntime()

    def tenant_for_job(self, job_id):
        return "default"

    def get(self, job_id):
        return self.runtime

    def correct(self, job_id, plan):
        self.corrected = (job_id, plan)
        return {
            "revision": 1,
            "plan_id": plan.plan_id,
            "artifact_path": "corrected.dxf",
        }


def test_correction_endpoint_applies_structured_plan(tmp_path, monkeypatch):
    manager = FakeCorrectionManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    response = client.post(
        "/jobs/job-correction/corrections",
        json={
            "instruction": "把 P-1 改成 P-1A",
            "operations": [
                {
                    "operation": "rename_number",
                    "target": "P-1",
                    "value": "P-1A",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["correction"]["revision"] == 1
    assert manager.corrected[0] == "job-correction"
    assert manager.corrected[1].operations[0].value == "P-1A"


def test_root_serves_nesting_chat_frontend(tmp_path, monkeypatch):
    monkeypatch.setattr("src.http_api.manager", SyncJobManager(tmp_path))
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "识图排板对话" in response.text


def test_root_serves_portal_ui_module(tmp_path, monkeypatch):
    monkeypatch.setattr("src.http_api.manager", SyncJobManager(tmp_path))
    client = TestClient(app)

    response = client.get("/portal-ui.js")

    assert response.status_code == 200
    assert "StonebotUI" in response.text
    assert "stageLabel" in response.text


def test_nesting_frontend_has_action_contract(tmp_path, monkeypatch):
    monkeypatch.setattr("src.http_api.manager", SyncJobManager(tmp_path))
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    for element_id in (
        "job-panel",
        "workflow-steps",
        "parameter-panel",
        "param-process",
        "param-width",
        "param-height",
        "param-thickness",
        "param-usable-width",
        "param-usable-height",
        "param-oversize-enabled",
        "param-oversize-mode",
        "param-oversize-width",
        "param-oversize-height",
        "param-kerf",
        "derived-size",
        "btn-status",
        "btn-confirm",
        "btn-run",
        "btn-review",
        "btn-cancel",
    ):
        assert f'id="{element_id}"' in response.text

def test_upload_input_accepts_dxf_bytes(tmp_path, monkeypatch):
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
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
    assert body["object_key"].startswith(
        "tenants/tenant-a/projects/project-1/inputs/"
    )
    assert "tenant-a" in body["dxf_path"]
    assert "project-1" in body["dxf_path"]
    assert manager.object_store.exists(body["object_key"]) is True


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


def test_job_manager_uses_sqlite_job_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setenv("STONEBOT_JOB_STORE", "sqlite")
    root = tmp_path / "manager-root"
    request = _job_request(tmp_path)

    first_manager = JobManager(root)
    first_runtime = first_manager.create(request, run_in_background=False)
    first_runtime.run_next()
    job_id = first_runtime.session_id

    second_manager = JobManager(root)
    recovered = second_manager.get(job_id)

    assert recovered.session.current_stage == "audited"
    assert second_manager.tenant_for_job(job_id) == "default"


def test_job_manager_uses_sqlite_session_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setenv("STONEBOT_JOB_STORE", "sqlite")
    monkeypatch.setenv("STONEBOT_SESSION_STORE", "sqlite")
    root = tmp_path / "manager-root"
    request = _job_request(tmp_path)

    first_manager = JobManager(root)
    first_runtime = first_manager.create(request, run_in_background=False)
    first_runtime.run_next()
    job_id = first_runtime.session_id

    second_manager = JobManager(root)
    recovered = second_manager.get(job_id)

    assert recovered.session.current_stage == "audited"
    assert second_manager.session_store.exists(job_id) is True


def test_job_manager_uses_sqlite_tenant_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setenv("STONEBOT_TENANT_STORE", "sqlite")
    root = tmp_path / "manager-root"
    request = _tenant_request(tmp_path, "tenant-a")

    first_manager = JobManager(root)
    first_manager.create(request, run_in_background=False)

    second_manager = JobManager(root)

    assert second_manager.tenant_store.list_projects("tenant-a") == ["demo"]
    assert (
        second_manager.tenant_store.repository.list_audit("tenant-a")[0]["event"]
        == "create_project"
    )


def test_job_manager_uses_unified_state_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setenv("STONEBOT_STATE_DB", "unified")
    root = tmp_path / "manager-root"
    request = _job_request(tmp_path)

    first_manager = JobManager(root)
    runtime = first_manager.create(request, run_in_background=False)
    runtime.run_next()
    job_id = runtime.session_id

    second_manager = JobManager(root)
    recovered = second_manager.get(job_id)

    assert recovered.session.current_stage == "audited"
    assert second_manager.tenant_store.list_projects("default") == ["demo"]
    assert [path.name for path in root.rglob("*.sqlite3")] == ["state.sqlite3"]


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


def test_download_artifact_prefers_object_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    runtime = manager.create(_job_request(tmp_path), run_in_background=False)
    object_key = (
        f"jobs/{runtime.session_id}/artifacts/report/report.json"
    )
    manager.object_store.put_bytes(object_key, b'{"sheets": 3}')
    runtime.session.artifacts["report"] = {
        "path": str(tmp_path / "missing.json"),
        "object_key": object_key,
        "digest": "object-store-digest",
    }

    response = client.get(
        f"/jobs/{runtime.session_id}/artifacts/report",
    )

    assert response.status_code == 200
    assert response.content == b'{"sheets": 3}'
    assert "missing.json" in response.headers["content-disposition"]


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
    pending_deadline = time.time() + 5
    while manager.task_queue.has_pending() and time.time() < pending_deadline:
        time.sleep(0.05)
    assert manager.task_queue.has_pending() is False
    manager._stop_event.set()


def test_background_job_leases_sqlite_queue_when_worker_id_set(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setenv("STONEBOT_TASK_STORE", "sqlite")
    monkeypatch.setenv("STONEBOT_WORKER_ID", "worker-a")
    manager = JobManager(tmp_path)

    runtime = manager.create(_job_request(tmp_path), run_in_background=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        if manager.get(runtime.session_id).session.current_stage == "audited":
            break
        time.sleep(0.05)

    tasks = manager.list_tasks(runtime.session_id)
    assert tasks[0]["status"] == "succeeded"
    assert tasks[0]["lease_owner"] is None
    assert any(event["event"] == "leased" for event in tasks[0]["audit_log"])
    assert any(event["event"] == "released" for event in tasks[0]["audit_log"])
    manager._stop_event.set()


def test_separate_worker_process_can_consume_queued_job(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setenv("STONEBOT_TASK_STORE", "sqlite")
    monkeypatch.setenv("STONEBOT_WORKER_ENABLED", "0")
    api = JobManager(tmp_path)
    runtime = api.create(_job_request(tmp_path), run_in_background=True)
    assert api.task_queue.has_pending() is True
    assert api._worker_thread is None

    monkeypatch.setenv("STONEBOT_WORKER_ENABLED", "1")
    monkeypatch.setenv("STONEBOT_WORKER_ID", "worker-b")
    worker = JobManager(tmp_path)
    worker._ensure_worker()

    deadline = time.time() + 5
    while time.time() < deadline:
        if worker.get(runtime.session_id).session.current_stage == "audited":
            break
        time.sleep(0.05)

    assert worker.get(runtime.session_id).session.current_stage == "audited"
    tasks = worker.list_tasks(runtime.session_id)
    assert tasks[0]["status"] == "succeeded"
    api._stop_event.set()
    worker._stop_event.set()


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
    assert tasks[0]["task_type"] == "start_job"
    assert tasks[0]["status"] == "succeeded"
    manager._stop_event.set()


def test_job_event_stream_generates_subscribed_and_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    runtime = manager.create(_job_request(tmp_path), run_in_background=False)

    events = manager.iter_job_events(runtime.session_id)
    assert next(events)["event"] == "subscribed"
    manager.task_events.publish(
        {
            "event": "stream_end",
            "job_id": runtime.session_id,
            "data": {"ok": True},
        }
    )
    terminal = next(events)
    assert terminal["event"] == "stream_end"
    assert terminal["data"] == {"ok": True}
    events.close()


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


def test_auth_requires_valid_tenant_key(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setattr(
        "src.http_api.load_tenant_api_keys",
        lambda: {"tenant-a": "secret-a"},
    )
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    unauthorized = client.post(
        "/jobs",
        json=_tenant_request(tmp_path, "tenant-a").model_dump(),
    )
    assert unauthorized.status_code == 401

    authorized = client.post(
        "/jobs",
        json=_tenant_request(tmp_path, "tenant-a").model_dump(),
        headers={
            "X-API-Key": "secret-a",
            "X-Tenant-ID": "tenant-a",
        },
    )
    assert authorized.status_code == 202


def test_auth_rejects_tenant_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setattr(
        "src.http_api.load_tenant_api_keys",
        lambda: {"tenant-a": "secret-a"},
    )
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    response = client.post(
        "/jobs",
        json=_tenant_request(tmp_path, "tenant-b").model_dump(),
        headers={
            "X-API-Key": "secret-a",
            "X-Tenant-ID": "tenant-a",
        },
    )

    assert response.status_code == 403


def test_auth_rejects_expired_tenant_key(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setattr(
        "src.http_api.load_tenant_api_keys",
        lambda: {
            "tenant-a": {
                "key": "secret-a",
                "expires_at": time.time() - 10,
                "roles": ["admin"],
            }
        },
    )
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    response = client.post(
        "/jobs",
        json=_tenant_request(tmp_path, "tenant-a").model_dump(),
        headers={
            "X-API-Key": "secret-a",
            "X-Tenant-ID": "tenant-a",
        },
    )

    assert response.status_code == 401


def test_auth_requires_operator_role(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    monkeypatch.setattr(
        "src.http_api.load_tenant_api_keys",
        lambda: {
            "tenant-a": {
                "key": "secret-a",
                "expires_at": None,
                "roles": ["viewer"],
            }
        },
    )
    manager = SyncJobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    client = TestClient(app)

    response = client.post(
        "/jobs",
        json=_tenant_request(tmp_path, "tenant-a").model_dump(),
        headers={
            "X-API-Key": "secret-a",
            "X-Tenant-ID": "tenant-a",
        },
    )

    assert response.status_code == 403


def test_fastapi_lifespan_shuts_down_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.agent_runtime.build_tool_handlers",
        lambda context: _ok_handlers(),
    )
    manager = JobManager(tmp_path)
    monkeypatch.setattr("src.http_api.manager", manager)
    runtime = manager.create(_job_request(tmp_path), run_in_background=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        if manager.get(runtime.session_id).session.current_stage == "audited":
            break
        time.sleep(0.05)

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(run_lifespan())

    assert manager._worker_thread is not None
    assert manager._worker_thread.is_alive() is False
