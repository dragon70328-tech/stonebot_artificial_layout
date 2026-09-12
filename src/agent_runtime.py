"""Non-interactive runtime for one agent workflow job.

This wraps the deterministic tool chain and AgentLoop so both the local CLI
and an HTTP service can drive the same state machine. It never blocks on
``input()``; callers inspect ``status()`` and call ``confirm()`` at gates.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.agent_cli import (
    build_agent_plan_provider,
    build_initial_plan,
    plan_after_confirmation,
)
from src.agent_loop import AgentLoop, AgentLoopResult
from src.job_context import JobContext
from src.session_store import WorkflowSessionStore
from src.tool_handlers import build_tool_handlers
from src.units import UnitSystem
from src.worker import Worker
from src.workflow_session import WorkflowSession


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
        output_dir: Path | None = None,
        session_id: str | None = None,
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
        self.session = WorkflowSession(session_id=self.session_id)
        self.store = WorkflowSessionStore(self.output_dir)
        self.provider = build_agent_plan_provider(provider_name)
        self.worker = Worker(build_tool_handlers(self.context))
        self.loop = AgentLoop(
            self.session,
            self.worker,
            self.provider,
            store=self.store,
        )
        if provider_name == "queue":
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
        self.store.save(self.session)
        return self.last_result

    def confirm(self, decision: str) -> AgentLoopResult:
        if self.last_result is None or not self.last_result.confirmation_required:
            raise RuntimeError("no confirmation gate is waiting")

        normalized = decision.strip().lower()
        if normalized in {"cancel", "reject", "no", "n"}:
            self.session.cancel("用户拒绝确认")
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

    def cancel(self) -> None:
        self.session.cancel("用户取消任务")
        self.store.save(self.session)

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
            "error": self.last_error,
            "session": self.session.to_dict(),
            "last_result": (
                self.last_result.to_dict() if self.last_result else None
            ),
        }
