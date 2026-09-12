"""HTTP boundary for the agent workflow.

The MVP intentionally accepts a server-local DXF path rather than an upload.
It exposes the same AgentRuntime state machine used by the CLI: create a job,
poll its stage, and confirm at the three human gates. Long work runs in a
daemon thread so the web process stays responsive.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.agent_loop import AgentLoopResult
from src.agent_runtime import AgentRuntime
from src.artifact_store import content_digest
from src.constraints import PROFILE_MIN_SHEETS, PROFILES
from src.input_guard import DXFInputLimits, check_dxf_file
from src.llm_client import LLMError
from src.session_store import WorkflowSessionStore
from src.task_queue import FileTaskQueue, Task
from src.tenant_store import TenantError, TenantStore
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


class JobManager:
    """File-backed job registry that can reload AgentRuntime sessions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.tenant_store = TenantStore(self.root / "tenants")
        self.jobs_root = self.root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.task_queue = FileTaskQueue(self.root / "queue")
        self._jobs: dict[str, AgentRuntime] = {}
        self._lock = threading.RLock()
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def _manifest_path(self, job_id: str) -> Path:
        safe_id = _SAFE_ID.sub("_", job_id).strip("_")
        return self.jobs_root / f"{safe_id}.json"

    def _project_id(self, request: JobCreateRequest) -> str:
        return request.project_id or Path(request.dxf_path).stem

    def _output_dir(self, request: JobCreateRequest) -> Path:
        project_id = self._project_id(request)
        return self.tenant_store.project_dir(
            request.tenant_id,
            project_id,
        ) / "runs"

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
            output_dir=self._output_dir(request),
            session_id=job_id,
            restore_session=restore_session,
        )

    def _write_manifest(
        self,
        runtime: AgentRuntime,
        request: JobCreateRequest,
    ) -> None:
        payload = {
            "job_id": runtime.session_id,
            "request": request.model_dump(),
            "created_at": time.time(),
        }
        self._manifest_path(runtime.session_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_runtime(self, job_id: str) -> AgentRuntime:
        manifest_path = self._manifest_path(job_id)
        if not manifest_path.exists():
            raise JobStateError(f"job not found: {job_id}")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        request = JobCreateRequest(**payload["request"])
        session_store = WorkflowSessionStore(self._output_dir(request))
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
        self.task_queue.enqueue(
            Task(
                task_type=task_type,
                payload=payload,
                timeout_s=3600.0,
            )
        )
        self._ensure_worker()

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
                self.task_queue.run_all(self._task_handlers())

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

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            job_ids = {path.stem for path in self.jobs_root.glob("*.json")}
            job_ids.update(self._jobs)
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


app = FastAPI(title="Stonebot Nesting Agent API", version="0.1.0")
manager = JobManager(PROJECT_ROOT / "output" / "api_jobs")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/inputs/{filename}", status_code=201)
async def upload_input(
    filename: str,
    request: Request,
    tenant_id: str = "default",
    project_id: str = "uploads",
) -> JSONResponse:
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
        manager.tenant_store.create_project(tenant_id, project_id)
    except TenantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    upload_root = manager.tenant_store.project_dir(tenant_id, project_id) / "inputs"
    upload_root.mkdir(parents=True, exist_ok=True)
    path = upload_root / f"{Path(safe_name).stem}_{digest[:12]}.dxf"
    path.write_bytes(data)

    violations = check_dxf_file(path, limits)
    if violations:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="; ".join(violations))

    return JSONResponse(
        status_code=201,
        content={
            "dxf_path": str(path),
            "bytes": len(data),
            "digest": digest,
        },
    )


@app.post("/jobs", status_code=202)
def create_job(request: JobCreateRequest) -> JSONResponse:
    try:
        runtime = manager.create(request)
    except (LLMError, TenantError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=runtime.status())


@app.get("/jobs")
def list_jobs() -> dict[str, Any]:
    return {"jobs": manager.list_jobs()}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.get(job_id).status()
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/artifacts/{artifact_name}")
def download_artifact(job_id: str, artifact_name: str) -> FileResponse:
    try:
        runtime = manager.get(job_id)
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    artifact = runtime.session.artifacts.get(artifact_name)
    if not isinstance(artifact, dict):
        raise HTTPException(status_code=404, detail="artifact not found")
    path = Path(artifact.get("path", ""))
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file missing")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
    )


@app.post("/jobs/{job_id}/confirm")
def confirm_job(job_id: str, request: ConfirmationRequest) -> JSONResponse:
    try:
        runtime = manager.confirm(job_id, request.decision)
    except JobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=runtime.status())


@app.post("/jobs/{job_id}/run", status_code=202)
def run_job(job_id: str) -> JSONResponse:
    try:
        runtime = manager.run(job_id)
    except JobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=runtime.status())


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.cancel(job_id).status()
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
