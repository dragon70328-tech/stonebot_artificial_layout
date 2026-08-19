from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

from src.drawing_profile import (
    DrawingProfile,
    DrawingIssue,
    audit_drawing,
    load_profile,
    match_profile,
    rank_profiles,
    read_dxf_with_profile,
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


def test_read_dxf_with_profile_reads_line_panels_and_numbers(tmp_path):
    path = tmp_path / "line_panel.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("NUM")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "PANEL"})
    msp.add_line((100, 0), (100, 100), dxfattribs={"layer": "PANEL"})
    msp.add_line((100, 100), (0, 100), dxfattribs={"layer": "PANEL"})
    msp.add_line((0, 100), (0, 0), dxfattribs={"layer": "PANEL"})
    text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
    text.set_placement((50, 50))
    doc.saveas(path)

    parts, _ = read_dxf_with_profile(path, _test_profile())
    assert len(parts) == 1
    assert parts[0]["original_number"] == "01B-1"


def test_read_dxf_with_profile_reads_hatch_panels(tmp_path):
    path = tmp_path / "hatch_panel.dxf"
    profile = DrawingProfile(
        name="hatch_test",
        version="1.0.0",
        panel_layer="PANEL",
        hatch_layer="HATCH",
        use_hatch=True,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )
    doc = ezdxf.new("R2010")
    doc.layers.add("HATCH")
    doc.layers.add("NUM")
    msp = doc.modelspace()
    hatch = msp.add_hatch(dxfattribs={"layer": "HATCH"})
    hatch.paths.add_polyline_path(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        is_closed=True,
    )
    text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
    text.set_placement((50, 50))
    doc.saveas(path)

    parts, _ = read_dxf_with_profile(path, profile)
    assert len(parts) == 1
    assert parts[0]["original_number"] == "01B-1"
    assert parts[0]["outer_polygon"].area > 0


def test_audit_detects_open_chain(tmp_path):
    path = tmp_path / "open_chain.dxf"

    def add_entities(msp):
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "PANEL"})

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, _test_profile())
    assert any(issue.type == "open_chain" for issue in issues)


def test_audit_detects_self_intersecting_geometry(tmp_path):
    path = tmp_path / "self_intersecting.dxf"

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (100, 100), (100, 0), (0, 100), (0, 0)],
            dxfattribs={"layer": "PANEL"},
            format="xy",
        )

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, _test_profile())
    assert any(issue.type == "self_intersecting_geometry" for issue in issues)


def test_audit_detects_duplicate_label(tmp_path):
    path = tmp_path / "duplicate_label.dxf"

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
            dxfattribs={"layer": "PANEL"},
        )
        msp.add_lwpolyline(
            [(200, 0), (300, 0), (300, 100), (200, 100), (200, 0)],
            dxfattribs={"layer": "PANEL"},
        )
        text1 = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
        text1.set_placement((50, 50))
        text2 = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
        text2.set_placement((250, 50))

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, _test_profile())
    assert any(issue.type == "duplicate_label" for issue in issues)


def test_audit_detects_number_outside_panel(tmp_path):
    path = tmp_path / "number_outside_panel.dxf"

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
            dxfattribs={"layer": "PANEL"},
        )
        text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
        text.set_placement((100, 50), align=TextEntityAlignment.MIDDLE_CENTER)

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, _test_profile())
    assert any(issue.type == "number_outside_panel" for issue in issues)


def test_audit_detects_hole_outside_panel(tmp_path):
    path = tmp_path / "hole_outside_panel.dxf"

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
            dxfattribs={"layer": "PANEL"},
        )
        msp.add_circle((300, 300), 20, dxfattribs={"layer": "PANEL"})

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, _test_profile())
    assert any(issue.type == "hole_outside_panel" for issue in issues)
