from pathlib import Path

from shapely.geometry import Polygon

from src.recognized_contract import parts_data_to_recognized_drawing


def test_parts_data_to_recognized_drawing_builds_panels_and_holes(tmp_path):
    source = tmp_path / "sample.dxf"
    source.write_bytes(b"DXF-PLACEHOLDER")

    outer = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    hole = Polygon([(40, 40), (60, 40), (60, 60), (40, 60)])
    combined = outer.difference(hole)

    drawing = parts_data_to_recognized_drawing(
        [
            {
                "index": 0,
                "polygon": combined,
                "outer_polygon": outer,
                "holes": [hole],
                "hole_handles": ["H1"],
                "outer_handle": "P1",
                "original_number": "01B-1",
                "layer": "PANEL",
                "area": combined.area,
                "confidence": 0.9,
                "source": "closed_polygon",
            }
        ],
        source,
        profile_name="test_profile",
    )

    assert drawing.profile_name == "test_profile"
    assert len(drawing.panels) == 1
    assert len(drawing.panels[0].holes) == 1
    assert drawing.panels[0].number == "01B-1"
    assert drawing.panels[0].holes[0].entity_handle == "H1"
    assert drawing.input_digest is not None
    assert drawing.output_digest is not None

