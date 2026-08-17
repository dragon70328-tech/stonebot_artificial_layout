from pathlib import Path

import ezdxf

from src.drawing_profile import (
    DrawingProfile,
    DrawingIssue,
    audit_drawing,
    load_profile,
    match_profile,
    rank_profiles,
    write_audit_json,
)


PROFILE_PATH = Path(__file__).resolve().parents[1] / "drawing_profiles" / "dongguan_outlets.json"


def test_load_dongguan_outlets_profile():
    profile = load_profile(PROFILE_PATH)
    assert profile.name == "dongguan_outlets"
    assert profile.panel_layer == "石材分缝"
    assert "石材箱号" in profile.number_layers
    assert "CIRCLE" in profile.exclude_entity_types


def test_match_profile_prefers_matching_fingerprint():
    profile = load_profile(PROFILE_PATH)
    wrong_profile = DrawingProfile(
        name="other",
        version="1.0.0",
        panel_layer="其他层",
        use_hatch=False,
        number_layers=["其他编号"],
    )

    fingerprint = {
        "layer_counts": {
            "石材分缝": 780,
            "石材箱号": 750,
            "石材编号": 25,
        },
        "entity_counts": {
            "HATCH": 0,
            "LWPOLYLINE": 775,
            "LINE": 5,
            "CIRCLE": 0,
        },
        "has_hatch": False,
        "has_lwpolyline": True,
        "has_line": True,
        "has_circle": False,
    }

    best, score = match_profile(fingerprint, [wrong_profile, profile])
    assert best is profile
    assert score > 0


def test_rank_profiles_orders_matching_profile_first():
    profile = load_profile(PROFILE_PATH)
    wrong_profile = DrawingProfile(
        name="other",
        version="1.0.0",
        panel_layer="其他层",
        number_layers=["其他编号"],
    )
    fingerprint = {
        "layer_counts": {
            "石材分缝": 780,
            "石材箱号": 750,
        },
        "entity_counts": {"HATCH": 0, "LWPOLYLINE": 775},
        "has_hatch": False,
        "has_lwpolyline": True,
        "has_line": False,
        "has_circle": False,
    }
    ranked = rank_profiles(fingerprint, [wrong_profile, profile])
    assert ranked[0][0] is profile


def _test_profile() -> DrawingProfile:
    return DrawingProfile(
        name="test",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )


def _write_dxf(path: Path, entities_callback) -> None:
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("NUM")
    entities_callback(doc.modelspace())
    doc.saveas(path)


def test_audit_detects_unclosed_polyline(tmp_path):
    path = tmp_path / "unclosed.dxf"

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (100, 0), (100, 100)],
            dxfattribs={"layer": "PANEL"},
        )

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, _test_profile())
    assert any(issue.type == "unclosed_geometry" for issue in issues)


def test_audit_detects_duplicate_panels(tmp_path):
    path = tmp_path / "duplicate.dxf"
    points = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]

    def add_entities(msp):
        msp.add_lwpolyline(points, dxfattribs={"layer": "PANEL"})
        msp.add_lwpolyline(points, dxfattribs={"layer": "PANEL"})

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, _test_profile())
    assert any(issue.type == "duplicate_geometry" for issue in issues)


def test_audit_detects_number_without_panel(tmp_path):
    path = tmp_path / "number_without_panel.dxf"
    points = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]

    def add_entities(msp):
        msp.add_lwpolyline(points, dxfattribs={"layer": "PANEL"})
        text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
        text.set_placement((500, 500))

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, _test_profile())
    assert any(issue.type == "number_without_panel" for issue in issues)


def test_write_audit_json(tmp_path):
    issues = [
        DrawingIssue(
            issue_id=1,
            severity="warning",
            type="unclosed_geometry",
            entity_handle=None,
            layer="PANEL",
            coordinates=(10.0, 20.0),
            message="未封闭",
            suggestion="闭合",
        )
    ]
    output = write_audit_json(issues, tmp_path / "audit.json")
    assert output.exists()
    assert output.read_text(encoding="utf-8").startswith("{")
