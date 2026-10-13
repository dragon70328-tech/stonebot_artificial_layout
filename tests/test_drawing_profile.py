from pathlib import Path

import pytest
import ezdxf
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import Point

from src.drawing_profile import (
    DrawingProfile,
    DrawingIssue,
    _assign_numbers_by_panel_layer,
    _assign_numbers_by_containing_region,
    _assign_room_numbers_to_panels,
    _assign_texts_to_panels,
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


def test_from_dict_applies_default_factory_fields():
    profile = DrawingProfile.from_dict(
        {
            "name": "minimal",
            "version": "1.0.0",
            "panel_layer": "PANEL",
        }
    )
    assert profile.number_layers == []
    assert profile.allowed_material_prefixes == []
    assert profile.exclude_entity_types == []
    assert profile.exclude_linetypes == ["DASH", "PHANTOM"]


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


def test_rank_profiles_prefers_hierarchy_for_circle_and_flat_for_no_circle():
    hierarchical = DrawingProfile(
        name="hierarchical",
        version="1.0.0",
        panel_layer="0",
        use_hatch=False,
        number_layers=["编号"],
        build_hierarchy=True,
    )
    flat = DrawingProfile(
        name="flat",
        version="1.0.0",
        panel_layer="0",
        use_hatch=False,
        number_layers=["编号"],
        build_hierarchy=False,
    )
    base_fingerprint = {
        "layer_counts": {"0": 100, "编号": 10},
        "entity_counts": {"LWPOLYLINE": 50},
        "has_hatch": False,
        "has_lwpolyline": True,
        "has_line": False,
    }

    with_circle = {**base_fingerprint, "has_circle": True}
    assert rank_profiles(with_circle, [flat, hierarchical])[0][0] is hierarchical

    without_circle = {**base_fingerprint, "has_circle": False}
    assert rank_profiles(without_circle, [hierarchical, flat])[0][0] is flat


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


def test_audit_does_not_report_excluded_linetype(tmp_path):
    path = tmp_path / "excluded_linetype.dxf"
    profile = DrawingProfile(
        name="excluded_linetype",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=["DASH"],
    )

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
            dxfattribs={"layer": "PANEL", "linetype": "DASH"},
        )

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, profile)
    assert not any(issue.type == "excluded_linetype_entity" for issue in issues)


def test_audit_does_not_report_non_number_text(tmp_path):
    path = tmp_path / "non_number_text.dxf"
    profile = DrawingProfile(
        name="non_number_text",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
            dxfattribs={"layer": "PANEL"},
        )
        text = msp.add_text("标题", dxfattribs={"layer": "TITLE"})
        text.set_placement((50, 50))

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, profile)
    assert not any(issue.type == "non_panel_text" for issue in issues)


def test_audit_ignore_rules_remove_degenerate_lwpolyline(tmp_path):
    path = tmp_path / "degenerate_lwpolyline.dxf"
    profile = DrawingProfile(
        name="degenerate_lwpolyline",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
        audit_ignore_rules=[
            {
                "issue_type": ["unclosed_geometry", "invalid_geometry"],
                "entity_type": "LWPOLYLINE",
                "vertex_count_max": 2,
                "action": "ignore",
            }
        ],
    )

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (10, 0)],
            dxfattribs={"layer": "PANEL"},
        )

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, profile)
    assert not any(
        issue.type in ("unclosed_geometry", "invalid_geometry")
        for issue in issues
    )


def test_assign_room_numbers_to_panels_uses_nearest_label_and_sequence():
    profile = DrawingProfile(
        name="mercury",
        version="1.0.0",
        panel_layer="0",
        use_hatch=False,
        number_layers=["0"],
        label_pattern=r"^户型[:：]\s*(?P<unit>[A-Za-z0-9\-]+)\s*(?P<room>.+)$",
        assignment_mode="nearest_room",
        room_number_format="{unit}-{room}-{index:02d}",
        room_max_distance=5000.0,
        build_hierarchy=False,
    )
    panels = [
        {"index": 0, "centroid": Point(0, 0)},
        {"index": 1, "centroid": Point(10, 0)},
        {"index": 2, "centroid": Point(0, 20)},
    ]
    texts = [
        {"text": "户型：A 厨房", "point": (0, 0)},
        {"text": "户型：A 卫生间", "point": (0, 20)},
    ]
    assignments = _assign_room_numbers_to_panels(panels, texts, profile)
    assert assignments == {
        0: "A-厨房-01",
        1: "A-厨房-02",
        2: "A-卫生间-01",
    }


