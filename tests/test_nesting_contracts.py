import pytest
from pydantic import ValidationError

from src.contracts import ConfirmedDrawing, RecognizedPanel
from src.nesting_contracts import (
    NestingRequest,
    OversizeMode,
    ProcessPolicy,
    SheetSpec,
    SharedEdgeMode,
)


def _confirmed() -> ConfirmedDrawing:
    return ConfirmedDrawing(
        confirmed_id="confirm-1",
        drawing_id="drawing-1",
        revision_id="revision-1",
        panels=[
            RecognizedPanel(
                panel_id="panel-1",
                geometry_wkt="POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))",
                number="P-0001",
            )
        ],
    )


def test_sheet_spec_total_oversize_affects_effective_dimensions():
    sheet = SheetSpec(
        nominal_width_mm=3200,
        nominal_height_mm=1800,
        usable_width_mm=3150,
        usable_height_mm=1750,
        thickness_mm=20,
        oversize_enabled=True,
        oversize_mode=OversizeMode.TOTAL,
        oversize_width_mm=30,
        oversize_height_mm=20,
    )

    assert sheet.effective_width_mm == 3180
    assert sheet.effective_height_mm == 1770


def test_sheet_spec_per_side_oversize_applies_to_both_edges():
    sheet = SheetSpec(
        nominal_width_mm=3200,
        nominal_height_mm=1800,
        usable_width_mm=3150,
        usable_height_mm=1750,
        oversize_enabled=True,
        oversize_mode=OversizeMode.PER_SIDE,
        oversize_width_mm=30,
        oversize_height_mm=20,
    )

    assert sheet.oversize_added_width_mm == 60
    assert sheet.oversize_added_height_mm == 40
    assert sheet.effective_width_mm == 3210
    assert sheet.effective_height_mm == 1790


def test_sheet_spec_disabled_oversize_is_ignored():
    sheet = SheetSpec(
        nominal_width_mm=3200,
        nominal_height_mm=1800,
        usable_width_mm=3200,
        usable_height_mm=1800,
        oversize_enabled=False,
        oversize_width_mm=30,
        oversize_height_mm=30,
    )

    assert sheet.effective_width_mm == 3200
    assert sheet.effective_height_mm == 1800


def test_bridge_policy_rejects_non_quarter_turn_rotation():
    with pytest.raises(ValidationError):
        ProcessPolicy.bridge(
            kerf_mm=5,
            rotation_degrees=[0, 45, 90],
        )


def test_bridge_policy_reports_missing_kerf_and_request_rejects_it():
    policy = ProcessPolicy.bridge(kerf_mm=0)

    assert policy.is_ready is False
    assert "kerf_mm" in policy.validation_errors()[0]

    with pytest.raises(ValidationError):
        NestingRequest(
            request_id="request-1",
            confirmed_drawing=_confirmed(),
            sheet=SheetSpec.from_legacy(3200, 1800, 20),
            process=policy,
        )


def test_waterjet_policy_allows_arbitrary_contour_sharing():
    policy = ProcessPolicy.waterjet(min_gap_mm=0)

    assert policy.arbitrary_rotation is True
    assert policy.shared_edge_mode == SharedEdgeMode.ANY_CONTOUR
    assert policy.is_ready is True


def test_nesting_request_digest_changes_with_process_parameters():
    sheet = SheetSpec.from_legacy(3200, 1800, 20)
    common = {
        "request_id": "request-1",
        "confirmed_drawing": _confirmed(),
        "sheet": sheet,
        "process": ProcessPolicy.bridge(kerf_mm=5),
    }

    first = NestingRequest(**common)
    second = NestingRequest(
        **{
            **common,
            "process": ProcessPolicy.bridge(kerf_mm=6),
        }
    )

    assert first.digest() != second.digest()
