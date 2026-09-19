import asyncio
from types import SimpleNamespace

import httpx
from httpx import ASGITransport

from src.conversation_store import ConversationStore
from src.http_api import app


class SyncASGIClient:
    def __init__(self, app):
        self._transport = ASGITransport(app=app)

    def _request(self, method, path, **kwargs):
        async def run():
            async with httpx.AsyncClient(
                transport=self._transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(run())

    def get(self, path, **kwargs):
        return self._request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self._request("POST", path, **kwargs)


def TestClient(app):
    return SyncASGIClient(app)


def test_conversation_crud(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path)
    monkeypatch.setattr("src.http_api.conversation_store", store)
    client = TestClient(app)

    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    message = client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "role": "user",
            "content": "3200x1800 排板",
            "parameters": {"process": "bridge", "kerf_mm": 5},
        },
    )
    assert message.status_code == 201
    assert message.json()["content"] == "3200x1800 排板"
    assert message.json()["parameters"]["kerf_mm"] == 5

    loaded = client.get(f"/conversations/{conversation_id}")
    assert loaded.status_code == 200
    assert loaded.json()["messages"][0]["content"] == "3200x1800 排板"

    listed = client.get("/conversations")
    assert listed.status_code == 200
    assert len(listed.json()["conversations"]) == 1


class FakeConversationPlanner:
    def __init__(self):
        self.calls = []

    def plan(self, messages, *, attachments=None):
        self.calls.append((messages, attachments))
        return self.intent


class FakeCorrectionPlanner:
    def __init__(self):
        self.calls = []

    def plan(self, instruction, *, context, target_ids=None):
        self.calls.append((instruction, context, target_ids))
        return SimpleNamespace(instruction=instruction)


class FakeRuntime:
    def __init__(self, request=None, job_id="job-conversation", stage="queued"):
        self.request = request
        self.session_id = job_id
        self.session = SimpleNamespace(current_stage=stage)
        self.context = SimpleNamespace(
            parts=[],
            recognized_drawing=None,
            correction_revision=0,
        )

    def status(self):
        return {
            "job_id": self.session_id,
            "stage": self.session.current_stage,
        }


class FakeManager:
    def __init__(self):
        self.created = []

    def create(self, request, *, run_in_background=True):
        self.created.append(request)
        return FakeRuntime(request)

    def tenant_for_job(self, job_id):
        return "default"

    def review(self, job_id, max_reductions=1):
        self.reviewed = (job_id, max_reductions)
        return {
            "reviewed": True,
            "original_total_sheets": 8,
            "reduced_total_sheets": 7,
            "reductions": [{"removed_sheet_index": 8}],
        }

    def get(self, job_id):
        self.got = job_id
        return FakeRuntime(job_id=job_id, stage="nested_reported")

    def confirm(self, job_id, decision, **kwargs):
        self.confirmed = (job_id, decision)
        self.confirmed_scope = kwargs.get("feedback_scope", "project")
        return FakeRuntime(job_id=job_id, stage="postprocess_confirmed")

    def run(self, job_id):
        self.ran = job_id
        return FakeRuntime(job_id=job_id, stage="nested_reported")

    def cancel(self, job_id):
        self.cancelled = job_id
        return FakeRuntime(job_id=job_id, stage="cancelled")

    def correct(self, job_id, plan):
        self.corrected = (job_id, plan)
        return {"revision": 2, "plan_id": plan.instruction}


def _conversation_client(tmp_path, monkeypatch, planner_intent):
    store = ConversationStore(tmp_path)
    monkeypatch.setattr("src.http_api.conversation_store", store)
    monkeypatch.setattr("src.http_api.ConversationPlanner", FakeConversationPlanner)
    planner = FakeConversationPlanner()
    planner.intent = planner_intent
    monkeypatch.setattr("src.http_api.ConversationPlanner", lambda: planner)
    manager = FakeManager()
    monkeypatch.setattr("src.http_api.manager", manager)
    return TestClient(app), planner, manager