def test_assign_texts_to_panels_uses_number_fallback_radius():
    profile = DrawingProfile(
        name="fallback",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
        number_fallback_radius=1000.0,
    )
    panels = [
        {
            "index": 0,
            "centroid": Point(100, 100),
            "polygon": Point(100, 100).buffer(1),
        }
    ]
    texts = [
        {
            "text": "01B-7",
            "point": (120, 120),
            "box": None,
            "entity": None,
            "layer": "NUM",
        }
    ]
    assignments, matched_texts = _assign_texts_to_panels(
        panels,
        texts,
        profile,
    )
    assert assignments == {0: [0]}
    assert matched_texts == {0}


def test_audit_nearest_room_uses_generated_room_numbers(tmp_path):
    path = tmp_path / "nearest_room_audit.dxf"
    profile = DrawingProfile(
        name="mercury_audit",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        label_pattern=r"^户型[:：]\s*(?P<unit>[A-Za-z0-9\-]+)\s*(?P<room>.+)$",
        assignment_mode="nearest_room",
        room_number_format="{unit}-{room}-{index:02d}",
        room_max_distance=5000.0,
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
        expected_counts={"panels": 2, "holes": 0, "numbers": 2},
    )

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
            dxfattribs={"layer": "PANEL"},
        )
        msp.add_lwpolyline(
            [(100, 0), (200, 0), (200, 100), (100, 100), (100, 0)],
            dxfattribs={"layer": "PANEL"},
        )
        text1 = msp.add_text("户型:A Kitchen", dxfattribs={"layer": "NUM"})
        text1.set_placement((50, 50))
        text2 = msp.add_text("户型:A Bath", dxfattribs={"layer": "NUM"})
        text2.set_placement((150, 50))

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, profile)
    assert not any(issue.type == "expected_count_mismatch" for issue in issues)
    assert not any(issue.type == "panel_without_number" for issue in issues)
    assert not any(issue.type == "number_outside_panel" for issue in issues)


def test_load_cgr45_profile_expectations_and_rules():
    profile = load_profile(
        Path(__file__).resolve().parents[1] / "drawing_profiles" / "cgr45.json"
    )
    assert profile.expected_counts == {"panels": 203, "holes": 306, "numbers": 203}
    assert profile.audit_ignore_rules
    assert profile.audit_ignore_rules[0]["action"] == "ignore"


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


def test_read_dxf_with_profile_assigns_holes_to_hatch_panels(tmp_path):
    path = tmp_path / "hatch_panel_with_hole.dxf"
    profile = DrawingProfile(
        name="hatch_hole_test",
        version="1.0.0",
        panel_layer="PANEL",
        hatch_layer="HATCH",
        hole_layers=["HOLES"],
        use_hatch=True,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )
    doc = ezdxf.new("R2010")
    for layer in ("HATCH", "HOLES", "NUM"):
        doc.layers.add(layer)
    msp = doc.modelspace()
    hatch = msp.add_hatch(dxfattribs={"layer": "HATCH"})
    hatch.paths.add_polyline_path(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        is_closed=True,
    )
    msp.add_circle((50, 50), radius=10, dxfattribs={"layer": "HOLES"})
    text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
    text.set_placement((80, 80))
    doc.saveas(path)

    parts, _ = read_dxf_with_profile(path, profile)
    assert len(parts) == 1
    assert len(parts[0]["holes"]) == 1
    assert parts[0]["polygon"].area < parts[0]["outer_polygon"].area


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


def test_audit_detects_low_confidence_line_panel(tmp_path):
    path = tmp_path / "low_confidence_panel.dxf"

    def add_entities(msp):
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "PANEL"})
        msp.add_line((100, 0), (100, 100), dxfattribs={"layer": "PANEL"})
        msp.add_line((100, 100), (0, 100), dxfattribs={"layer": "PANEL"})
        msp.add_line((0, 100), (0, 0), dxfattribs={"layer": "PANEL"})
        text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
        text.set_placement((50, 50))

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, _test_profile())
    assert any(issue.type == "low_confidence_entity" for issue in issues)


