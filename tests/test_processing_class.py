import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import box

from src.constraints import (
    PROCESSING_CLASS_BRIDGE,
    PROCESSING_CLASS_WATERJET_LASER,
    PROFILE_LASER,
    PROFILE_MIN_SHEETS,
    PROFILE_WATERJET,
)
from src.models import NestingResult, Part, Sheet
from src.units import UnitSystem
import src.workflow as workflow
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


def _single_part_result(part: Part) -> NestingResult:
    sheet = Sheet(index=1, width=1000.0, height=1000.0,
                  thickness=20.0, parts=[part])
    return NestingResult(
        sheets=[sheet],
        unit=UnitSystem.METRIC.value,
        total_parts=1,
        total_sheets=1,
        total_part_area=part.area,
        total_sheet_area=sheet.total_area,
    )


def test_bridge_group_forwards_rect_fast_escape_hatch(monkeypatch):
    poly = box(0.0, 0.0, 100.0, 100.0)
    part = Part(
        id=1, number="P1", polygon=poly, outer_polygon=poly,
        holes=[], area=poly.area, label_position=(50.0, 50.0),
    )
    captured = {}

    def fake_nest_parts(*args, **kwargs):
        captured.update(kwargs)
        return _single_part_result(part)

    monkeypatch.setattr(workflow, "nest_parts", fake_nest_parts)
    workflow._nest_one_group(
        [part], 1000.0, 1000.0, None, None, 20.0,
        UnitSystem.METRIC, PROFILE_MIN_SHEETS, None,
        1, 0, 0.0, True,
        rect_fast=False,
    )

    assert captured["rect_fast"] is False


def test_deepnest_group_forwards_low_fill_controls(monkeypatch):
    poly = box(0.0, 0.0, 100.0, 100.0)
    part = Part(
        id=1, number="P1", polygon=poly, outer_polygon=poly,
        holes=[], area=poly.area, label_position=(50.0, 50.0),
    )
    captured = {}

    def fake_nest_deepnest(*args, **kwargs):
        captured.update(kwargs)
        return _single_part_result(part)

    monkeypatch.setattr(
        workflow, "nest_parts_deepnest", fake_nest_deepnest
    )
    workflow._nest_one_group(
        [part], 1000.0, 1000.0, None, None, 20.0,
        UnitSystem.METRIC, PROFILE_WATERJET, None,
        1, 0, 60.0, True,
        low_fill_rework=True,
        low_fill_threshold=0.45,
        candidate_mode="nfp_lite",
    )

    assert captured["low_fill_rework"] is True
    assert captured["low_fill_threshold"] == 0.45
    assert captured["candidate_mode"] == "nfp_lite"


def test_deepnest_rejects_low_fill_rework_with_pairing():
    poly = box(0.0, 0.0, 100.0, 100.0)
    part = Part(
        id=1, number="P1", polygon=poly, outer_polygon=poly,
        holes=[], area=poly.area, label_position=(50.0, 50.0),
    )

    with pytest.raises(ValueError, match="暂不与 --pairing"):
        workflow._nest_one_group(
            [part], 1000.0, 1000.0, None, None, 20.0,
            UnitSystem.METRIC, PROFILE_WATERJET, None,
            1, 0, 60.0, True,
            pairing=True,
            low_fill_rework=True,
        )
