"""Workflow orchestrator for structured tool plans.

The orchestrator is the state machine's first enforcement point: it validates
every proposed tool call against the current stage, delegates execution to a
Worker, and only then advances the session. It never executes production code
directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.ai_planning import (
    PRODUCTION_WRITE_TOOLS,
    ToolCall,
    validate_plan,
)
from src.tool_schemas import get_tool_spec
from src.worker import ToolResult, Worker
from src.workflow_session import WorkflowSession, WorkflowStage


@dataclass
class OrchestrationResult:
    completed: bool
    blocked: bool = False
    tool_results: list[ToolResult] = field(default_factory=list)
    session_stage: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "blocked": self.blocked,
            "session_stage": self.session_stage,
            "tool_results": [result.to_dict() for result in self.tool_results],
        }


class Orchestrator:
    def __init__(self, session: WorkflowSession, worker: Worker) -> None:
        self.session = session
        self.worker = worker

    def run_plan(self, calls: list[ToolCall]) -> OrchestrationResult:
        results: list[ToolResult] = []
        wrote_production_artifact = False

        for call in calls:
            plan_check = validate_plan(self.session.stage, [call])
            if not plan_check.valid:
                self.session.block(
                    "invalid tool plan",
                    summary={"errors": plan_check.errors},
                )
                return OrchestrationResult(
                    completed=False,
                    blocked=True,
                    tool_results=results,
                    session_stage=self.session.current_stage,
                )

            result = self.worker.execute(call, stage=self.session.stage)
            results.append(result)
            if not result.ok:
                self.session.block(
                    f"tool {call.tool} failed",
                    summary={"error": result.error},
                )
                return OrchestrationResult(
                    completed=False,
                    blocked=True,
                    tool_results=results,
                    session_stage=self.session.current_stage,
                )

            spec = get_tool_spec(call.tool)
            if spec is not None and spec.advances_to is not None:
                self.session.transition(
                    spec.advances_to,
                    summary={"tool": call.tool},
                )
            if call.tool in PRODUCTION_WRITE_TOOLS:
                wrote_production_artifact = True

        if wrote_production_artifact:
            if self.session.stage == WorkflowStage.POSTPROCESS_CONFIRMED:
                self.session.transition(
                    WorkflowStage.COMPLETED,
                    summary={"production_artifacts_written": True},
                )

        return OrchestrationResult(
            completed=self.session.stage == WorkflowStage.COMPLETED,
            blocked=False,
            tool_results=results,
            session_stage=self.session.current_stage,
        )
