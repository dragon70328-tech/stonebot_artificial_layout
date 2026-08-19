import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import main as app
from src.contracts import DrawingIssue as ContractDrawingIssue
from src.contracts import IssueSeverity, IssueStatus, ReviewState
from src.drawing_profile import DrawingIssue


def test_parse_args_audit_flag(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--audit", "--accept-issue", "1,3", "sample.dxf"],
    )
    args = app.parse_args()
    assert args.audit is True
    assert args.accept_issue == "1,3"


def test_split_issue_ids():
    assert app._split_issue_ids("1,3, 5") == ["1", "3", "5"]
    assert app._split_issue_ids(None) == []


def test_apply_issue_statuses():
    issues = [
        ContractDrawingIssue(
            issue_id="1",
            severity=IssueSeverity.WARNING,
            issue_type="unclosed_geometry",
            message="未闭合",
        ),
        ContractDrawingIssue(
            issue_id="2",
            severity=IssueSeverity.WARNING,
            issue_type="duplicate_geometry",
            message="重复图形",
        ),
    ]
    updated = app._apply_issue_statuses(
        issues,
        accept_issue_ids=["1"],
        ignore_issue_ids=[],
        fixed_issue_ids=["2"],
    )
    assert updated[0].status == IssueStatus.ACCEPTED
    assert updated[1].status == IssueStatus.FIXED


def test_severity_contract_maps_legacy_values():
    assert app._severity_contract("error") == IssueSeverity.ERROR
    assert app._severity_contract("warning") == IssueSeverity.WARNING
    assert app._severity_contract("info") == IssueSeverity.INFO


def test_to_contract_issue_maps_fields():
    legacy = DrawingIssue(
        issue_id=7,
        severity="warning",
        type="unclosed_geometry",
        entity_handle="AB12",
        layer="石材分缝",
        coordinates=(123.0, 456.0),
        message="图形未闭合",
        suggestion="请闭合",
    )
    contract = app._to_contract_issue(legacy)
    assert contract.issue_id == "7"
    assert contract.severity == IssueSeverity.WARNING
    assert contract.status == IssueStatus.NEW
    assert contract.issue_type == "unclosed_geometry"
    assert contract.coordinates == (123.0, 456.0)


def test_run_audit_writes_contract_json(monkeypatch, tmp_path):
    source_dxf = tmp_path / "sample.dxf"
    source_dxf.write_bytes(b"DXF-PLACEHOLDER")
    legacy_issue = DrawingIssue(
        issue_id=1,
        severity="warning",
        type="unclosed_geometry",
        entity_handle=None,
        layer="PANEL",
        coordinates=(10.0, 20.0),
        message="未闭合",
        suggestion="闭合",
    )
    profile = SimpleNamespace(name="test_profile")
    monkeypatch.setattr(app, "resolve_drawing_profile", lambda path: profile)
    monkeypatch.setattr(app, "audit_drawing", lambda path, prof: [legacy_issue])
    monkeypatch.setattr(app, "make_output_dir", lambda path: tmp_path)
    monkeypatch.setattr(app, "write_issue_evidence_svg", lambda path, issues, out_dir: [])
    overview_paths = []
    monkeypatch.setattr(
        app,
        "write_dxf_overview_svg",
        lambda source, output, issues=None: overview_paths.append(Path(output)) or Path(output),
    )
    written_paths = []
    monkeypatch.setattr(
        app,
        "write_audit_dxf",
        lambda source, issues, output: written_paths.append(Path(output)) or Path(output),
    )

    app.run_audit(str(source_dxf), accept_issue_ids=["1"])

    audit_json = tmp_path / "sample_audit.json"
    assert audit_json.exists()
    payload = json.loads(audit_json.read_text(encoding="utf-8"))
    assert payload["issue_count"] == 1
    assert payload["issues"][0]["issue_type"] == "unclosed_geometry"
    assert len(written_paths) == 1
    assert overview_paths == [tmp_path / "sample_overview.svg"]
    state_path = tmp_path / "sample_review_state.json"
    assert state_path.exists()
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_payload["issues"][0]["status"] == "accepted"


def test_run_audit_recheck_marks_fixed_and_new(monkeypatch, tmp_path):
    source_dxf = tmp_path / "revised.dxf"
    source_dxf.write_bytes(b"DXF-PLACEHOLDER-REVISED")
    previous_state_path = tmp_path / "previous_review_state.json"
    previous = ReviewState(
        review_id="review_previous",
        drawing_path="old.dxf",
        file_sha256="old-hash",
        profile_name="test_profile",
        issues=[
            ContractDrawingIssue(
                issue_id="1",
                severity=IssueSeverity.WARNING,
                issue_type="unclosed_geometry",
                layer="PANEL",
                coordinates=(10.0, 20.0),
                message="未闭合",
                status=IssueStatus.NEW,
            ),
            ContractDrawingIssue(
                issue_id="2",
                severity=IssueSeverity.ERROR,
                issue_type="duplicate_geometry",
                layer="PANEL",
                coordinates=(30.0, 40.0),
                message="重复图形",
                status=IssueStatus.NEW,
            ),
        ],
        created_at=datetime(2026, 8, 18, 10, 0, 0),
    )
    previous_state_path.write_text(previous.to_json(), encoding="utf-8")

    current_issues = [
        DrawingIssue(
            issue_id=1,
            severity="warning",
            type="unclosed_geometry",
            entity_handle=None,
            layer="PANEL",
            coordinates=(11.0, 21.0),
            message="未闭合",
            suggestion="闭合",
        ),
        DrawingIssue(
            issue_id=2,
            severity="warning",
            type="new_issue",
            entity_handle=None,
            layer="PANEL",
            coordinates=(99.0, 88.0),
            message="新增问题",
            suggestion="处理",
        ),
    ]
    profile = SimpleNamespace(name="test_profile")
    monkeypatch.setattr(app, "resolve_drawing_profile", lambda path: profile)
    monkeypatch.setattr(app, "audit_drawing", lambda path, prof: current_issues)
    monkeypatch.setattr(app, "make_output_dir", lambda path: tmp_path)
    monkeypatch.setattr(app, "write_issue_evidence_svg", lambda path, issues, out_dir: [])
    monkeypatch.setattr(
        app,
        "write_dxf_overview_svg",
        lambda source, output, issues=None: Path(output),
    )
    monkeypatch.setattr(
        app,
        "write_audit_dxf",
        lambda source, issues, output: Path(output),
    )

    app.run_audit(str(source_dxf), previous_state_path=str(previous_state_path))

    recheck_json = tmp_path / "revised_recheck.json"
    assert recheck_json.exists()
    payload = json.loads(recheck_json.read_text(encoding="utf-8"))
    assert payload["fixed_issue_ids"] == ["2"]
    assert payload["still_open_issue_ids"] == ["1"]
    assert payload["new_issue_ids"] == ["2"]
