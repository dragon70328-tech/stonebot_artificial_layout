"""读图子系统数据契约 v0。

该模块只定义跨工具、跨服务传递的结构化对象，不包含几何计算和排板逻辑。
所有契约都应包含版本、输入摘要或输出摘要，以便确定性执行、缓存和审计。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = "0.1.0"


class DrawingInputFormat(str, Enum):
    DXF = "dxf"
    DWG = "dwg"
    PDF = "pdf"
    SKETCH = "sketch"


class ActorKind(str, Enum):
    USER = "user"
    AI = "ai"
    SYSTEM = "system"


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class IssueStatus(str, Enum):
    NEW = "new"
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    FIXED = "fixed"
    VERIFIED = "verified"
    NEEDS_MANUAL_FIX = "needs_manual_fix"


class CorrectionAction(str, Enum):
    ACCEPT = "accept"
    IGNORE = "ignore"
    FIX = "fix"
    VERIFY = "verify"
    REUPLOAD = "reupload"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Contract(BaseModel):
    """所有读图契约的公共基类。"""

    schema_version: str = SCHEMA_VERSION
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    def to_json(self, indent: int | None = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Contract":
        return cls.model_validate_json(raw)

    def digest(self, exclude: set[str] | None = None) -> str:
        excluded = {"created_at", "resolved_at", "confirmed_at"}
        excluded.update(exclude or set())
        payload = self.model_dump(mode="json", exclude=excluded, exclude_none=True)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return _sha256(canonical)


class DrawingSource(Contract):
    """原始输入文件的来源与转换信息。"""

    source_id: str
    source_format: DrawingInputFormat
    original_filename: str
    file_sha256: str
    normalized_path: str | None = None
    converter: str | None = None
    converter_version: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    input_digest: str | None = None
    output_digest: str | None = None


class DrawingRevision(Contract):
    """同一图纸的版本，用于用户重传或结构化修正后的复检。"""

    revision_id: str
    revision_no: int = 1
    parent_revision_id: str | None = None
    source_id: str
    change_summary: str = ""
    created_by: ActorKind = ActorKind.USER
    created_at: datetime = Field(default_factory=datetime.utcnow)
    input_digest: str | None = None
    output_digest: str | None = None


class RecognizedHole(Contract):
    """识别出的孔洞，属于某个规格件。"""

    hole_id: str
    geometry_wkt: str
    layer: str | None = None
    entity_handle: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class RecognizedPanel(Contract):
    """识别出的规格件，可能是台面板、挡水条或其他异形件。"""

    panel_id: str
    geometry_wkt: str
    outer_geometry_wkt: str | None = None
    layer: str | None = None
    entity_handle: str | None = None
    number: str | None = None
    material_group: str | None = None
    holes: list[RecognizedHole] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    properties: dict[str, Any] = Field(default_factory=dict)


class RecognizedText(Contract):
    """识别出的文字及其与面板的关系。"""

    text_id: str
    raw_text: str
    normalized_text: str | None = None
    layer: str | None = None
    entity_handle: str | None = None
    insert_point: tuple[float, float] | None = None
    bbox_wkt: str | None = None
    assigned_panel_id: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class RecognizedDrawing(Contract):
    """一次读图与语义识别后的完整结果。"""

    drawing_id: str
    source_id: str
    revision_id: str
    extraction_version: str = SCHEMA_VERSION
    unit: str = "mm"
    tolerance: float = 0.01
    closed_tolerance: float = 0.01
    profile_name: str | None = None
    profile_match_score: float | None = None
    panels: list[RecognizedPanel] = Field(default_factory=list)
    texts: list[RecognizedText] = Field(default_factory=list)
    input_digest: str | None = None
    output_digest: str | None = None


class DrawingIssue(Contract):
    """审图发现的一个问题。"""

    issue_id: str
    severity: IssueSeverity = IssueSeverity.WARNING
    issue_type: str
    entity_handle: str | None = None
    layer: str | None = None
    coordinates: tuple[float, float] | None = None
    message: str
    suggestion: str = ""
    evidence_artifact_id: str | None = None
    evidence_digest: str | None = None
    status: IssueStatus = IssueStatus.NEW
    created_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: datetime | None = None
    resolution_note: str | None = None


class CorrectionEvent(Contract):
    """用户或 AI 对审图问题的处理记录。"""

    event_id: str
    issue_ids: list[str] = Field(default_factory=list)
    action: CorrectionAction
    description: str = ""
    created_by: ActorKind = ActorKind.USER
    created_at: datetime = Field(default_factory=datetime.utcnow)
    before_digest: str | None = None
    after_digest: str | None = None


class ConfirmedDrawing(Contract):
    """用户确认后的图纸快照，是排板阶段的唯一输入。"""

    confirmed_id: str
    drawing_id: str
    revision_id: str
    confirmed_by: ActorKind = ActorKind.USER
    confirmed_at: datetime = Field(default_factory=datetime.utcnow)
    confirmed_digest: str | None = None
    artifact_path: str | None = None
    notes: str = ""


class ReviewState(Contract):
    """一次审图的持久化状态，用于修正重传后的复检对比。"""

    review_id: str
    drawing_path: str
    file_sha256: str
    profile_name: str | None = None
    parent_review_id: str | None = None
    issues: list[DrawingIssue] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RecheckSummary(Contract):
    """修正后复检的差异摘要。"""

    parent_review_id: str | None = None
    child_review_id: str
    fixed_issue_ids: list[str] = Field(default_factory=list)
    still_open_issue_ids: list[str] = Field(default_factory=list)
    new_issue_ids: list[str] = Field(default_factory=list)
    verified_issue_ids: list[str] = Field(default_factory=list)


def compute_digest(contract: Contract, exclude: set[str] | None = None) -> str:
    """为契约生成稳定摘要，供输入/输出校验使用。"""
    return contract.digest(exclude=exclude)