def test_audit_detects_material_conflict(tmp_path):
    path = tmp_path / "material_conflict.dxf"
    profile = DrawingProfile(
        name="material_test",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
        material_group_enabled=True,
        material_prefix_pattern=r"^(?P<prefix>\d{2}B)",
        allowed_material_prefixes=["01B"],
    )

    def add_entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
            dxfattribs={"layer": "PANEL"},
        )
        text = msp.add_text("02B-1", dxfattribs={"layer": "NUM"})
        text.set_placement((50, 50))

    _write_dxf(path, add_entities)
    issues = audit_drawing(path, profile)
    assert any(issue.type == "material_conflict" for issue in issues)


def test_rank_profiles_uses_panel_layers_collection():
    profile = DrawingProfile(
        name="multi_panel_layers",
        version="1.0.0",
        panel_layers=["ST-01水刀", "ST-02整砖"],
        use_hatch=False,
        number_layers=["0"],
        build_hierarchy=False,
    )
    wrong_profile = DrawingProfile(
        name="wrong",
        version="1.0.0",
        panel_layer="其他层",
        use_hatch=False,
        number_layers=["其他编号"],
        build_hierarchy=False,
    )
    fingerprint = {
        "layer_counts": {"ST-01水刀": 10, "ST-02整砖": 20, "0": 5},
        "entity_counts": {"LWPOLYLINE": 30},
        "has_hatch": False,
        "has_lwpolyline": True,
        "has_line": False,
        "has_circle": False,
    }
    ranked = rank_profiles(fingerprint, [wrong_profile, profile])
    assert ranked[0][0] is profile


def test_assign_numbers_by_panel_layer_uses_panel_layer_material():
    profile = DrawingProfile(
        name="panel_layer_numbering",
        version="1.0.0",
        panel_layers=["ST-01水刀", "ST-02整砖"],
        use_hatch=False,
        number_layers=["0"],
        label_pattern=r"^ST-(?P<material>\d+)",
        assignment_mode="panel_layer",
        room_number_format="ST-{unit}-{index:02d}",
        build_hierarchy=False,
    )
    panels = [
        {"index": 0, "centroid": Point(0, 0), "layer": "ST-01水刀"},
        {"index": 1, "centroid": Point(10, 0), "layer": "ST-01水刀"},
        {"index": 2, "centroid": Point(0, 20), "layer": "ST-02整砖"},
    ]
    assignments = _assign_numbers_by_panel_layer(panels, profile)
    assert assignments == {
        0: "ST-01-01",
        1: "ST-01-02",
        2: "ST-02-01",
    }


def test_assign_numbers_by_panel_layer_includes_nearest_zone():
    profile = DrawingProfile(
        name="panel_layer_zone_numbering",
        version="1.0.0",
        panel_layers=["ST-01水刀", "ST-02整砖"],
        use_hatch=False,
        number_layers=["0"],
        zone_layers=["0"],
        zone_label_pattern=r"^(?P<zone>\d+#)$",
        zone_max_distance=100000.0,
        label_pattern=r"^ST-(?P<material>\d+)",
        assignment_mode="panel_layer",
        room_number_format="4F-{zone}-ST-{unit}-{index:02d}",
        build_hierarchy=False,
    )
    panels = [
        {"index": 0, "centroid": Point(0, 0), "layer": "ST-01水刀"},
        {"index": 1, "centroid": Point(10, 0), "layer": "ST-01水刀"},
        {"index": 2, "centroid": Point(100, 0), "layer": "ST-02整砖"},
    ]
    zone_texts = [
        {"text": "1#", "point": (0, 0)},
        {"text": "2#", "point": (100, 0)},
    ]
    assignments = _assign_numbers_by_panel_layer(
        panels,
        profile,
        zone_texts=zone_texts,
    )
    assert assignments == {
        0: "4F-1#-ST-01-01",
        1: "4F-1#-ST-01-02",
        2: "4F-2#-ST-02-01",
    }


def test_read_dxf_with_profile_hole_layers_only_assigns_contained_circles(tmp_path):
    path = tmp_path / "contained_holes.dxf"
    doc = ezdxf.new("R2010")
    for layer in ("PANEL_A", "PANEL_B", "HOLES"):
        doc.layers.add(layer)
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
        dxfattribs={"layer": "PANEL_A"},
    )
    msp.add_lwpolyline(
        [(200, 0), (300, 0), (300, 100), (200, 100), (200, 0)],
        dxfattribs={"layer": "PANEL_B"},
    )
    msp.add_circle((50, 50), radius=10, dxfattribs={"layer": "HOLES"})
    msp.add_circle((500, 500), radius=10, dxfattribs={"layer": "HOLES"})
    doc.saveas(path)

    profile = DrawingProfile(
        name="contained_holes",
        version="1.0.0",
        panel_layers=["PANEL_A", "PANEL_B"],
        hole_layers=["HOLES"],
        use_hatch=False,
        number_layers=[],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )
    parts, _ = read_dxf_with_profile(path, profile)
    assert len(parts) == 2
    assert sum(len(part["holes"]) for part in parts) == 1


