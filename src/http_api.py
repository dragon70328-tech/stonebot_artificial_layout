"""HTTP boundary for the agent workflow.

The MVP intentionally accepts a server-local DXF path rather than an upload.
It exposes the same AgentRuntime state machine used by the CLI: create a job,
poll its stage, and confirm at the three human gates. Long work runs in a
daemon thread so the web process stays responsive.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from src.agent_loop import AgentLoopResult
from src.agent_runtime import AgentRuntime
from src.artifact_store import content_digest
from src.constraints import PROFILE_MIN_SHEETS, PROFILES
from src.input_guard import DXFInputLimits, check_dxf_file
from src.job_repository import build_job_repository
from src.llm_client import LLMError
from src.object_store import build_object_store
from src.session_store import (
    SqliteWorkflowSessionStore,
    WorkflowSessionStore,
    build_session_store,
)
from src.state_db import StateDatabase
from src.task_queue import SqliteTaskQueue, Task, build_task_queue
from src.task_events import TaskEventBroker, format_sse
from src.tenant_store import TenantError, build_tenant_store
from src.units import UnitSystem
from src.workflow_session import WorkflowStage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


class JobCreateRequest(BaseModel):
    dxf_path: str
    tenant_id: str = "default"
    width_mm: float
    height_mm: float
    thickness_mm: float = 20.0
    unit: Literal["metric", "imperial"] = "metric"
    profile: str | None = None
    process: Literal["bridge", "waterjet", "laser"] | None = None
    provider: Literal["queue", "llm"] = "queue"
    trials: int = 1
    seed: int = 0
    budget: float = 0.0
    quick: bool = True
    pairing: bool = False
    skip_unnumbered: bool = True
    project_id: str | None = None


class ConfirmationRequest(BaseModel):
    decision: str


class JobStateError(RuntimeError):
    """Raised when an API action is invalid for the current job state."""


def _build_profile(request: JobCreateRequest):
    profile = PROFILES.get(request.profile, PROFILE_MIN_SHEETS)
    if request.process is None:
        return profile
    processing_class = {
        "bridge": "bridge",
        "waterjet": "waterjet_laser",
        "laser": "waterjet_laser",
    }[request.process]
    return profile.with_overrides(processing_class=processing_class)


def _safe_project(value: str) -> str:
    safe = _SAFE_ID.sub("_", value).strip("_")
    return safe or "unnamed"


def _safe_upload_name(filename: str) -> str:
    raw_name = Path(filename).name
    if not raw_name.lower().endswith(".dxf"):
        raise HTTPException(status_code=400, detail="only .dxf input is accepted")
    stem = raw_name[: -len(".dxf")]
    safe_stem = _SAFE_ID.sub("_", stem).strip("_") or "upload"
    return f"{safe_stem}.dxf"


def _safe_download_name(filename: str) -> str:
    raw_name = Path(filename).name
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name).strip("._")
    return safe_name or "artifact"


def load_tenant_api_keys() -> dict[str, Any]:
    raw = os.environ.get("STONEBOT_TENANT_API_KEYS")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    single_key = os.environ.get("STONEBOT_API_KEY")
    if single_key:
        return {"default": single_key}
    return {}


@dataclass
class TenantAuth:
    tenant_id: str
    roles: set[str] = field(default_factory=set)

    def has_role(self, role: str) -> bool:
        return "admin" in self.roles or role in self.roles


def _normalize_credential(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        return {
            "key": value,
            "expires_at": None,
            "roles": ["admin"],
        }
    if isinstance(value, dict) and isinstance(value.get("key"), str):
        return {
            "key": value["key"],
            "expires_at": value.get("expires_at"),
            "roles": list(value.get("roles") or []),
        }
    return None


def get_authenticated_tenant(
    x_api_key: str = Header(default=""),
    x_tenant_id: str = Header(default="default"),
) -> TenantAuth:
    keys = load_tenant_api_keys()
    tenant_id = x_tenant_id.strip() or "default"
    if not keys:
        return TenantAuth(tenant_id=tenant_id, roles={"admin"})
    credential = _normalize_credential(keys.get(tenant_id))
    if credential is None:
        raise HTTPException(status_code=401, detail="invalid tenant credentials")
    if (
        credential["expires_at"] is not None
        and time.time() > float(credential["expires_at"])
    ):
        raise HTTPException(status_code=401, detail="tenant credential expired")
    if not x_api_key or credential["key"] != x_api_key:
        raise HTTPException(status_code=401, detail="invalid tenant credentials")
    return TenantAuth(
        tenant_id=tenant_id,
        roles=set(credential["roles"]),
    )


def _ensure_tenant_match(auth: TenantAuth, target_tenant: str) -> None:
    if not load_tenant_api_keys():
        return
    if auth.tenant_id != target_tenant:
        raise HTTPException(
            status_code=403,
            detail="tenant mismatch",
        )


def _require_role(auth: TenantAuth, role: str) -> None:
    if not load_tenant_api_keys():
        return
    if not auth.has_role(role):
        raise HTTPException(
            status_code=403,
            detail=f"missing required role: {role}",
        )


class JobManager:
    """File-backed job registry that can reload AgentRuntime sessions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.object_store = build_object_store(self.root / "object_store")
        if os.environ.get("STONEBOT_STATE_DB", "").strip().lower() == "unified":
            state_db = StateDatabase(self.root / "state.sqlite3")
            conn = state_db.conn
            self._state_db = state_db
            self.tenant_store = build_tenant_store(
                self.root / "tenants",
                conn=conn,
            )
            self.job_repository = build_job_repository(self.root, conn=conn)
            self.session_store = build_session_store(
                self.root / "sessions",
                conn=conn,
            )
            self.task_queue = build_task_queue(self.root / "queue", conn=conn)
        else:
            self._state_db = None
            self.tenant_store = build_tenant_store(self.root / "tenants")
            self.job_repository = build_job_repository(self.root)
            self.session_store = build_session_store(self.root / "sessions")
            self.task_queue = build_task_queue(self.root / "queue")
        self._sqlite_sessions = isinstance(
            self.session_store,
            SqliteWorkflowSessionStore,
        )
        self.task_events = TaskEventBroker()
        self._worker_id = os.environ.get("STONEBOT_WORKER_ID")
        self._jobs: dict[str, AgentRuntime] = {}
        self._lock = threading.RLock()
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def _project_id(self, request: JobCreateRequest) -> str:
        return request.project_id or Path(request.dxf_path).stem

    def _output_dir(self, request: JobCreateRequest) -> Path:
        project_id = self._project_id(request)
        return self.tenant_store.project_dir(
            request.tenant_id,
            project_id,
        ) / "runs"

    def _session_store_for(self, request: JobCreateRequest):
        if self._sqlite_sessions:
            return self.session_store
        return WorkflowSessionStore(self._output_dir(request))

    def _make_runtime(
        self,
        request: JobCreateRequest,
        job_id: str,
        restore_session=None,
    ) -> AgentRuntime:
        unit = UnitSystem.IMPERIAL if request.unit == "imperial" else UnitSystem.METRIC
        return AgentRuntime(
            request.dxf_path,
            request.width_mm,
            request.height_mm,
            request.thickness_mm,
            unit,
            _build_profile(request),
            provider_name=request.provider,
            trials=request.trials,
            seed=request.seed,
            budget=request.budget,
            quick=request.quick,
            pairing=request.pairing,
            skip_unnumbered=request.skip_unnumbered,
            object_store=self.object_store,
            artifact_object_prefix=f"jobs/{job_id}/artifacts",
            session_store=self._session_store_for(request),
            output_dir=self._output_dir(request),
            session_id=job_id,
            restore_session=restore_session,
        )

    def _write_manifest(
        self,
        runtime: AgentRuntime,
        request: JobCreateRequest,
    ) -> None:
        self.job_repository.save(
            runtime.session_id,
            request.model_dump(),
            time.time(),
        )

    def _load_runtime(self, job_id: str) -> AgentRuntime:
        payload = self.job_repository.get(job_id)
        if payload is None:
            raise JobStateError(f"job not found: {job_id}")
        request = JobCreateRequest(**payload["request"])
        session_store = self._session_store_for(request)
        if not session_store.exists(job_id):
            raise JobStateError(f"job session missing: {job_id}")
        runtime = self._make_runtime(
            request,
            job_id,
            restore_session=session_store.load(job_id),
        )
        stage = WorkflowStage(runtime.session.current_stage)
        runtime.last_result = AgentLoopResult(
            session_id=job_id,
            stage=runtime.session.current_stage,
            completed=stage == WorkflowStage.COMPLETED,
            blocked=stage == WorkflowStage.BLOCKED,
            confirmation_required=stage in {
                WorkflowStage.AUDITED,
                WorkflowStage.NESTED_REPORTED,
                WorkflowStage.POSTPROCESS_CONFIRMED,
            },
            tool_results=[],
        )
        return runtime

    def tenant_for_job(self, job_id: str) -> str:
        payload = self.job_repository.get(job_id)
        if payload is None:
            raise JobStateError(f"job not found: {job_id}")
        return payload["request"].get("tenant_id", "default")

    def store_input(
        self,
        tenant_id: str,
        project_id: str,
        safe_name: str,
        data: bytes,
    ) -> tuple[Path, dict[str, object]]:
        """Persist an uploaded DXF in object storage and materialize a local copy."""
        self.tenant_store.create_project(tenant_id, project_id)
        object_key = (
            f"tenants/{tenant_id}/projects/{project_id}/inputs/{safe_name}"
        )
        object_ref = self.object_store.put_bytes(object_key, data)

        working_dir = self.tenant_store.project_dir(tenant_id, project_id) / "inputs"
        working_dir.mkdir(parents=True, exist_ok=True)
        digest = content_digest(data)
        path = working_dir / f"{Path(safe_name).stem}_{digest[:12]}.dxf"
        path.write_bytes(self.object_store.get_bytes(str(object_ref["key"])))
        return path, object_ref

    def get_artifact_bytes(
        self,
        job_id: str,
        artifact_name: str,
    ) -> tuple[bytes, str]:
        """Read artifact bytes from object storage, falling back to local disk."""
        runtime = self.get(job_id)
        artifact = runtime.session.artifacts.get(artifact_name)
        if not isinstance(artifact, dict):
            raise JobStateError("artifact not found")

        object_key = artifact.get("object_key")
        if object_key and self.object_store.exists(str(object_key)):
            filename = Path(str(artifact.get("path", ""))).name or (
                f"{artifact_name}.dxf"
            )
            return self.object_store.get_bytes(str(object_key)), filename

        path = Path(str(artifact.get("path", "")))
        if not path.exists() or not path.is_file():
            raise JobStateError("artifact file missing")
        return path.read_bytes(), path.name

    def _task_handlers(self) -> dict[str, Any]:
        return {
            "run_job": lambda payload: self.get(
                payload["job_id"]
            ).resume().to_dict(),
            "confirm_job": lambda payload: self.get(
                payload["job_id"]
            ).confirm(payload["decision"]).to_dict(),
        }

    def _enqueue_action(self, task_type: str, payload: dict[str, Any]) -> None:
        task = self.task_queue.enqueue(
            Task(
                task_type=task_type,
                payload=payload,
                timeout_s=3600.0,
            )
        )
        self.task_events.publish(
            {
                "event": "task_enqueued",
                "job_id": payload.get("job_id"),
                "data": task.to_dict(),
            }
        )
        self._ensure_worker()

    def _publish_task_event(self, event: str, task: Task) -> None:
        self.task_events.publish(
            {
                "event": f"task_{event}",
                "job_id": task.payload.get("job_id"),
                "data": task.to_dict(),
            }
        )
        if event in {"succeeded", "failed", "timed_out", "dead_letter"}:
            self._publish_job_event(task.payload.get("job_id"))

    def _publish_job_event(self, job_id: str | None) -> None:
        if not job_id:
            return
        try:
            status = self.get(job_id).status()
        except JobStateError:
            return
        stage = status.get("stage")
        if status.get("completed") or stage == WorkflowStage.COMPLETED.value:
            event = "job_completed"
        elif status.get("blocked") or stage == WorkflowStage.BLOCKED.value:
            event = "job_blocked"
        elif stage == WorkflowStage.CANCELLED.value:
            event = "job_cancelled"
        else:
            return
        self.task_events.publish(
            {"event": event, "job_id": job_id, "data": status}
        )

    def iter_job_events(self, job_id: str):
        """Yield task snapshots and live progress events for one job."""
        subscriber = self.task_events.subscribe()
        try:
            yield {
                "event": "subscribed",
                "job_id": job_id,
                "data": {},
            }
            for task in self.list_tasks(job_id):
                yield {
                    "event": "task_snapshot",
                    "job_id": job_id,
                    "data": {"task": task},
                }
            while True:
                try:
                    event = subscriber.get(timeout=5.0)
                except queue.Empty:
                    yield {
                        "event": "keepalive",
                        "job_id": job_id,
                        "data": {},
                    }
                    continue
                if event.get("job_id") != job_id:
                    continue
                yield event
                if event.get("event") in {
                    "stream_end",
                    "job_completed",
                    "job_cancelled",
                    "job_blocked",
                }:
                    break
        finally:
            self.task_events.unsubscribe(subscriber)

    def _ensure_worker(self) -> None:
        if (
            self._worker_thread is not None
            and self._worker_thread.is_alive()
        ):
            return

        def worker_loop() -> None:
            while not self._stop_event.wait(0.1):
                if not self.task_queue.has_pending():
                    continue
                if isinstance(self.task_queue, SqliteTaskQueue) and self._worker_id:
                    self.task_queue.run_all(
                        self._task_handlers(),
                        on_event=self._publish_task_event,
                        worker_id=self._worker_id,
                    )
                else:
                    self.task_queue.run_all(
                        self._task_handlers(),
                        on_event=self._publish_task_event,
                    )

        self._worker_thread = threading.Thread(
            target=worker_loop,
            daemon=True,
            name="job-task-worker",
        )
        self._worker_thread.start()

    def create(
        self,
        request: JobCreateRequest,
        *,
        run_in_background: bool = True,
    ) -> AgentRuntime:
        self.tenant_store.create_project(
            request.tenant_id,
            self._project_id(request),
        )
        runtime = self._make_runtime(
            request,
            f"job-{uuid.uuid4().hex}",
        )
        with self._lock:
            self._jobs[runtime.session_id] = runtime
            self._write_manifest(runtime, request)
        if run_in_background:
            self._enqueue_action(
                "run_job",
                {"job_id": runtime.session_id},
            )
        return runtime

    def get(self, job_id: str) -> AgentRuntime:
        with self._lock:
            runtime = self._jobs.get(job_id)
            if runtime is None:
                runtime = self._load_runtime(job_id)
                self._jobs[job_id] = runtime
            return runtime

    def list_jobs(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            job_ids = set(self.job_repository.list_job_ids())
            job_ids.update(self._jobs)
            if tenant_id is not None:
                job_ids = {
                    job_id
                    for job_id in job_ids
                    if self.tenant_for_job(job_id) == tenant_id
                }
            return [
                {
                    "job_id": job_id,
                    "stage": self.get(job_id).session.current_stage,
                    "completed": (
                        self.get(job_id).last_result.completed
                        if self.get(job_id).last_result
                        else False
                    ),
                    "blocked": (
                        self.get(job_id).last_result.blocked
                        if self.get(job_id).last_result
                        else False
                    ),
                }
                for job_id in sorted(job_ids)
            ]

    def list_tasks(self, job_id: str) -> list[dict[str, Any]]:
        return [
            task.to_dict()
            for task in self.task_queue.list_tasks()
            if task.payload.get("job_id") == job_id
        ]

    def retry_task(self, job_id: str, task_id: str) -> dict[str, Any]:
        task = self.task_queue.load(task_id)
        if task.payload.get("job_id") != job_id:
            raise JobStateError("task does not belong to job")
        retried = self.task_queue.retry_task(task_id)
        self._ensure_worker()
        return retried.to_dict()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        if self._state_db is not None:
            self._state_db.close()

    def confirm(
        self,
        job_id: str,
        decision: str,
        *,
        run_in_background: bool = True,
    ) -> AgentRuntime:
        runtime = self.get(job_id)
        normalized = decision.strip().lower()
        if normalized not in {"confirm", "yes", "y", "cancel", "reject", "no", "n"}:
            raise JobStateError(f"unknown confirmation decision: {decision!r}")
        if runtime.last_result is None or not runtime.last_result.confirmation_required:
            raise JobStateError("no confirmation gate is waiting")
        if run_in_background:
            self._enqueue_action(
                "confirm_job",
                {
                    "job_id": job_id,
                    "decision": decision,
                },
            )
        else:
            runtime.confirm(decision)
        return runtime

    def run(
        self,
        job_id: str,
        *,
        run_in_background: bool = True,
    ) -> AgentRuntime:
        runtime = self.get(job_id)
        stage = WorkflowStage(runtime.session.current_stage)
        if stage in {WorkflowStage.COMPLETED, WorkflowStage.CANCELLED}:
            raise JobStateError(f"job is terminal: {stage.value}")
        if stage == WorkflowStage.BLOCKED:
            raise JobStateError("blocked job must be recovered before running")
        if runtime.last_result and runtime.last_result.confirmation_required:
            raise JobStateError("job is waiting for confirmation; use /confirm")
        if run_in_background:
            self._enqueue_action(
                "run_job",
                {"job_id": job_id},
            )
        else:
            runtime.resume()
        return runtime

    def cancel(self, job_id: str) -> AgentRuntime:
        runtime = self.get(job_id)
        runtime.cancel()
        return runtime


manager = JobManager(PROJECT_ROOT / "output" / "api_jobs")


@asynccontextmanager
async def lifespan(app):
    yield
    manager.shutdown()


app = FastAPI(
    title="Stonebot Nesting Agent API",
    version="0.1.0",
    lifespan=lifespan,
)


def _require_job_tenant(job_id: str, auth: TenantAuth) -> None:
    try:
        actual_tenant = manager.tenant_for_job(job_id)
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_tenant_match(auth, actual_tenant)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/inputs/{filename}", status_code=201)
async def upload_input(
    filename: str,
    request: Request,
    tenant_id: str = "default",
    project_id: str = "uploads",
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    _require_role(tenant, "operator")
    _ensure_tenant_match(tenant, tenant_id)
    safe_name = _safe_upload_name(filename)
    data = await request.body()
    limits = DXFInputLimits()
    if len(data) > limits.max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file size exceeds {limits.max_bytes} bytes",
        )

    digest = content_digest(data)
    try:
        path, object_ref = manager.store_input(
            tenant_id,
            project_id,
            safe_name,
            data,
        )
    except TenantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    violations = check_dxf_file(path, limits)
    if violations:
        path.unlink(missing_ok=True)
        manager.object_store.delete(str(object_ref["key"]))
        raise HTTPException(status_code=400, detail="; ".join(violations))

    return JSONResponse(
        status_code=201,
        content={
            "dxf_path": str(path),
            "object_key": object_ref["key"],
            "bytes": len(data),
            "digest": digest,
        },
    )


@app.post("/jobs", status_code=202)
def create_job(
    request: JobCreateRequest,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    _require_role(tenant, "operator")
    _ensure_tenant_match(tenant, request.tenant_id)
    try:
        runtime = manager.create(request)
    except (LLMError, TenantError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=runtime.status())


@app.get("/jobs")
def list_jobs(
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> dict[str, Any]:
    return {"jobs": manager.list_jobs(tenant.tenant_id)}


@app.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> dict[str, Any]:
    _require_job_tenant(job_id, tenant)
    try:
        return manager.get(job_id).status()
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/events")
def stream_job_events(
    job_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> StreamingResponse:
    _require_job_tenant(job_id, tenant)
    try:
        manager.get(job_id)
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    def event_stream():
        for event in manager.iter_job_events(job_id):
            yield format_sse(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/jobs/{job_id}/tasks")
def list_job_tasks(
    job_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> dict[str, Any]:
    _require_job_tenant(job_id, tenant)
    try:
        manager.get(job_id)
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "job_id": job_id,
        "tasks": manager.list_tasks(job_id),
    }


@app.post("/jobs/{job_id}/tasks/{task_id}/retry", status_code=202)
def retry_job_task(
    job_id: str,
    task_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    _require_role(tenant, "operator")
    _require_job_tenant(job_id, tenant)
    try:
        task = manager.retry_task(job_id, task_id)
    except JobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=task)


@app.get("/jobs/{job_id}/artifacts/{artifact_name}")
def download_artifact(
    job_id: str,
    artifact_name: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> Response:
    _require_job_tenant(job_id, tenant)
    try:
        data, filename = manager.get_artifact_bytes(job_id, artifact_name)
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    safe_filename = _safe_download_name(filename)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
        },
    )


@app.post("/jobs/{job_id}/confirm")
def confirm_job(
    job_id: str,
    request: ConfirmationRequest,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    _require_role(tenant, "operator")
    _require_job_tenant(job_id, tenant)
    try:
        runtime = manager.confirm(job_id, request.decision)
    except JobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=runtime.status())


@app.post("/jobs/{job_id}/run", status_code=202)
def run_job(
    job_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    _require_role(tenant, "operator")
    _require_job_tenant(job_id, tenant)
    try:
        runtime = manager.run(job_id)
    except JobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=runtime.status())


@app.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> dict[str, Any]:
    _require_role(tenant, "operator")
    _require_job_tenant(job_id, tenant)
    try:
        return manager.cancel(job_id).status()
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
