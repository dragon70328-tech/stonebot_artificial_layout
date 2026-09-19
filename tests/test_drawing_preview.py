import json
from pathlib import Path

from shapely.geometry import Polygon

from src.drawing_preview import build_drawing_preview
from src.job_context import JobContext
from src.models import Part
from src.units import UnitSystem


def _context(tmp_path: Path) -> JobContext:
    outer = Polygon([(100, 200), (500, 200), (500, 600), (100, 600)])
    hole = Polygon([(200, 300), (260, 300), (260, 360), (200, 360)])
    part = Part(
        id=7,
        number="01B-7",
        polygon=outer.difference(hole),
        outer_polygon=outer,
        holes=[hole],
        original_number="01B-7",
        material_group="01B",
        outer_handle="A1",
        hole_handles=["A2"],
        label_position=(300.0, 400.0),
    )
    return JobContext(
        dxf_path=str(tmp_path / "sample.dxf"),
        unit=UnitSystem.METRIC,
        parts=[part],
        closed_entities=[
            {
                "handle": "B1",
                "entity_type": "LWPOLYLINE",
                "layer": "PANEL",
                "linetype": "CONTINUOUS",
                "polygon": Polygon(
                    [(700, 200), (800, 200), (800, 300), (700, 300)]
                ),
            }
        ],
    )


def test_build_drawing_preview_exposes_clickable_targets(tmp_path):
    payload = build_drawing_preview(_context(tmp_path))

    assert payload["revision"] == 0
    assert payload["counts"] == {
        "panels": 1,
        "holes": 1,
        "unrecognized": 1,
        "entities": 3,
    }
    panel = next(item for item in payload["entities"] if item["kind"] == "panel")
    hole = next(item for item in payload["entities"] if item["kind"] == "hole")
    unrecognized = next(
        item for item in payload["entities"] if item["kind"] == "unrecognized"
    )

    assert panel["target_id"] == "7"
    assert panel["points"][0] == [0.0, 400.0]
    assert hole["target_id"] == "handle:A2"
    assert hole["parent_target_id"] == "7"
    assert unrecognized["target_id"] == "handle:B1"
    assert payload["view_box"][2] > 700
    assert payload["view_box"][3] > 400


def test_build_drawing_preview_handles_empty_context(tmp_path):
    payload = build_drawing_preview(JobContext(dxf_path=str(tmp_path / "empty.dxf")))

    assert payload["entities"] == []
    assert payload["counts"]["entities"] == 0
    assert payload["view_box"] == [0.0, 0.0, 1.0, 1.0]