@pytest.mark.parametrize("build_hierarchy", [True, False])
def test_read_dxf_with_profile_recovers_when_layer_hints_are_wrong(
    tmp_path,
    build_hierarchy,
):
    path = tmp_path / "renamed_layers.dxf"
    doc = ezdxf.new("R2010")
    for layer in ("CUSTOM_A", "CUSTOM_B", "CUSTOM_C", "LABELS"):
        doc.layers.add(layer)
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (1000, 0), (1000, 600), (0, 600), (0, 0)],
        dxfattribs={"layer": "CUSTOM_A"},
    )
    msp.add_lwpolyline(
        [(1200, 0), (2200, 0), (2200, 600), (1200, 600), (1200, 0)],
        dxfattribs={"layer": "CUSTOM_B"},
    )
    msp.add_circle((500, 300), radius=80, dxfattribs={"layer": "CUSTOM_C"})
    first = msp.add_text("PANEL-1", dxfattribs={"layer": "LABELS"})
    first.set_placement((500, 300))
    second = msp.add_text("PANEL-2", dxfattribs={"layer": "LABELS"})
    second.set_placement((1700, 300))
    doc.saveas(path)

    profile = DrawingProfile(
        name="renamed_layers",
        version="1.0.0",
        panel_layers=["OLD_PANEL_LAYER"],
        hole_layers=["OLD_HOLE_LAYER"],
        number_layers=["OLD_LABELS_LAYER"],
        label_pattern=r"^PANEL-\d+$",
        use_hatch=False,
        build_hierarchy=build_hierarchy,
        exclude_entity_types=[],
        exclude_linetypes=[],
        small_area_threshold=100.0,
    )

    parts, _ = read_dxf_with_profile(path, profile)

    assert len(parts) == 2
    assert sum(len(part["holes"]) for part in parts) == 1
    assert {part["original_number"] for part in parts} == {"PANEL-1", "PANEL-2"}

def test_read_dxf_with_profile_grid_mode_reconstructs_rectangular_cells(tmp_path):
    path = tmp_path / "grid_panels.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("GRID")
    doc.layers.add("NUM")
    msp = doc.modelspace()

    for x in (0, 100, 200):
        msp.add_lwpolyline(
            [(x, 0), (x, 200)],
            dxfattribs={"layer": "GRID"},
        )
    for y in (0, 100, 200):
        msp.add_lwpolyline(
            [(0, y), (200, y)],
            dxfattribs={"layer": "GRID"},
        )

    labels = {
        (50, 50): "1",
        (150, 50): "2",
        (50, 150): "3",
        (150, 150): "4",
    }
    for point, label in labels.items():
        text = msp.add_text(label, dxfattribs={"layer": "NUM"})
        text.set_placement(point)

    doc.saveas(path)

    profile = DrawingProfile(
        name="grid_test",
        version="1.0.0",
        panel_layers=["GRID"],
        use_hatch=False,
        number_layers=["NUM"],
        label_pattern=r"^(?P<part>\d+)$",
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
        grid_mode=True,
        grid_layer="GRID",
        grid_min_line_length=50.0,
        grid_min_area=5000.0,
        grid_max_area=40000.0,
        grid_label_margin=200.0,
    )

    parts, _ = read_dxf_with_profile(path, profile)
    assert len(parts) == 4
    assert {part["original_number"] for part in parts} == {"1", "2", "3", "4"}
    assert all(part["area"] == 10000 for part in parts)


