"""HTTP boundary for the agent workflow.

The MVP intentionally accepts a server-local DXF path rather than an upload.
It exposes the same AgentRuntime state machine used by the CLI: create a job,
poll its stage, and confirm at the three human gates. Long work runs in a
daemon thread so the web process stays responsive.
"""

from __future__ import annotations

import json
import logging
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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from src.agent_loop import AgentLoopResult
from src.agent_runtime import AgentRuntime
from src.artifact_store import content_digest
from src.constraints import PROFILE_MIN_SHEETS, PROFILES
from src.correction_contracts import CorrectionPlan
from src.correction_planner import CorrectionPlanner
from src.conversation_planner import ConversationPlanner
from src.conversation_store import ConversationStore
from src.drawing_corrections import build_correction_context
from src.drawing_preview import build_drawing_preview
from src.dxf_writer import write_nested_dxf
from src.input_guard import DXFInputLimits, check_dxf_file
from src.job_repository import build_job_repository
from src.llm_client import LLMError
from src.recognition_contracts import FeedbackScope
from src.nesting_contracts import (
    OversizeMode,
    ProcessPolicy,
    SheetSpec,
)
from src.nesting_reviewer import find_reductions, load_nested_dxf
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
PORTAL_NESTING_DIR = PROJECT_ROOT / "portal" / "nesting"
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")
JOB_PARAMETER_KEYS = {
    "width_mm",
    "height_mm",
    "thickness_mm",
    "usable_width_mm",
    "usable_height_mm",
    "oversize_enabled",
    "oversize_mode",
    "oversize_width_mm",
    "oversize_height_mm",
    "kerf_mm",
    "rotation_degrees",
    "allow_shared_edge",
    "process",
    "profile",
    "pairing",
    "skip_unnumbered",
}