def test_respond_uses_latest_attachment_for_create_job(tmp_path, monkeypatch):
    client, planner, manager = _conversation_client(
        tmp_path,
        monkeypatch,
        {"action": "create_job", "width_mm": 3200, "height_mm": 1800},
    )
    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    conversation_id = created.json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "role": "user",
            "content": "3200x1800 排板",
            "attachments": [{"dxf_path": "C:/uploads/water.dxf"}],
            "parameters": {
                "process": "bridge",
                "kerf_mm": 5,
                "usable_width_mm": 3180,
                "usable_height_mm": 1780,
                "oversize_enabled": True,
                "oversize_mode": "total",
                "oversize_width_mm": 20,
                "oversize_height_mm": 20,
            },
        },
    )

    response = client.post(f"/conversations/{conversation_id}/respond")

    assert response.status_code == 202
    assert response.json()["result"]["job_id"] == "job-conversation"
    assert len(manager.created) == 1
    assert manager.created[0].dxf_path == "C:/uploads/water.dxf"
    assert manager.created[0].width_mm == 3200
    assert manager.created[0].height_mm == 1800
    assert manager.created[0].process == "bridge"
    assert manager.created[0].kerf_mm == 5
    assert manager.created[0].usable_width_mm == 3180
    assert manager.created[0].usable_height_mm == 1780
    assert manager.created[0].oversize_enabled is True
    assert planner.calls[0][1] == [{"dxf_path": "C:/uploads/water.dxf"}]


def test_structured_parameters_create_job_without_llm(tmp_path, monkeypatch):
    client, planner, manager = _conversation_client(
        tmp_path,
        monkeypatch,
        {"action": "create_job", "width_mm": 1, "height_mm": 1},
    )
    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    conversation_id = created.json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "role": "user",
            "content": "开始排板",
            "attachments": [{"dxf_path": "C:/uploads/structured.dxf"}],
            "parameters": {
                "width_mm": 3200,
                "height_mm": 1800,
                "thickness_mm": 20,
                "usable_width_mm": 3200,
                "usable_height_mm": 1800,
                "process": "bridge",
                "kerf_mm": 5,
            },
        },
    )

    response = client.post(f"/conversations/{conversation_id}/respond")

    assert response.status_code == 202
    assert planner.calls == []
    assert manager.created[0].dxf_path == "C:/uploads/structured.dxf"
    assert manager.created[0].process == "bridge"
    assert manager.created[0].kerf_mm == 5


def test_respond_create_job_prefers_intent_dxf_path(tmp_path, monkeypatch):
    client, _, manager = _conversation_client(
        tmp_path,
        monkeypatch,
        {
            "action": "create_job",
            "dxf_path": "C:/uploads/from-llm.dxf",
            "width_mm": 2400,
            "height_mm": 1200,
        },
    )
    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    conversation_id = created.json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "role": "user",
            "content": "2400x1200 排板",
            "attachments": [{"dxf_path": "C:/uploads/attached.dxf"}],
        },
    )

    response = client.post(f"/conversations/{conversation_id}/respond")

    assert response.status_code == 202
    assert manager.created[0].dxf_path == "C:/uploads/from-llm.dxf"


def test_respond_create_job_without_dxf_returns_400(tmp_path, monkeypatch):
    client, _, manager = _conversation_client(
        tmp_path,
        monkeypatch,
        {"action": "create_job", "width_mm": 3200, "height_mm": 1800},
    )
    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    conversation_id = created.json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={"role": "user", "content": "3200x1800 排板"},
    )

    response = client.post(f"/conversations/{conversation_id}/respond")

    assert response.status_code == 400
    assert manager.created == []


