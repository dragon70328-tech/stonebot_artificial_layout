import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.constraints import (
    PROCESSING_CLASS_BRIDGE,
    PROCESSING_CLASS_WATERJET_LASER,
    PROFILE_LASER,
    PROFILE_MIN_SHEETS,
    PROFILE_WATERJET,
)
import main as app


def test_bridge_profiles_use_original_engine():
    assert PROFILE_MIN_SHEETS.processing_class == PROCESSING_CLASS_BRIDGE
    assert PROFILE_MIN_SHEETS.uses_deepnest is False


def test_waterjet_profile_routes_to_deepnest_class():
    assert PROFILE_WATERJET.processing_class == PROCESSING_CLASS_WATERJET_LASER
    assert PROFILE_WATERJET.uses_deepnest is True
    assert PROFILE_WATERJET.rotation == [0, 90, 180, 270]
    assert PROFILE_WATERJET.arbitrary_rotation is True
    assert PROFILE_WATERJET.slide_to_edge is False
    assert PROFILE_WATERJET.align_edges is False


def test_laser_profile_shares_deepnest_rules():
    assert PROFILE_LASER.processing_class == PROCESSING_CLASS_WATERJET_LASER
    assert PROFILE_LASER.uses_deepnest is True
    assert PROFILE_LASER.arbitrary_rotation is True


def test_resolve_processing_class_maps_cli_values():
    assert app.resolve_processing_class("bridge") == PROCESSING_CLASS_BRIDGE
    assert app.resolve_processing_class("waterjet") == PROCESSING_CLASS_WATERJET_LASER
    assert app.resolve_processing_class("laser") == PROCESSING_CLASS_WATERJET_LASER
    assert app.resolve_processing_class(None) is None


def test_default_profile_for_process_returns_waterjet_and_laser():
    assert app.default_profile_for_process("waterjet") is PROFILE_WATERJET
    assert app.default_profile_for_process("laser") is PROFILE_LASER
    assert app.default_profile_for_process("bridge") is None


def test_profile_can_override_processing_class():
    profile = PROFILE_MIN_SHEETS.with_overrides(
        processing_class=PROCESSING_CLASS_WATERJET_LASER
    )
    assert profile.uses_deepnest is True
