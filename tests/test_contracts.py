from datetime import datetime

import pytest
from pydantic import ValidationError

from src.contracts import (
    ConfirmedDrawing,
    CorrectionAction,
    CorrectionEvent,
    DrawingIssue,
    DrawingInputFormat,
    DrawingRevision,
    DrawingSource,
    IssueSeverity,
    IssueStatus,
    RecognizedDrawing,
    RecognizedPanel,
)


def _source() -> DrawingSource:
    return DrawingSource(
        source_id="src-1",
        source_format=DrawingInputFormat.DXF,
        original_filename="outlets2.dxf",
        file_sha256="abc123",
    )


def test_contract_json_roundtrip():
    source = _source()
    raw = source.to_json()
    restored = DrawingSource.from_json(raw)
    assert restored.source_id == source.source_id
    assert restored.source_format == DrawingInputFormat.DXF


def test_contract_digest_is_stable():
    source = _source()
    first = source.digest(exclude={"output_digest"})
    second = _source().digest(exclude={"output_digest"})
    assert first == second


def test_contract_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        DrawingSource(
            source_id="src-1",
            source_format="dxf",
            original_filename="a.dxf",
            file_sha256="abc",
            unknown_field=True,
        )


def test_recognized_drawing_contains_panels_and_digests():
    panel = RecognizedPanel(
        panel_id="p1",
        geometry_wkt="POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))",
        number="01B-1",
        material_group="01B",
        confidence=0.97,
    )
    drawing = RecognizedDrawing(
        drawing_id="draw-1",
        source_id="src-1",
        revision_id="rev-1",
        panels=[panel],
        input_digest="in-digest",
        output_digest="out-digest",
    )
    assert drawing.panels[0].number == "01B-1"
    assert drawing.schema_version == "0.1.0"


def test_drawing_issue_status_values():
    issue = DrawingIssue(
        issue_id="issue-1",
        severity=IssueSeverity.ERROR,
        issue_type="unclosed_geometry",
        message="图形未闭合",
        status=IssueStatus.NEW,
    )
    assert issue.status == IssueStatus.NEW
    assert issue.severity == IssueSeverity.ERROR


def test_correction_event_uses_enum_action():
    event = CorrectionEvent(
        event_id="event-1",
        issue_ids=["issue-1"],
        action=CorrectionAction.FIX,
    )
    assert event.action == CorrectionAction.FIX


def test_revision_and_confirmation_roundtrip():
    revision = DrawingRevision(
        revision_id="rev-1",
        source_id="src-1",
        change_summary="第一次上传",
    )
    confirmed = ConfirmedDrawing(
        confirmed_id="confirm-1",
        drawing_id="draw-1",
        revision_id=revision.revision_id,
        confirmed_at=datetime(2026, 8, 18, 12, 0, 0),
    )
    restored = ConfirmedDrawing.from_json(confirmed.to_json())
    assert restored.drawing_id == "draw-1"