def test_respond_correct_drawing_regenerates_numbered_dxf(tmp_path, monkeypatch):
    client, _, manager = _conversation_client(
        tmp_path,
        monkeypatch,
        {
            "action": "correct_drawing",
            "job_id": "job-1",
            "instruction": "把 P-1 改成 P-1A",
        },
    )
    correction_planner = FakeCorrectionPlanner()
    monkeypatch.setattr(
        "src.http_api.CorrectionPlanner",
        lambda: correction_planner,
    )
    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    conversation_id = created.json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "role": "user",
            "content": "把 P-1 改成 P-1A",
            "parameters": {
                "target_ids": ["P-1"],
                "feedback_scope": "global",
            },
        },
    )

    response = client.post(f"/conversations/{conversation_id}/respond")

    assert response.status_code == 202
    assert response.json()["result"]["correction"]["revision"] == 2
    assert manager.corrected[0] == "job-1"
    assert correction_planner.calls[0][0] == "把 P-1 改成 P-1A"
    assert correction_planner.calls[0][2] == ["P-1"]
    assert manager.corrected[1].feedback_scope == "global"


def test_respond_review_job_adds_assistant_summary(tmp_path, monkeypatch):
    client, _, manager = _conversation_client(
        tmp_path,
        monkeypatch,
        {"action": "review_job", "job_id": "job-1", "max_reductions": 1},
    )
    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    conversation_id = created.json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={"role": "user", "content": "板数不合适，回看能不能少一张"},
    )

    response = client.post(f"/conversations/{conversation_id}/respond")

    assert response.status_code == 202
    assert response.json()["result"]["reduced_total_sheets"] == 7
    assert manager.reviewed == ("job-1", 1)
    loaded = client.get(f"/conversations/{conversation_id}")
    assistant = loaded.json()["messages"][-1]
    assert assistant["role"] == "assistant"
    assert "8" in assistant["content"]
    assert "7" in assistant["content"]


def test_respond_confirm_uses_conversation_active_job(tmp_path, monkeypatch):
    client, planner, manager = _conversation_client(
        tmp_path,
        monkeypatch,
        {"action": "create_job", "width_mm": 3200, "height_mm": 1800},
    )
    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    conversation_id = created.json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "role": "user",
            "content": "3200x1800 排板",
            "attachments": [{"dxf_path": "C:/uploads/water.dxf"}],
        },
    )
    first = client.post(f"/conversations/{conversation_id}/respond")
    assert first.status_code == 202
    assert first.json()["result"]["job_id"] == "job-conversation"

    planner.intent = {
        "action": "confirm_job",
        "decision": "confirm",
    }
    response = client.post(f"/conversations/{conversation_id}/respond")

    assert response.status_code == 202
    assert manager.confirmed == ("job-conversation", "confirm")
    assert response.json()["result"]["stage"] == "postprocess_confirmed"


def test_respond_get_job_uses_active_job_without_intent_id(tmp_path, monkeypatch):
    client, planner, manager = _conversation_client(
        tmp_path,
        monkeypatch,
        {"action": "create_job", "width_mm": 3200, "height_mm": 1800},
    )
    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    conversation_id = created.json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "role": "user",
            "content": "3200x1800 排板",
            "attachments": [{"dxf_path": "C:/uploads/water.dxf"}],
        },
    )
    assert client.post(f"/conversations/{conversation_id}/respond").status_code == 202

    planner.intent = {"action": "get_job"}
    response = client.post(f"/conversations/{conversation_id}/respond")

    assert response.status_code == 202
    assert manager.got == "job-conversation"
    assert response.json()["result"]["stage"] == "nested_reported"


def test_get_conversation_artifacts_uses_active_job(tmp_path, monkeypatch):
    client, planner, manager = _conversation_client(
        tmp_path,
        monkeypatch,
        {"action": "create_job", "width_mm": 3200, "height_mm": 1800},
    )
    created = client.post(
        "/conversations",
        json={"tenant_id": "default"},
    )
    conversation_id = created.json()["id"]
    client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "role": "user",
            "content": "3200x1800 排板",
            "attachments": [{"dxf_path": "C:/uploads/water.dxf"}],
        },
    )
    assert client.post(f"/conversations/{conversation_id}/respond").status_code == 202

    response = client.get(f"/conversations/{conversation_id}/artifacts")

    assert response.status_code == 200
    assert response.json()["job_id"] == "job-conversation"
