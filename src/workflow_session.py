"""In-process workflow state machine for drawing intake and nesting.

This is a first-stage implementation of the scheduler loop described in
``docs/saas-architecture-goal.md``. It intentionally keeps persistence and
orchestration out of ``main.py`` so the CLI can later be replaced by a worker
without changing the state contract.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class WorkflowStage(str, Enum):
    UPLOADED = "uploaded"
    ANALYZED = "analyzed"
    PROFILE_MATCHED = "profile_matched"
    READ = "read"
    AUDITED = "audited"
    NUMBERING_CONFIRMED = "numbering_confirmed"
    NESTED_REPORTED = "nested_reported"
    POSTPROCESS_CONFIRMED = "postprocess_confirmed"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


FORWARD_FLOW: tuple[WorkflowStage, ...] = (
    WorkflowStage.UPLOADED,
    WorkflowStage.ANALYZED,
    WorkflowStage.PROFILE_MATCHED,
    WorkflowStage.READ,
    WorkflowStage.AUDITED,
    WorkflowStage.NUMBERING_CONFIRMED,
    WorkflowStage.NESTED_REPORTED,
    WorkflowStage.POSTPROCESS_CONFIRMED,
    WorkflowStage.COMPLETED,
)

TERMINAL_STAGES = {WorkflowStage.COMPLETED, WorkflowStage.CANCELLED}


@dataclass
class StageRecord:
    stage: str
    started_at: float
    finished_at: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    artifact_id: str | None = None

    @property
    def elapsed_seconds(self) -> float:
        if self.finished_at is None:
            return time.time() - self.started_at
        return max(0.0, self.finished_at - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "summary": self.summary,
            "artifact_id": self.artifact_id,
        }


@dataclass
class WorkflowSession:
    """Stateful session with validated stage transitions."""

    session_id: str
    stage: WorkflowStage = WorkflowStage.UPLOADED
    history: list[StageRecord] = field(default_factory=list)

    @property
    def current_stage(self) -> str:
        return self.stage.value

    def _record(self, target: WorkflowStage, summary: dict | None, artifact_id: str | None) -> None:
        record = StageRecord(
            stage=target.value,
            started_at=time.time(),
            finished_at=time.time(),
            summary=summary or {},
            artifact_id=artifact_id,
        )
        self.history.append(record)

    def transition(
        self,
        target: WorkflowStage | str,
        *,
        reason: str | None = None,
        summary: dict | None = None,
        artifact_id: str | None = None,
    ) -> "WorkflowSession":
        """Move to the next allowed workflow stage."""
        target_stage = target if isinstance(target, WorkflowStage) else WorkflowStage(target)
        if target_stage == self.stage:
            return self
        self._validate_forward(target_stage)
        if reason and target_stage not in TERMINAL_STAGES:
            summary = {**(summary or {}), "transition_reason": reason}
        self._record(target_stage, summary, artifact_id)
        self.stage = target_stage
        return self

    def _validate_forward(self, target: WorkflowStage) -> None:
        if self.stage in TERMINAL_STAGES:
            raise ValueError(f"terminal stage {self.stage.value} cannot transition")
        try:
            current_index = FORWARD_FLOW.index(self.stage)
            target_index = FORWARD_FLOW.index(target)
        except ValueError as exc:
            raise ValueError(f"unknown workflow stage: {target.value}") from exc
        if target_index != current_index + 1:
            raise ValueError(
                f"invalid transition {self.stage.value} -> {target.value}"
            )

    def backtrack(
        self,
        target: WorkflowStage | str,
        *,
        reason: str | None = None,
        artifact_id: str | None = None,
    ) -> "WorkflowSession":
        """Return to an earlier stage, preserving the prior stage record."""
        target_stage = target if isinstance(target, WorkflowStage) else WorkflowStage(target)
        if self.stage in TERMINAL_STAGES:
            raise ValueError(f"terminal stage {self.stage.value} cannot backtrack")
        try:
            current_index = FORWARD_FLOW.index(self.stage)
            target_index = FORWARD_FLOW.index(target_stage)
        except ValueError as exc:
            raise ValueError(f"unknown workflow stage: {target_stage.value}") from exc
        if target_index >= current_index:
            raise ValueError(
                f"backtrack target must be earlier than {self.stage.value}"
            )
        self._record(
            target_stage,
            {"backtracked_from": self.stage.value, "reason": reason or ""},
            artifact_id,
        )
        self.stage = target_stage
        return self

    def block(self, reason: str, *, summary: dict | None = None) -> "WorkflowSession":
        """Mark the session as blocked without losing the current stage context."""
        if self.stage in TERMINAL_STAGES:
            raise ValueError(f"terminal stage {self.stage.value} cannot be blocked")
        if self.stage == WorkflowStage.BLOCKED:
            return self
        self._record(
            WorkflowStage.BLOCKED,
            {"blocked_from": self.stage.value, "reason": reason, **(summary or {})},
            None,
        )
        self.stage = WorkflowStage.BLOCKED
        return self

    def resume(
        self,
        target: WorkflowStage | str,
        *,
        artifact_id: str | None = None,
    ) -> "WorkflowSession":
        """Resume from blocked into a non-terminal stage."""
        if self.stage != WorkflowStage.BLOCKED:
            raise ValueError("only a blocked session can be resumed")
        target_stage = target if isinstance(target, WorkflowStage) else WorkflowStage(target)
        if target_stage not in FORWARD_FLOW[:-1]:
            raise ValueError(f"cannot resume into {target_stage.value}")
        self._record(
            target_stage,
            {"resumed_from": WorkflowStage.BLOCKED.value},
            artifact_id,
        )
        self.stage = target_stage
        return self

    def cancel(self, reason: str) -> "WorkflowSession":
        """Cancel the session. Completed sessions cannot be cancelled."""
        if self.stage == WorkflowStage.COMPLETED:
            raise ValueError("completed session cannot be cancelled")
        if self.stage == WorkflowStage.CANCELLED:
            return self
        self._record(
            WorkflowStage.CANCELLED,
            {"cancelled_from": self.stage.value, "reason": reason},
            None,
        )
        self.stage = WorkflowStage.CANCELLED
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "current_stage": self.current_stage,
            "history": [record.to_dict() for record in self.history],
        }
