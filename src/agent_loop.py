"""Minimal deterministic agent loop.

The loop receives a plan from a pluggable provider, validates and executes it
through the orchestrator, persists the session, and reports whether a human
confirmation gate has been reached. It deliberately has no model dependency;
the provider can be a fake, an LLM client, or a rule-based planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from src.ai_planning import ToolCall
from src.orchestrator import Orchestrator
from src.session_store import WorkflowSessionStore
from src.worker import Worker
from src.workflow_session import WorkflowSession, WorkflowStage


class PlanProvider(Protocol):
    def plan(self, session: WorkflowSession, context: Any) -> list[ToolCall]:
        """Return the next tool plan for the current session stage."""


class StaticPlanProvider:
    def __init__(self, plans: list[list[ToolCall]]) -> None:
        self._plans = list(plans)
        self._cursor = 0

    def plan(self, session: WorkflowSession, context: Any) -> list[ToolCall]:
        if self._cursor >= len(self._plans):
            return []
        plan = self._plans[self._cursor]
        self._cursor += 1
        return plan


@dataclass
class AgentLoopResult:
    session_id: str
    stage: str
    completed: bool
    blocked: bool
    confirmation_required: bool
    tool_results: list[Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "stage": self.stage,
            "completed": self.completed,
            "blocked": self.blocked,
            "confirmation_required": self.confirmation_required,
            "tool_results": [result.to_dict() for result in self.tool_results],
        }


class AgentLoop:
    confirmation_stages = {
        WorkflowStage.AUDITED,
        WorkflowStage.NESTED_REPORTED,
        WorkflowStage.POSTPROCESS_CONFIRMED,
    }

    def __init__(
        self,
        session: WorkflowSession,
        worker: Worker,
        provider: PlanProvider,
        *,
        store: WorkflowSessionStore | None = None,
    ) -> None:
        self.session = session
        self.worker = worker
        self.provider = provider
        self.store = store
        self.orchestrator = Orchestrator(session, worker)

    def run_next(self, context: Any = None) -> AgentLoopResult:
        tool_results = []
        while True:
            plan = self.provider.plan(self.session, context)
            outcome = self.orchestrator.run_plan(plan)
            tool_results.extend(outcome.tool_results)
            if self.store is not None:
                self.store.save(self.session)
            if outcome.blocked or outcome.completed:
                return AgentLoopResult(
                    session_id=self.session.session_id,
                    stage=self.session.current_stage,
                    completed=outcome.completed,
                    blocked=outcome.blocked,
                    confirmation_required=False,
                    tool_results=tool_results,
                )
            if self.session.stage in self.confirmation_stages or not plan:
                return AgentLoopResult(
                    session_id=self.session.session_id,
                    stage=self.session.current_stage,
                    completed=False,
                    blocked=False,
                    confirmation_required=(
                        self.session.stage in self.confirmation_stages
                    ),
                    tool_results=tool_results,
                )
