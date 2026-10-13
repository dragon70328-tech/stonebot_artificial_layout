import hashlib
import json
from pathlib import Path

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


def _run(dxf_path, output_root, **overrides):
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
        output_root=output_root,
    )
    kwargs.update(overrides)
    return app.run(str(dxf_path), 1000, 800, 20, **kwargs)


def _latest_session_payload(stem, output_root):
    candidates = sorted(Path(output_root).glob(f"*_{stem}"))
    assert candidates, "no output directory created"
    session_file = candidates[-1] / f"{stem}_workflow_session.json"
    assert session_file.exists(), f"missing {session_file}"
    return json.loads(session_file.read_text(encoding="utf-8")), candidates[-1]


def test_cli_run_records_completed_workflow_session(tmp_path):
    dxf_path = tmp_path / "session_full.dxf"
    _make_dxf(dxf_path)

    outcome = _run(dxf_path, tmp_path)

    assert not outcome["geometry_errors"]
    payload, _out_dir = _latest_session_payload("session_full", tmp_path)
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


def _report_of(payload):
    report_refs = [
        ref for ref in payload["artifacts"].values()
        if "_report_" in ref["path"] and ref["path"].endswith(".json")
    ]
    assert len(report_refs) == 1
    return json.loads(
        Path(report_refs[0]["path"]).read_text(encoding="utf-8")
    )


def test_cli_sheet_oversize_keeps_usable_space_at_nominal(tmp_path):
    """让尺是标称之外的实物余量，默认不进零件可利用区。"""
    dxf_path = tmp_path / "session_oversize.dxf"
    _make_dxf(dxf_path)

    outcome = _run(dxf_path, tmp_path, sheet_oversize=(20, 10))

    assert not outcome["geometry_errors"]
    payload, _out_dir = _latest_session_payload("session_oversize", tmp_path)
    report = _report_of(payload)
    assert report["sheets"][0]["width"] == 1000
    assert report["sheets"][0]["height"] == 800
    dimensions = report["sheet_dimensions"]
    assert dimensions["width"] == 1000
    assert dimensions["height"] == 800
    assert dimensions["usable_width"] == 1000
    assert dimensions["usable_height"] == 800
    assert dimensions["usable_includes_oversize"] is False
    assert dimensions["sheet_oversize_x"] == 20
    assert dimensions["sheet_oversize_y"] == 10


def test_cli_usable_includes_oversize_expands_nesting_space(tmp_path):
    """显式声明"可利用尺寸包括让尺"时，零件才用上让尺带。"""
    dxf_path = tmp_path / "session_usable_oversize.dxf"
    _make_dxf(dxf_path)

    outcome = _run(
        dxf_path,
        tmp_path,
        sheet_oversize=(20, 10),
        usable_includes_oversize=True,
    )

    assert not outcome["geometry_errors"]
    payload, _out_dir = _latest_session_payload(
        "session_usable_oversize", tmp_path
    )
    report = _report_of(payload)
    assert report["sheets"][0]["width"] == 1020
    assert report["sheets"][0]["height"] == 810
    assert report["total_sheet_area"] == (
        report["total_sheets"] * 1020 * 810
    )
    dimensions = report["sheet_dimensions"]
    assert dimensions["usable_width"] == 1020
    assert dimensions["usable_height"] == 810
    assert dimensions["usable_includes_oversize"] is True


def test_cli_kerf_check_gets_oversize_allowance_once(tmp_path, monkeypatch):
    """刀缝校验拿到的 allowance 只应是让尺本身，且让尺不重复计入。

    回归：旧实现把让尺既塞进排板空间（3230 当可利用）、又加进 allowance
    （再放宽一次），校验被放宽两次。
    """
    calls = []
    real_validate = app.validate_kerf_material

    def spy(sheets, kerf_mm, allowance_x_mm=0.0, allowance_y_mm=0.0):
        calls.append((kerf_mm, allowance_x_mm, allowance_y_mm))
        return real_validate(
            sheets,
            kerf_mm,
            allowance_x_mm=allowance_x_mm,
            allowance_y_mm=allowance_y_mm,
        )

    monkeypatch.setattr(app, "validate_kerf_material", spy)
    dxf_path = tmp_path / "session_kerf.dxf"
    _make_dxf(dxf_path)

    _run(
        dxf_path,
        tmp_path,
        kerf_mm=5,
        sheet_oversize=(20, 10),
        kerf_reserve_mm=30,
    )

    # 让尺 (20,10) 优先于兼容别名 --kerf-reserve，且没有被加两次
    assert calls == [(5.0, 20.0, 10.0)]


def test_cli_waterjet_skips_kerf_material_check(tmp_path, monkeypatch):
    """水刀刀缝按口径"可不计"：不做刀缝材料校验。"""
    calls = []
    monkeypatch.setattr(
        app,
        "validate_kerf_material",
        lambda *args, **kwargs: calls.append(args) or [],
    )
    dxf_path = tmp_path / "session_waterjet.dxf"
    _make_dxf(dxf_path)

    _run(
        dxf_path,
        tmp_path,
        kerf_mm=5,
        sheet_oversize=(20, 10),
        profile=app.PROFILES["waterjet"],
    )

    assert calls == []


def test_cli_check_only_stops_at_postprocess_confirmed(tmp_path):
    dxf_path = tmp_path / "session_check.dxf"
    _make_dxf(dxf_path)

    outcome = _run(dxf_path, tmp_path, check_only=True)

    assert outcome["check_only"] is True
    payload, _out_dir = _latest_session_payload("session_check", tmp_path)
    stages = [record["stage"] for record in payload["history"]]
    assert payload["current_stage"] == "postprocess_confirmed"
    assert "completed" not in stages
    assert "nested_reported" in stages