def test_audit_drawing_grid_mode_ignores_open_grid_segments(tmp_path):
    path = tmp_path / "grid_audit.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("GRID")
    doc.layers.add("NUM")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (0, 200)], dxfattribs={"layer": "GRID"})
    msp.add_lwpolyline([(100, 0), (100, 200)], dxfattribs={"layer": "GRID"})
    msp.add_lwpolyline([(0, 0), (100, 0)], dxfattribs={"layer": "GRID"})
    msp.add_lwpolyline([(0, 200), (100, 200)], dxfattribs={"layer": "GRID"})
    text = msp.add_text("1", dxfattribs={"layer": "NUM"})
    text.set_placement((50, 100))
    doc.saveas(path)

    profile = DrawingProfile(
        name="grid_audit_test",
        version="1.0.0",
        panel_layers=["GRID"],
        use_hatch=False,
        number_layers=["NUM"],
        label_pattern=r"^(?P<part>\d+)$",
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
        grid_mode=True,
        grid_layer="GRID",
        grid_min_line_length=50.0,
        grid_min_area=5000.0,
        grid_max_area=40000.0,
        grid_label_margin=200.0,
    )

    issues = audit_drawing(path, profile)
    assert not any(
        issue.type in {"unclosed_geometry", "invalid_geometry", "open_chain"}
        for issue in issues
    )


def test_read_dxf_with_profile_grid_mode_splits_cell_into_labeled_parts(tmp_path):
    path = tmp_path / "grid_split_panel.dxf"
    doc = ezdxf.new("R2010")
    for layer in ("GRID", "SPLIT", "NUM"):
        doc.layers.add(layer)
    msp = doc.modelspace()

    msp.add_lwpolyline([(0, 0), (0, 100)], dxfattribs={"layer": "GRID"})
    msp.add_lwpolyline([(100, 0), (100, 100)], dxfattribs={"layer": "GRID"})
    msp.add_lwpolyline([(0, 0), (100, 0)], dxfattribs={"layer": "GRID"})
    msp.add_lwpolyline([(0, 100), (100, 100)], dxfattribs={"layer": "GRID"})

    msp.add_lwpolyline(
        [(0, 0), (100, 0), (100, 100), (0, 0)],
        dxfattribs={"layer": "SPLIT"},
        format="xy",
        close=True,
    )
    msp.add_lwpolyline(
        [(0, 0), (100, 100), (0, 100), (0, 0)],
        dxfattribs={"layer": "SPLIT"},
        format="xy",
        close=True,
    )

    text = msp.add_text("1", dxfattribs={"layer": "NUM"})
    text.set_placement((50, 50))
    doc.saveas(path)

    profile = DrawingProfile(
        name="grid_split_test",
        version="1.0.0",
        panel_layers=["GRID"],
        use_hatch=False,
        number_layers=["NUM"],
        label_pattern=r"^(?P<part>\d+)$",
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
        grid_mode=True,
        grid_layer="GRID",
        grid_split_layers=["SPLIT"],
        grid_min_line_length=50.0,
        grid_min_area=1000.0,
        grid_max_area=40000.0,
        grid_label_margin=200.0,
    )

    parts, _ = read_dxf_with_profile(path, profile)
    assert len(parts) == 2
    assert {part["original_number"] for part in parts} == {"1a", "1b"}
    assert all(part["area"] == 5000 for part in parts)


def test_read_dxf_with_profile_network_mode_filters_gap_and_keeps_curve_split(tmp_path):
    path = tmp_path / "line_network_panel.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("NET")
    msp = doc.modelspace()

    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "NET"})
    msp.add_line((100, 0), (100, 100), dxfattribs={"layer": "NET"})
    msp.add_line((100, 100), (0, 100), dxfattribs={"layer": "NET"})
    msp.add_line((0, 100), (0, 0), dxfattribs={"layer": "NET"})
    msp.add_line((0, 0), (100, 100), dxfattribs={"layer": "NET"})

    msp.add_line((120, 0), (120, 100), dxfattribs={"layer": "NET"})
    msp.add_line((122, 0), (122, 100), dxfattribs={"layer": "NET"})
    msp.add_line((120, 0), (122, 0), dxfattribs={"layer": "NET"})
    msp.add_line((120, 100), (122, 100), dxfattribs={"layer": "NET"})
    doc.saveas(path)

    profile = DrawingProfile(
        name="network_test",
        version="1.0.0",
        panel_layers=["NET"],
        use_hatch=False,
        number_layers=[],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
        network_mode=True,
        network_layers=["NET"],
        network_min_area=10.0,
        network_max_area=100000.0,
        network_gap_side_tolerance=3.0,
        network_arc_flattening=1.0,
    )

    parts, _ = read_dxf_with_profile(path, profile)
    assert len(parts) == 2
    assert all(part["area"] == 5000 for part in parts)
    assert all(part["original_number"] is None for part in parts)