logger = logging.getLogger("stonebot.api")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=os.environ.get("STONEBOT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class JobCreateRequest(BaseModel):
    dxf_path: str
    tenant_id: str = "default"

    # 兼容旧接口：width/height 表示标称大板尺寸。
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    thickness_mm: float = Field(default=20.0, gt=0)

    # 新排板契约字段；未提供时可利用尺寸默认等于标称尺寸。
    usable_width_mm: float | None = Field(default=None, gt=0)
    usable_height_mm: float | None = Field(default=None, gt=0)
    oversize_enabled: bool = False
    oversize_mode: OversizeMode = OversizeMode.TOTAL
    oversize_width_mm: float = Field(default=0.0, ge=0)
    oversize_height_mm: float = Field(default=0.0, ge=0)
    kerf_mm: float = Field(default=0.0, ge=0)
    rotation_degrees: list[float] | None = None
    allow_shared_edge: bool = True

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

    @model_validator(mode="after")
    def validate_process_parameters(self) -> "JobCreateRequest":
        if self.process == "bridge" and self.kerf_mm <= 0:
            raise ValueError("bridge process requires kerf_mm > 0")
        return self


class ConfirmationRequest(BaseModel):
    decision: str
    feedback_scope: FeedbackScope = FeedbackScope.PROJECT


class ReviewRequest(BaseModel):
    max_reductions: int = 1


class CorrectionRequest(BaseModel):
    instruction: str = Field(min_length=1)
    target_ids: list[str] = Field(default_factory=list)
    operations: list[dict[str, Any]] | None = None
    feedback_scope: FeedbackScope = FeedbackScope.PROJECT


class ConversationCreateRequest(BaseModel):
    tenant_id: str = "default"


class ConversationMessageRequest(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str
    attachments: list[dict[str, Any]] | None = None
    parameters: dict[str, Any] | None = None


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


def _build_sheet_spec(request: JobCreateRequest) -> SheetSpec:
    return SheetSpec(
        nominal_width_mm=request.width_mm,
        nominal_height_mm=request.height_mm,
        usable_width_mm=request.usable_width_mm or request.width_mm,
        usable_height_mm=request.usable_height_mm or request.height_mm,
        thickness_mm=request.thickness_mm,
        oversize_enabled=request.oversize_enabled,
        oversize_mode=request.oversize_mode,
        oversize_width_mm=request.oversize_width_mm,
        oversize_height_mm=request.oversize_height_mm,
    )


def _build_process_policy(request: JobCreateRequest, profile) -> ProcessPolicy | None:
    if request.process is None:
        return None
    if request.process == "bridge":
        return ProcessPolicy.bridge(
            kerf_mm=request.kerf_mm,
            min_gap_mm=profile.min_gap,
            rotation_degrees=request.rotation_degrees,
            allow_shared_edge=request.allow_shared_edge,
        )
    if request.process == "waterjet":
        return ProcessPolicy.waterjet(
            min_gap_mm=profile.min_gap,
            kerf_mm=request.kerf_mm,
            allow_shared_edge=request.allow_shared_edge,
        )
    return ProcessPolicy.laser(
        min_gap_mm=profile.min_gap,
        kerf_mm=request.kerf_mm,
        allow_shared_edge=request.allow_shared_edge,
    )


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


def _latest_attachment_path(conversation) -> str | None:
    """Return the newest local DXF path attached to a conversation."""
    for message in reversed(conversation.messages):
        for attachment in reversed(message.attachments or []):
            if not isinstance(attachment, dict):
                continue
            for key in ("dxf_path", "local_path", "path", "file_path"):
                value = attachment.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return None


def _resolve_conversation_job(conversation, intent: dict[str, Any]) -> str:
    job_id = intent.get("job_id") or conversation.job_id
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")
    return str(job_id)


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
        profile = _build_profile(request)
        return AgentRuntime(
            request.dxf_path,
            request.width_mm,
            request.height_mm,
            request.thickness_mm,
            unit,
            profile,
            provider_name=request.provider,
            sheet_spec=_build_sheet_spec(request),
            process_policy=_build_process_policy(request, profile),
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
            tenant_id=request.tenant_id,
            project_id=self._project_id(request),
            recognition_memory_path=(
                self.tenant_store.project_dir(
                    request.tenant_id,
                    self._project_id(request),
                )
                / "recognition_memory.json"
            ),
            recognition_model_path=(
                self.root / "recognition_models" / "current.json"
            ),
            global_feedback_dir=(
                self.root / "recognition_models" / "global_feedback"
            ),
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
            "start_job": lambda payload: self.get(
                payload["job_id"]
            ).start().to_dict(),
            "run_job": lambda payload: self.get(
                payload["job_id"]
            ).resume().to_dict(),
            "confirm_job": lambda payload: self.get(
                payload["job_id"]
            ).confirm(
                payload["decision"],
                feedback_scope=FeedbackScope(
                    payload.get("feedback_scope", "project")
                ),
            ).to_dict(),
            "review_job": lambda payload: self.review(
                payload["job_id"],
                int(payload.get("max_reductions", 1)),
            ),
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
            os.environ.get("STONEBOT_WORKER_ENABLED", "1")
            .strip()
            .lower()
            in {"0", "false", "no"}
        ):
            return
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
            runtime.store.save(runtime.session)
        if run_in_background:
            self._enqueue_action(
                "start_job",
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
        feedback_scope: FeedbackScope = FeedbackScope.PROJECT,
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
                    "feedback_scope": feedback_scope.value,
                },
            )
        else:
            runtime.confirm(decision, feedback_scope=feedback_scope)
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

    def correct(self, job_id: str, plan: CorrectionPlan) -> dict[str, Any]:
        runtime = self.get(job_id)
        return runtime.correct(plan)

    def review(
        self,
        job_id: str,
        max_reductions: int = 1,
    ) -> dict[str, Any]:
        """Run the post-nesting local reviewer against an existing nested DXF."""
        runtime = self.get(job_id)
        nested_path = Path(
            runtime.context.nested_dxf_path
            or runtime.nested_path
        )
        if not nested_path.exists():
            raise JobStateError("nested dxf artifact not found")

        result = load_nested_dxf(
            nested_path,
            thickness=runtime.context.thickness_mm,
        )
        profile = runtime.context.nesting_profile
        candidates, reduced = find_reductions(
            result,
            profile=profile,
            max_reductions=max_reductions,
        )
        if not candidates:
            return {
                "reviewed": False,
                "original_total_sheets": result.total_sheets,
                "reduced_total_sheets": result.total_sheets,
                "reductions": [],
                "artifacts": {},
            }

        stem = nested_path.stem
        out_dxf = runtime.output_dir / f"{stem}_reviewed.dxf"
        out_report = runtime.output_dir / f"{stem}_reviewed.json"
        write_nested_dxf(reduced, str(out_dxf), unit_system=result.unit)
        report_payload = {
            "original_total_sheets": result.total_sheets,
            "reduced_total_sheets": reduced.total_sheets,
            "reductions": [
                {
                    "removed_sheet_index": candidate.removed_sheet_index,
                    "moved_parts": candidate.moved_parts,
                    "target_sheets": candidate.target_sheets,
                }
                for candidate in candidates
            ],
        }
        out_report.write_text(
            json.dumps(report_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        project_id = runtime.context.project_id or runtime.dxf_path.stem
        artifacts = {}
        for key, path in (
            ("reviewed_dxf", out_dxf),
            ("review_report", out_report),
        ):
            ref = runtime.artifact_store.track(
                project_id,
                runtime.session.current_stage,
                path,
            )
            artifact = ref.to_dict()
            if runtime.object_store is not None:
                object_key = (
                    f"{runtime.artifact_object_prefix}/{key}/{path.name}"
                )
                object_ref = runtime.object_store.put_bytes(
                    object_key,
                    path.read_bytes(),
                )
                artifact["object_key"] = object_ref["key"]
            runtime.session.artifacts[key] = artifact
            artifacts[key] = artifact
        runtime.store.save(runtime.session)
        return {
            "reviewed": True,
            "original_total_sheets": result.total_sheets,
            "reduced_total_sheets": reduced.total_sheets,
            "reductions": report_payload["reductions"],
            "artifacts": artifacts,
        }


manager = JobManager(
    Path(
        os.environ.get(
            "STONEBOT_DATA_ROOT",
            str(PROJECT_ROOT / "output" / "api_jobs"),
        )
    )
)
conversation_store = ConversationStore(manager.root / "conversations")


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
    return {
        "status": "ok",
        "service": "stonebot-nesting-api",
        "data_root": str(manager.root),
    }


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %s %.1fms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.post("/conversations", status_code=201)
def create_conversation(
    request: ConversationCreateRequest,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    _ensure_tenant_match(tenant, request.tenant_id)
    conversation = conversation_store.create(request.tenant_id)
    return JSONResponse(
        status_code=201,
        content=conversation.to_dict(),
    )


@app.get("/conversations")
def list_conversations(
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> dict[str, Any]:
    return {
        "conversations": [
            conversation.to_dict()
            for conversation in conversation_store.list(tenant.tenant_id)
        ]
    }


@app.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> dict[str, Any]:
    conversation = conversation_store.get(tenant.tenant_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation.to_dict()


@app.get("/conversations/{conversation_id}/artifacts")
def get_conversation_artifacts(
    conversation_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> dict[str, Any]:
    conversation = conversation_store.get(tenant.tenant_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    if not conversation.job_id:
        return {"job_id": None, "artifacts": {}}
    _require_job_tenant(conversation.job_id, tenant)
    try:
        status = manager.get(conversation.job_id).status()
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "job_id": conversation.job_id,
        "artifacts": status.get("artifacts") or {},
        "session_artifacts": status.get("session_artifacts") or {},
    }


@app.post("/conversations/{conversation_id}/messages", status_code=201)
def add_conversation_message(
    conversation_id: str,
    request: ConversationMessageRequest,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    message = conversation_store.add_message(
        tenant.tenant_id,
        conversation_id,
        role=request.role,
        content=request.content,
        attachments=request.attachments,
        parameters=request.parameters,
    )
    if message is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return JSONResponse(status_code=201, content=message.__dict__)


@app.post("/conversations/{conversation_id}/respond", status_code=202)
def respond_to_conversation(
    conversation_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    conversation = conversation_store.get(tenant.tenant_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    messages = [
        {
            "role": message.role,
            "content": message.content,
            "attachments": message.attachments,
            "parameters": message.parameters,
        }
        for message in conversation.messages
    ]
    attachments = [
        attachment
        for message in conversation.messages
        for attachment in message.attachments
    ]
    latest_parameters: dict[str, Any] = {}
    for message in reversed(conversation.messages):
        if message.role == "user" and message.parameters:
            latest_parameters = dict(message.parameters)
            break

    structured_create_ready = (
        not conversation.job_id
        and latest_parameters.get("width_mm") is not None
        and latest_parameters.get("height_mm") is not None
    )
    if structured_create_ready:
        intent = {
            "action": "create_job",
            **latest_parameters,
        }
    else:
        try:
            intent = ConversationPlanner().plan(
                messages,
                attachments=attachments,
            )
        except LLMError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if intent.get("action") == "create_job":
        intent.update(
            {
                key: value
                for key, value in latest_parameters.items()
                if key in JOB_PARAMETER_KEYS
            }
        )
    if intent.get("action") == "correct_drawing":
        if latest_parameters.get("target_ids") is not None:
            intent["target_ids"] = [
                str(item)
                for item in latest_parameters.get("target_ids", [])
            ]
        if latest_parameters.get("feedback_scope") is not None:
            intent["feedback_scope"] = str(
                latest_parameters.get("feedback_scope")
            )
    elif (
        intent.get("action") == "confirm_job"
        and latest_parameters.get("feedback_scope") is not None
    ):
        intent["feedback_scope"] = str(
            latest_parameters.get("feedback_scope")
        )

    action = intent["action"]
    try:
        if action == "create_job":
            dxf_path = intent.get("dxf_path") or _latest_attachment_path(
                conversation
            )
            if not dxf_path:
                raise HTTPException(status_code=400, detail="dxf_path is required")
            request = JobCreateRequest(
                dxf_path=str(dxf_path),
                tenant_id=tenant.tenant_id,
                width_mm=float(intent["width_mm"]),
                height_mm=float(intent["height_mm"]),
                thickness_mm=float(intent.get("thickness_mm", 20.0)),
                usable_width_mm=(
                    float(intent["usable_width_mm"])
                    if intent.get("usable_width_mm") is not None
                    else None
                ),
                usable_height_mm=(
                    float(intent["usable_height_mm"])
                    if intent.get("usable_height_mm") is not None
                    else None
                ),
                oversize_enabled=bool(intent.get("oversize_enabled", False)),
                oversize_mode=intent.get("oversize_mode", "total"),
                oversize_width_mm=float(intent.get("oversize_width_mm", 0.0)),
                oversize_height_mm=float(intent.get("oversize_height_mm", 0.0)),
                kerf_mm=float(intent.get("kerf_mm", 0.0)),
                rotation_degrees=intent.get("rotation_degrees"),
                allow_shared_edge=bool(intent.get("allow_shared_edge", True)),
                unit=intent.get("unit", "metric"),
                profile=intent.get("profile", "min_sheets"),
                process=intent.get("process"),
                provider=intent.get("provider", "queue"),
                trials=int(intent.get("trials", 1)),
                seed=int(intent.get("seed", 0)),
                budget=float(intent.get("budget", 0.0)),
                quick=bool(intent.get("quick", True)),
                pairing=bool(intent.get("pairing", False)),
                skip_unnumbered=bool(intent.get("skip_unnumbered", True)),
            )
            runtime = manager.create(request, run_in_background=True)
            conversation_store.set_job_id(
                tenant.tenant_id,
                conversation_id,
                runtime.session_id,
            )
            assistant_content = (
                f"已创建识图排板任务 {runtime.session_id}，"
                f"当前阶段：{runtime.session.current_stage}"
            )
            result_payload = runtime.status()
        elif action == "review_job":
            job_id = _resolve_conversation_job(conversation, intent)
            result_payload = manager.review(
                job_id,
                max_reductions=int(intent.get("max_reductions", 1)),
            )
            assistant_content = (
                f"回看完成：{result_payload.get('original_total_sheets')} -> "
                f"{result_payload.get('reduced_total_sheets')} 张板"
            )
        elif action == "get_job":
            job_id = _resolve_conversation_job(conversation, intent)
            result_payload = manager.get(job_id).status()
            assistant_content = f"当前任务状态：{result_payload.get('stage')}"
        elif action == "confirm_job":
            job_id = _resolve_conversation_job(conversation, intent)
            runtime = manager.confirm(
                job_id,
                str(intent.get("decision", "")),
                feedback_scope=FeedbackScope(
                    str(intent.get("feedback_scope", "project"))
                ),
            )
            result_payload = runtime.status()
            assistant_content = (
                f"已确认任务 {job_id}，当前阶段："
                f"{result_payload.get('stage')}"
            )
        elif action == "run_job":
            job_id = _resolve_conversation_job(conversation, intent)
            runtime = manager.run(job_id)
            result_payload = runtime.status()
            assistant_content = (
                f"已继续任务 {job_id}，当前阶段："
                f"{result_payload.get('stage')}"
            )
        elif action == "correct_drawing":
            job_id = _resolve_conversation_job(conversation, intent)
            runtime = manager.get(job_id)
            plan = CorrectionPlanner().plan(
                str(intent["instruction"]),
                context=build_correction_context(runtime.context),
                target_ids=[
                    str(item)
                    for item in intent.get("target_ids", [])
                ],
            )
            feedback_scope = FeedbackScope(
                str(intent.get("feedback_scope", "project"))
            )
            if hasattr(plan, "model_copy"):
                plan = plan.model_copy(update={"feedback_scope": feedback_scope})
            else:
                plan.feedback_scope = feedback_scope
            correction = manager.correct(job_id, plan)
            result_payload = runtime.status()
            result_payload["correction"] = correction
            assistant_content = (
                f"已重新生成原位DXF第 {correction.get('revision')} 版，"
                "请继续检查或确认。"
            )
        elif action == "cancel_job":
            job_id = _resolve_conversation_job(conversation, intent)
            runtime = manager.cancel(job_id)
            result_payload = runtime.status()
            assistant_content = f"已取消任务 {job_id}"
        else:
            raise HTTPException(status_code=400, detail="unsupported action")
    except (JobStateError, LLMError, TenantError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    conversation_store.add_message(
        tenant.tenant_id,
        conversation_id,
        role="assistant",
        content=assistant_content,
    )
    return JSONResponse(
        status_code=202,
        content={
            "conversation_id": conversation_id,
            "intent": intent,
            "result": result_payload,
        },
    )


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


@app.get("/jobs/{job_id}/drawing-preview")
def get_drawing_preview(
    job_id: str,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> dict[str, Any]:
    _require_role(tenant, "operator")
    _require_job_tenant(job_id, tenant)
    try:
        runtime = manager.get(job_id)
        return build_drawing_preview(runtime.context)
    except (JobStateError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/corrections")
def correct_job(
    job_id: str,
    request: CorrectionRequest,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> dict[str, Any]:
    _require_role(tenant, "operator")
    _require_job_tenant(job_id, tenant)
    runtime = manager.get(job_id)
    try:
        if request.operations:
            plan = CorrectionPlan(
                instruction=request.instruction,
                operations=request.operations,
                feedback_scope=request.feedback_scope,
            )
        else:
            plan = CorrectionPlanner().plan(
                request.instruction,
                context=build_correction_context(runtime.context),
                target_ids=request.target_ids,
            )
            plan = plan.model_copy(
                update={"feedback_scope": request.feedback_scope}
            )
        correction = manager.correct(job_id, plan)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (JobStateError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = runtime.status()
    payload["correction"] = correction
    return payload


@app.post("/jobs/{job_id}/confirm")
def confirm_job(
    job_id: str,
    request: ConfirmationRequest,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    _require_role(tenant, "operator")
    _require_job_tenant(job_id, tenant)
    try:
        runtime = manager.confirm(
            job_id,
            request.decision,
            feedback_scope=request.feedback_scope,
        )
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


@app.post("/jobs/{job_id}/review", status_code=202)
def review_job(
    job_id: str,
    request: ReviewRequest,
    tenant: TenantAuth = Depends(get_authenticated_tenant),
) -> JSONResponse:
    _require_role(tenant, "operator")
    _require_job_tenant(job_id, tenant)
    try:
        manager.get(job_id)
    except JobStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    manager._enqueue_action(
        "review_job",
        {
            "job_id": job_id,
            "max_reductions": request.max_reductions,
        },
    )
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "review_queued",
        },
    )


if PORTAL_NESTING_DIR.exists():
    app.mount("/", StaticFiles(directory=PORTAL_NESTING_DIR, html=True), name="portal-nesting")
