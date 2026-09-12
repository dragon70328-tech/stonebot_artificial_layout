"""HTTP boundary for the agent workflow.

The MVP intentionally accepts a server-local DXF path rather than an upload.
It exposes the same AgentRuntime state machine used by the CLI: create a job,
poll its stage, and confirm at the three human gates. Long work runs in a
daemon thread so the web process stays responsive.
"""

from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.agent_runtime import AgentRuntime
from src.constraints import PROFILE_MIN_SHEETS, PROFILES
from src.llm_client import LLMError
from src.units import UnitSystem


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


class JobCreateRequest(BaseModel):
    dxf_path: str
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


class JobManager:
    """In-memory job registry backed by AgentRuntime session files."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, AgentRuntime] = {}
        self._lock = threading.RLock()

    def _spawn(self, runtime: AgentRuntime, action) -> None:
        def target() -> None:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - HTTP thread boundary
                runtime.last_error = str(exc)
                try:
                    runtime.session.block(f"runtime error: {exc}")
                except ValueError:
                    pass
                runtime.store.save(runtime.session)

        threading.Thread(
            target=target,
            daemon=True,
            name=f"agent-{runtime.session_id}",
        ).start()

    def create(
        self,
        request: JobCreateRequest,
        *,
        run_in_background: bool = True,
    ) -> AgentRuntime:
        unit = UnitSystem.IMPERIAL if request.unit == "imperial" else UnitSystem.METRIC
        project_id = request.project_id or Path(request.dxf_path).stem
        runtime = AgentRuntime(
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
            output_dir=self.root / _safe_project(project_id),
            session_id=f"job-{uuid.uuid4().hex}",
        )
        with self._lock:
            self._jobs[runtime.session_id] = runtime
        if run_in_background:
            self._spawn(runtime, runtime.run_next)
        return runtime

    def get(self, job_id: str) -> AgentRuntime:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise JobStateError(f"job not found: {job_id}") from exc

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "job_id": runtime.session_id,
                    "stage": runtime.session.current_stage,
                    "completed": (
                        runtime.last_result.completed
                        if runtime.last_result
                        else False
                    ),
                    "blocked": (
                        runtime.last_result.blocked
                        if runtime.last_result
                        else False
                    ),
                }
                for runtime in self._jobs.values()
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
            self._spawn(runtime, lambda: runtime.confirm(decision))
        else:
            runtime.confirm(decision)
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


@app.post("/jobs", status_code=202)
def create_job(request: JobCreateRequest) -> JSONResponse:
    try:
        runtime = manager.create(request)
    except (LLMError, ValueError) as exc:
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


@app.post("/jobs/{job_id}/confirm")
def confirm_job(job_id: str, request: ConfirmationRequest) -> JSONResponse:
    try:
        runtime = manager.confirm(job_id, request.decision)
    except JobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=runtime.status())


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.cancel(job_id).status()
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