def _hierarchy_profile(**overrides) -> DrawingProfile:
    base = dict(
        name="hierarchy_holes",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=True,
        exclude_entity_types=[],
        exclude_linetypes=[],
        closed_tolerance=0.01,
    )
    base.update(overrides)
    return DrawingProfile(**base)


def test_nested_polyline_hole_is_recorded_not_dropped(tmp_path):
    """外围轮廓内的闭合多段线孔洞必须进 holes，不能被静默丢弃。

    回归 CGR45：153 个画成闭合多段线的盆孔曾因 hierarchy 子级索引取错
    （拿 accepted_candidates 去索引按面积排序的 child 序号）、又用分类器
    role == "hole" 二次过滤而整体丢失。
    """
    path = tmp_path / "polyline_hole.dxf"

    def entities(msp):
        msp.add_lwpolyline(
            [(0, 0), (400, 0), (400, 300), (0, 300)],
            close=True,
            dxfattribs={"layer": "PANEL"},
        )
        msp.add_lwpolyline(
            [(100, 100), (200, 100), (200, 180), (100, 180)],
            close=True,
            dxfattribs={"layer": "PANEL"},
        )
        msp.add_circle((320, 60), radius=20, dxfattribs={"layer": "PANEL"})

    _write_dxf(path, entities)

    parts, _ = read_dxf_with_profile(path, _hierarchy_profile())

    assert len(parts) == 1
    holes = parts[0]["holes"]
    assert len(holes) == 2, "圆形孔与多段线孔都必须记录"
    assert len(parts[0]["hole_handles"]) == 2
    outer_area = parts[0]["outer_polygon"].area
    assert parts[0]["polygon"].area == pytest.approx(
        outer_area - sum(hole.area for hole in holes), rel=1e-6
    )


def test_nested_contours_are_conserved_between_panels_and_holes(tmp_path):
    """封闭候选的守恒不变量：每个候选要么是面板、要么是某个面板的孔洞。"""
    path = tmp_path / "conservation.dxf"

    def entities(msp):
        for col in range(3):
            x = col * 600
            msp.add_lwpolyline(
                [(x, 0), (x + 400, 0), (x + 400, 300), (x, 300)],
                close=True,
                dxfattribs={"layer": "PANEL"},
            )
            msp.add_lwpolyline(
                [(x + 50, 50), (x + 150, 50), (x + 150, 150), (x + 50, 150)],
                close=True,
                dxfattribs={"layer": "PANEL"},
            )
            msp.add_circle((x + 320, 60), radius=20, dxfattribs={"layer": "PANEL"})

    _write_dxf(path, entities)

    parts, _ = read_dxf_with_profile(path, _hierarchy_profile())

    closed_candidates = 9  # 3 面板 + 3 多段线孔 + 3 圆孔
    assert len(parts) == 3
    assert len(parts) + sum(len(p["holes"]) for p in parts) == closed_candidates


CGR45_FIXTURE = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "dxf" / "CGR45final.dxf"
)


@pytest.mark.skipif(
    not CGR45_FIXTURE.exists(),
    reason="需要 tests/fixtures/dxf/CGR45final.dxf 金标准样本",
)
def test_cgr45_golden_counts_match_profile_expectations():
    """CGR45 金标准：识别数与画像期望一致（203 面板 / 306 孔洞 / 203 编号）。"""
    profile = load_profile(
        Path(__file__).resolve().parents[1] / "drawing_profiles" / "cgr45.json"
    )
    parts, _ = read_dxf_with_profile(CGR45_FIXTURE, profile)

    assert len(parts) == profile.expected_counts["panels"]
    assert sum(len(p["holes"]) for p in parts) == profile.expected_counts["holes"]
    assert sum(1 for p in parts if p.get("original_number")) == (
        profile.expected_counts["numbers"]
    )


