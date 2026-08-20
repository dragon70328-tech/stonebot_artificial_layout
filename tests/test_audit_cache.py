from src.audit_cache import (
    audit_cache_key,
    load_cached_issues,
    profile_digest,
    save_cached_issues,
)
from src.drawing_profile import DrawingIssue, DrawingProfile


def _profile(**updates):
    data = {
        "name": "test",
        "version": "1.0.0",
        "panel_layer": "0",
        "use_hatch": False,
        "number_layers": ["编号"],
        "build_hierarchy": True,
    }
    data.update(updates)
    return DrawingProfile(**data)


def test_profile_digest_changes_when_rules_change():
    left = _profile()
    right = _profile(
        audit_ignore_rules=[{"issue_type": "open_chain", "action": "ignore"}]
    )
    assert profile_digest(left) != profile_digest(right)


def test_audit_cache_key_changes_with_profile():
    file_sha256 = "a" * 64
    left = audit_cache_key(file_sha256, _profile())
    right = audit_cache_key(
        file_sha256,
        _profile(expected_counts={"panels": 1, "holes": 0, "numbers": 1}),
    )
    assert left != right


def test_save_and_load_cached_issues_round_trip(tmp_path):
    issues = [
        DrawingIssue(
            issue_id=1,
            severity="warning",
            type="open_chain",
            entity_handle="ABC",
            layer="0",
            coordinates=(1.0, 2.0),
            message="open",
            suggestion="fix",
            entity_type="LINE",
            metadata={"vertex_count": 2},
        )
    ]
    path = save_cached_issues(tmp_path, "key", issues)
    loaded = load_cached_issues(tmp_path, "key")
    assert loaded is not None
    assert loaded[0].issue_id == 1
    assert loaded[0].type == "open_chain"
    assert loaded[0].metadata["vertex_count"] == 2

