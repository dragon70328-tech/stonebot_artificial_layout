"""Contracts for learning from in-place DXF confirmations and corrections."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import Field

from src.contracts import Contract


class FeedbackScope(str, Enum):
    """Where a user-approved learning signal may be applied."""

    PROJECT = "project"
    GLOBAL = "global"
    NONE = "none"


class CandidateSnapshot(Contract):
    """Compact, auditable snapshot of one learned panel or hole."""

    target_id: str
    kind: str
    entity_handle: str | None = None
    layer: str | None = None
    number_layer: str | None = None
    entity_type: str | None = None
    number: str | None = None
    material_group: str | None = None
    area: float = Field(ge=0)
    bounds: list[float] = Field(min_length=4, max_length=4)
    geometry_wkt: str
    classification: dict | None = None
    source: str | None = None


class RecognitionFeedback(Contract):
    """One user-confirmed correction revision used as a learning sample."""

    feedback_id: str = Field(default_factory=lambda: f"feedback-{uuid.uuid4().hex}")
    job_id: str | None = None
    project_id: str = ""
    drawing_digest: str
    revision: int = Field(ge=1)
    instruction: str
    scope: FeedbackScope = FeedbackScope.PROJECT
    operations: list[dict] = Field(default_factory=list)
    before_panels: list[CandidateSnapshot] = Field(default_factory=list)
    after_panels: list[CandidateSnapshot] = Field(default_factory=list)
    before_holes: list[CandidateSnapshot] = Field(default_factory=list)
    after_holes: list[CandidateSnapshot] = Field(default_factory=list)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
