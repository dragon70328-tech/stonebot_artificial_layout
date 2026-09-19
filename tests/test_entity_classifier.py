from shapely.geometry import Polygon

from src.entity_classifier import classify_candidates


def _candidate(polygon, *, layer, entity_type="LWPOLYLINE", handle="H"):
    return {
        "handle": handle,
        "layer": layer,
        "linetype": "CONTINUOUS",
        "entity_type": entity_type,
        "polygon": polygon,
    }


def test_classifier_uses_geometry_and_topology_with_unknown_layers():
    outer = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    inner = Polygon([(20, 20), (35, 20), (35, 35), (20, 35)])
    circle = Polygon([(10, 10), (20, 10), (20, 20), (10, 20)])
    candidates = [
        _candidate(outer, layer="RANDOM_A", handle="H1"),
        _candidate(inner, layer="RANDOM_B", handle="H2"),
        _candidate(circle, layer="RANDOM_C", entity_type="CIRCLE", handle="H3"),
    ]

    classified = classify_candidates(
        candidates,
        panel_layers=["MISSING_PANEL_LAYER"],
        hole_layers=["MISSING_HOLE_LAYER"],
    )
    roles = [item["classification"]["role"] for item in classified]

    assert roles == ["panel", "hole", "hole"]
    assert classified[1]["classification"]["features"]["contained_by"] == [0]


def test_classifier_role_is_stable_when_layer_names_are_renamed():
    outer = Polygon([(0, 0), (200, 0), (200, 100), (0, 100)])
    inner = Polygon([(50, 40), (80, 40), (80, 70), (50, 70)])
    original = [
        _candidate(outer, layer="PANEL", handle="H1"),
        _candidate(inner, layer="HOLES", entity_type="CIRCLE", handle="H2"),
    ]
    renamed = [
        _candidate(outer, layer="0", handle="H1"),
        _candidate(inner, layer="0", entity_type="CIRCLE", handle="H2"),
    ]

    matching = classify_candidates(
        original,
        panel_layers=["PANEL"],
        hole_layers=["HOLES"],
    )
    unknown = classify_candidates(renamed)

    assert [
        item["classification"]["role"] for item in matching
    ] == [
        item["classification"]["role"] for item in unknown
    ] == ["panel", "hole"]


def test_number_text_inside_outline_adds_panel_evidence():
    with_text = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    without_text = Polygon([(200, 0), (300, 0), (300, 100), (200, 100)])
    text_box = Polygon([(45, 45), (55, 45), (55, 55), (45, 55)])

    classified = classify_candidates(
        [
            _candidate(with_text, layer="0", handle="H1"),
            _candidate(without_text, layer="0", handle="H2"),
        ],
        label_boxes=[text_box],
    )

    assert classified[0]["classification"]["features"]["has_number_text"] is True
    assert classified[1]["classification"]["features"]["has_number_text"] is False
    assert (
        classified[0]["classification"]["panel_score"]
        > classified[1]["classification"]["panel_score"]
    )


def test_panel_layer_hint_does_not_turn_circle_into_panel():
    circle = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    classified = classify_candidates(
        [_candidate(circle, layer="PANEL", entity_type="CIRCLE")],
        panel_layers=["PANEL"],
    )

    assert classified[0]["classification"]["role"] == "hole"
