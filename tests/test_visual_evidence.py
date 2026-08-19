from pathlib import Path

import ezdxf

from src.contracts import DrawingIssue, IssueSeverity, IssueStatus
from src.visual_evidence import write_issue_evidence_svg


def _write_source_dxf(path: Path) -> None:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 100))
    doc.saveas(path)


def test_write_issue_evidence_svg(tmp_path):
    source = tmp_path / "source.dxf"
    _write_source_dxf(source)
    issue = DrawingIssue(
        issue_id="1",
        severity=IssueSeverity.WARNING,
        issue_type="unclosed_geometry",
        coordinates=(50.0, 50.0),
        message="未闭合",
        status=IssueStatus.NEW,
    )

    results = write_issue_evidence_svg(source, [issue], tmp_path)

    assert len(results) == 1
    assert results[0]["artifact_id"] == "evidence_1.svg"
    evidence = tmp_path / "evidence_1.svg"
    assert evidence.exists()
    text = evidence.read_text(encoding="utf-8")
    assert "<svg" in text
    assert "<circle" in text


def test_write_issue_evidence_svg_skips_missing_coordinates(tmp_path):
    issue = DrawingIssue(
        issue_id="2",
        severity=IssueSeverity.INFO,
        issue_type="non_panel_text",
        message="无坐标",
        status=IssueStatus.NEW,
    )
    results = write_issue_evidence_svg("missing.dxf", [issue], tmp_path)
    assert results == []
