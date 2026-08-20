from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import Point

from src.drawing_profile import (
    DrawingProfile,
    DrawingIssue,
    _assign_numbers_by_panel_layer,
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
