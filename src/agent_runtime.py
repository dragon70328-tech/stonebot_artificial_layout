"""Non-interactive runtime for one agent workflow job.

This wraps the deterministic tool chain and AgentLoop so both the local CLI
and an HTTP service can drive the same state machine. It never blocks on
``input()``; callers inspect ``status()`` and call ``confirm()`` at gates.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agent_cli import (
    build_agent_plan_provider,
    build_initial_plan,
    plan_after_confirmation,
)
from src.agent_loop import AgentLoop, AgentLoopResult
from src.ai_planning import ToolCall
from src.artifact_store import ArtifactStore
from src.job_context import JobContext
from src.object_store import ObjectStore
from src.session_store import SqliteWorkflowSessionStore, WorkflowSessionStore
from src.tool_handlers import build_tool_handlers
from src.units import UnitSystem
from src.worker import Worker
from src.workflow_session import FORWARD_FLOW, WorkflowSession, WorkflowStage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AgentRuntime:
    def __init__(
        self,
        dxf_path: str,
        width_mm: float,
        height_mm: float,
        thickness_mm: float,
        unit: UnitSystem,
        profile,
        *,
        provider_name: str = "queue",
        trials: int,
        seed: int,
        budget: float,
        quick: bool,
        pairing: bool,
        skip_unnumbered: bool,
        object_store: ObjectStore | None = None,
        artifact_object_prefix: str | None = None,
        session_store: WorkflowSessionStore | SqliteWorkflowSessionStore | None = None,
        output_dir: Path | None = None,
        session_id: str | None = None,
        restore_session: WorkflowSession | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.dxf_path = Path(dxf_path)
        self.output_dir = output_dir or self._make_output_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        stem = self.dxf_path.stem
        self.numbered_path = str(self.output_dir / f"{stem}_numbered_原位.dxf")
        suffix = f"{int(width_mm)}x{int(height_mm)}"
        self.nested_path = str(self.output_dir / f"{stem}_nested_{suffix}.dxf")
        self.report_path = str(self.output_dir / f"{stem}_report_{suffix}.json")

        self.context = JobContext(
            dxf_path=str(self.dxf_path),
            unit=unit,
            nesting_profile=profile,
            width_mm=width_mm,
            height_mm=height_mm,
            thickness_mm=thickness_mm,
            trials=trials,
            seed=seed,
            budget=budget,
            quick=quick,
            pairing=pairing,
            skip_unnumbered=skip_unnumbered,
            output_dir=self.output_dir,
        )

        self.session_id = (
            session_id
            or f"agent-{stem}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        )
        self.session = restore_session or WorkflowSession(
            session_id=self.session_id
        )
        self.object_store = object_store
        self.artifact_object_prefix = (
            artifact_object_prefix or f"jobs/{self.session_id}/artifacts"
        ).rstrip("/")
        self.store = session_store or WorkflowSessionStore(self.output_dir)
        self.artifact_store = ArtifactStore(self.output_dir / "artifacts")
        self.provider = build_agent_plan_provider(provider_name)
        tool_timeout_s = float(
            os.environ.get("STONEBOT_TOOL_TIMEOUT_S", "1800")
        )
        self.worker = Worker(
            build_tool_handlers(self.context),
            default_timeout_s=tool_timeout_s,
        )
        self.loop = AgentLoop(
            self.session,
            self.worker,
            self.provider,
            store=self.store,
        )
        if provider_name == "queue" and restore_session is None:
            self.provider.enqueue(
                build_initial_plan(
                    str(self.dxf_path),
                    self.numbered_path,
                    skip_unnumbered=skip_unnumbered,
                )
            )
        self.last_result: AgentLoopResult | None = None
        self.last_error: str | None = None

    def _make_output_dir(self) -> Path:
        stem = self.dxf_path.stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return PROJECT_ROOT / "output" / f"{timestamp}_{stem}"

    def run_next(self) -> AgentLoopResult:
        try:
            self.last_result = self.loop.run_next(self.context)
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - service boundary
            self.last_error = str(exc)
            self.session.block(f"runtime error: {exc}")
            self.last_result = AgentLoopResult(
                session_id=self.session.session_id,
                stage=self.session.current_stage,
                completed=False,
                blocked=True,
                confirmation_required=False,
                tool_results=[],
            )
        self._record_artifacts()
        self.store.save(self.session)
        return self.last_result

    def start(self) -> AgentLoopResult:
        """Start a newly created job without duplicating a queued plan."""
        if self.provider_name == "queue" and not getattr(
            self.provider,
            "has_pending_plans",
            False,
        ):
            self.provider.enqueue(
                build_initial_plan(
                    str(self.dxf_path),
                    self.numbered_path,
                    skip_unnumbered=self.context.skip_unnumbered,
                )
            )
        return self.run_next()

    def confirm(self, decision: str) -> AgentLoopResult:
        if self.last_result is None or not self.last_result.confirmation_required:
            raise RuntimeError("no confirmation gate is waiting")

        normalized = decision.strip().lower()
        if normalized in {"cancel", "reject", "no", "n"}:
            self.session.cancel("用户拒绝确认")
            self._record_artifacts()
            self.store.save(self.session)
            self.last_result = AgentLoopResult(
                session_id=self.session.session_id,
                stage=self.session.current_stage,
                completed=False,
                blocked=False,
                confirmation_required=False,
                tool_results=[],
            )
            return self.last_result

        if normalized not in {"confirm", "yes", "y"}:
            raise ValueError(f"unknown confirmation decision: {decision!r}")

        if self.provider_name == "queue":
            self.provider.enqueue(
                plan_after_confirmation(
                    self.last_result.stage,
                    width_mm=self.context.width_mm,
                    height_mm=self.context.height_mm,
                    thickness_mm=self.context.thickness_mm,
                    nested_path=self.nested_path,
                    report_path=self.report_path,
                    trials=self.context.trials,
                    seed=self.context.seed,
                    budget=self.context.budget,
                    quick=self.context.quick,
                    pairing=self.context.pairing,
                )
            )
        return self.run_next()

    def resume(self) -> AgentLoopResult:
        """Continue a recovered non-gate session from its current stage."""
        if self.provider_name == "queue":
            plan = self._plan_for_current_stage()
            if plan:
                self.provider.enqueue(plan)
        return self.run_next()

    def _plan_for_current_stage(self) -> list[ToolCall]:
        stage = self.session.stage
        if stage == WorkflowStage.NUMBERING_CONFIRMED:
            return [
                ToolCall(
                    "nest_parts",
                    {
                        "sheet_width": self.context.width_mm,
                        "sheet_height": self.context.height_mm,
                        "thickness": self.context.thickness_mm,
                        "trials": self.context.trials,
                        "seed": self.context.seed,
                        "budget": self.context.budget,
                        "quick": self.context.quick,
                        "pairing": self.context.pairing,
                    },
                )
            ]

        tool_stages = {
            "analyze_drawing": WorkflowStage.UPLOADED,
            "match_profile": WorkflowStage.ANALYZED,
            "read_dxf": WorkflowStage.PROFILE_MATCHED,
            "audit_drawing": WorkflowStage.READ,
            "assign_numbers": WorkflowStage.AUDITED,
            "write_numbered_dxf": WorkflowStage.AUDITED,
        }
        current_index = FORWARD_FLOW.index(stage)
        return [
            call
            for call in build_initial_plan(
                str(self.dxf_path),
                self.numbered_path,
                skip_unnumbered=self.context.skip_unnumbered,
            )
            if FORWARD_FLOW.index(tool_stages[call.tool]) >= current_index
        ]

    def cancel(self) -> None:
        self.session.cancel("用户取消任务")
        self._record_artifacts()
        self.store.save(self.session)

    def _record_artifacts(self) -> None:
        candidates = {
            "numbered_dxf": self.context.numbered_dxf_path,
            "nested_dxf": self.context.nested_dxf_path,
            "report": self.context.report_path,
            "check": self.context.check_path,
        }
        project_id = self.context.project_id or self.dxf_path.stem
        for key, path in candidates.items():
            if path is None or not Path(path).exists():
                continue
            ref = self.artifact_store.track(
                project_id,
                self.session.current_stage,
                path,
            )
            artifact = ref.to_dict()
            if self.object_store is not None:
                try:
                    object_key = (
                        f"{self.artifact_object_prefix}/{key}/{Path(path).name}"
                    )
                    object_ref = self.object_store.put_bytes(
                        object_key,
                        Path(path).read_bytes(),
                    )
                    artifact["object_key"] = object_ref["key"]
                except Exception:  # noqa: BLE001 - local path remains fallback
                    pass
            self.session.artifacts[key] = artifact

    def status(self) -> dict[str, Any]:
        snapshot = self.context.snapshot()
        return {
            "job_id": self.session.session_id,
            "stage": self.session.current_stage,
            "completed": self.last_result.completed if self.last_result else False,
            "blocked": self.last_result.blocked if self.last_result else False,
            "confirmation_required": (
                self.last_result.confirmation_required
                if self.last_result
                else False
            ),
            "artifacts": snapshot["artifacts"],
            "session_artifacts": self.session.artifacts,
            "error": self.last_error,
            "session": self.session.to_dict(),
            "last_result": (
                self.last_result.to_dict() if self.last_result else None
            ),
        }