def _write_room_plan_dxf(path: Path) -> None:
    """两张"房间"（大轮廓 + 4 位房号）各含 2 件规格板，全部在图层 0。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    def rect(x0, y0, x1, y1):
        msp.add_lwpolyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)],
            dxfattribs={"layer": "0"},
        )

    # 房间 A：4000x3000 轮廓 + 内部两件 600x500
    rect(0, 0, 4000, 3000)
    rect(100, 100, 700, 600)
    rect(1000, 100, 1600, 600)
    # 房间 B：4000x3000 轮廓 + 内部两件
    rect(5000, 0, 9000, 3000)
    rect(5100, 100, 5700, 600)
    rect(6000, 100, 6600, 600)
    for text, point in (("1001", (2000, 1500)), ("1002", (7000, 1500))):
        entity = msp.add_text(text, dxfattribs={"layer": "0"})
        entity.set_placement(point)
    doc.saveas(path)


def _room_plan_profile(region_min_side_mm: float) -> DrawingProfile:
    return DrawingProfile(
        name="room_plan",
        version="1.0.0",
        panel_layer="0",
        panel_layers=["0"],
        use_hatch=False,
        number_layers=["0"],
        label_pattern=r"^(?P<unit>\d{4})$",
        assignment_mode="containing_region",
        room_number_format="{unit}-{index:02d}",
        region_min_side_mm=region_min_side_mm,
        build_hierarchy=False,
        closed_tolerance=0.1,
        exclude_linetypes=[],
        exclude_entity_types=[],
    )


def test_region_outlines_are_excluded_from_panels(tmp_path):
    """最小边达阈值的轮廓按"区域"排除，只留房间内的规格板。"""
    dxf_path = tmp_path / "room_plan.dxf"
    _write_room_plan_dxf(dxf_path)

    panels, _ = read_dxf_with_profile(
        dxf_path,
        _room_plan_profile(region_min_side_mm=1800.0),
    )
    without_region_rule, _ = read_dxf_with_profile(
        dxf_path,
        _room_plan_profile(region_min_side_mm=0.0),
    )

    assert len(panels) == 4          # 两个房间轮廓被排除
    assert len(without_region_rule) == 6  # 未启用区域规则时 6 个都是面板


def test_containing_region_numbers_panels_by_room(tmp_path):
    """编号 = 所在房间的房号 + 组内序号，且跨房间不串号。"""
    dxf_path = tmp_path / "room_plan.dxf"
    _write_room_plan_dxf(dxf_path)

    panels, _ = read_dxf_with_profile(
        dxf_path,
        _room_plan_profile(region_min_side_mm=1800.0),
    )
    numbers = {p["original_number"] for p in panels}

    assert numbers == {"1001-01", "1001-02", "1002-01", "1002-02"}
    # 房间 A（1001）内的板必须拿 1001-*，房间 B 同理
    by_number = {
        p["original_number"]: p["centroid"] for p in panels
    }
    assert by_number["1001-01"][0] < 4000
    assert by_number["1001-02"][0] < 4000
    assert by_number["1002-01"][0] > 4000
    assert by_number["1002-02"][0] > 4000
    # 序号按 x 递增
    assert by_number["1001-01"][0] < by_number["1001-02"][0]


def test_containing_region_assignment_is_deterministic():
    """同一批面板重复分配必须给出同样编号（集合遍历顺序不得影响结果）。"""
    from shapely.geometry import Polygon as ShapelyPolygon

    def panel(index, x, y):
        polygon = ShapelyPolygon([
            (x, y), (x + 600, y), (x + 600, y + 500), (x, y + 500),
        ])
        return {
            "index": index,
            "polygon": polygon,
            "area": polygon.area,
            "centroid": polygon.centroid,
        }

    def region(index, x, y, label):
        polygon = ShapelyPolygon([
            (x, y), (x + 4000, y), (x + 4000, y + 3000), (x, y + 3000),
        ])
        return {
            "index": index,
            "polygon": polygon,
            "area": polygon.area,
            "centroid": polygon.centroid,
            "label": label,
        }

    profile = _room_plan_profile(region_min_side_mm=1800.0)
    panels = [panel(0, 1000, 100), panel(1, 100, 100), panel(2, 5200, 100)]
    regions = [region(0, 0, 0, "1001"), region(1, 5000, 0, "1002")]
    texts = [
        {"text": "1001", "point": (2000.0, 1500.0)},
        {"text": "1002", "point": (7000.0, 1500.0)},
    ]

    forward = _assign_numbers_by_containing_region(panels, regions, texts, profile)
    reversed_inputs = _assign_numbers_by_containing_region(
        list(reversed(panels)),
        list(reversed(regions)),
        list(reversed(texts)),
        profile,
    )

    assert forward == reversed_inputs
    assert forward[1] == "1001-01"
    assert forward[0] == "1001-02"
    assert forward[2] == "1002-01"
