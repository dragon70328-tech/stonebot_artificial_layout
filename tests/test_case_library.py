import json
from pathlib import Path

from src.case_library import (
    CaseRecord,
    case_similarity,
    load_cases,
    load_case,
    match_case,
    overlay_case_onto_profile,
)
from src.drawing_profile import DrawingProfile


def _fingerprint(layers=("0", "编号"), entities=("LWPOLYLINE", "TEXT"), has_circle=False):
    return {
        "layer_counts": {layer: 1 for layer in layers},
        "entity_counts": {entity: 1 for entity in entities},
        "linetype_counts": {"BYLAYER": 1},
        "has_circle": has_circle,
        "has_lwpolyline": "LWPOLYLINE" in entities,
        "has_line": False,
        "dxf_version": "AC1032",
        "insunits": 4,
    }


def _case(case_id, profile_name, fingerprint):
    return CaseRecord(
        case_id=case_id,
        profile_name=profile_name,
        fingerprint=fingerprint,
        expected_counts={"panels": 1, "holes": 0, "numbers": 1},
        audit_ignore_rules=[{"issue_type": "open_chain", "action": "ignore"}],
    )


def test_case_similarity_is_full_for_identical_fingerprint():
    fingerprint = _fingerprint()
    case = _case("c1", "p1", fingerprint)
    assert case_similarity(fingerprint, case) == 100.0


def test_match_case_prefers_more_similar_case():
    target = _fingerprint(layers=("0", "编号"), entities=("LWPOLYLINE", "CIRCLE"), has_circle=True)
    circle_case = _case("circle", "circle_profile", target)
    flat_case = _case(
        "flat",
        "flat_profile",
        _fingerprint(layers=("0", "编号"), entities=("LWPOLYLINE",), has_circle=False),
    )
    best, score = match_case(target, [flat_case, circle_case], min_score=75.0)
    assert best is circle_case
    assert score >= 75.0


def test_match_case_returns_none_below_threshold():
    target = _fingerprint(layers=("A", "B"), entities=("LINE",))
    case = _case("c1", "p1", _fingerprint(layers=("X", "Y"), entities=("TEXT",)))
    assert match_case(target, [case], min_score=99.0) is None


def test_overlay_case_onto_profile_merges_rules_and_counts():
    profile = DrawingProfile(
        name="p1",
        version="1.0.0",
        panel_layer="0",
        use_hatch=False,
        number_layers=["编号"],
        build_hierarchy=False,
        audit_ignore_rules=[{"issue_type": "unclosed_geometry", "action": "ignore"}],
    )
    case = _case("c1", "p1", _fingerprint())
    enriched = overlay_case_onto_profile(profile, case)
    assert enriched.expected_counts == case.expected_counts
    assert {"issue_type": "open_chain", "action": "ignore"} in enriched.audit_ignore_rules


def test_load_and_save_case_round_trip(tmp_path):
    case = _case("c1", "p1", _fingerprint())
    path = tmp_path / "c1.json"
    path.write_text(json.dumps(case.to_dict(), ensure_ascii=False), encoding="utf-8")
    loaded = load_case(path)
    assert loaded.case_id == "c1"
    assert loaded.profile_name == "p1"
    assert loaded.fingerprint["dxf_version"] == "AC1032"
