import hashlib
import json

import ezdxf

import main as app
from src.drawing_profile import DrawingProfile
from src.units import UnitSystem


def _make_dxf(path):
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("NUM")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (400, 0), (400, 300), (0, 300), (0, 0)],
        dxfattribs={"layer": "PANEL"},
    )
    msp.add_lwpolyline(
        [(500, 0), (900, 0), (900, 300), (500, 300), (500, 0)],
        dxfattribs={"layer": "PANEL"},
    )
    text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
    text.set_placement((200, 150))
    text = msp.add_text("01B-2", dxfattribs={"layer": "NUM"})
    text.set_placement((700, 150))
    doc.saveas(path)


def _profile():
    return DrawingProfile(
        name="session_test",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )


def _run(dxf_path, **overrides):
    kwargs = dict(
        unit=UnitSystem.METRIC,
        trials=1,
        seed=0,
        budget=0,
        skip_unnumbered=False,
        layers=None,
        exclude_layers=None,
        drawing_profile=_profile(),
        profile=app.PROFILES["quick"],
        confirm_sheet_count=False,
        quick=True,
    )
    kwargs.update(overrides)
    return app.run(str(dxf_path), 1000, 800, 20, **kwargs)


def _latest_session_payload(stem):
    candidates = sorted(app.PROJECT_ROOT.glob(f"output/*_{stem}"))
    assert candidates, "no output directory created"
    session_file = candidates[-1] / f"{stem}_workflow_session.json"
    assert session_file.exists(), f"missing {session_file}"
    return json.loads(session_file.read_text(encoding="utf-8")), candidates[-1]


def test_cli_run_records_completed_workflow_session(tmp_path):
    dxf_path = tmp_path / "session_full.dxf"
    _make_dxf(dxf_path)

    outcome = _run(dxf_path)

    assert not outcome["geometry_errors"]
    payload, _out_dir = _latest_session_payload("session_full")
    stages = [record["stage"] for record in payload["history"]]
    assert stages == [
        "analyzed",
        "profile_matched",
        "read",
        "audited",
        "numbering_confirmed",
        "nested_reported",
        "postprocess_confirmed",
        "completed",
    ]
    assert payload["current_stage"] == "completed"

    artifacts = payload["artifacts"]
    nested_refs = [
        ref for ref in artifacts.values()
        if "_nested_" in ref["path"] and ref["path"].endswith(".dxf")
    ]
    assert len(nested_refs) == 1
    digest = hashlib.sha256(
        bytes(open(nested_refs[0]["path"], "rb").read())
    ).hexdigest()
    assert nested_refs[0]["digest"] == digest


def test_cli_check_only_stops_at_postprocess_confirmed(tmp_path):
    dxf_path = tmp_path / "session_check.dxf"
    _make_dxf(dxf_path)

    outcome = _run(dxf_path, check_only=True)

    assert outcome["check_only"] is True
    payload, _out_dir = _latest_session_payload("session_check")
    stages = [record["stage"] for record in payload["history"]]
    assert payload["current_stage"] == "postprocess_confirmed"
    assert "completed" not in stages
    assert "nested_reported" in stages
