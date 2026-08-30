from pathlib import Path

import ezdxf
from shapely.geometry import box

from src.drawing_profile import DrawingProfile
from src.models import Part
from src.workflow import assign_group_ids, prepare_drawing


def _part(number: str) -> Part:
    polygon = box(0.0, 0.0, 100.0, 50.0)
    return Part(
        id=abs(hash(number)) % 1_000_000,
        number=number,
        polygon=polygon,
        outer_polygon=polygon,
        holes=[],
        original_number=number,
        area=polygon.area,
        label_position=(50.0, 25.0),
    )


def _write_profile_dxf(path: Path) -> None:
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("NUM")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
        dxfattribs={"layer": "PANEL"},
    )
    text = msp.add_text("01B-1", dxfattribs={"layer": "NUM"})
    text.set_placement((50, 50))
    doc.saveas(path)


def test_prepare_drawing_returns_parts_and_contract(tmp_path):
    path = tmp_path / "simple.dxf"
    _write_profile_dxf(path)
    profile = DrawingProfile(
        name="integration_test",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )

    prepared = prepare_drawing(
        str(path),
        drawing_profile=profile,
        skip_unnumbered=False,
    )

    assert prepared.error is None
    assert prepared.parts
    assert prepared.recognized_drawing is not None
    assert prepared.groups == [None]
    assert prepared.material_group_enabled is False


def test_prepare_drawing_reports_empty_drawing(tmp_path):
    path = tmp_path / "empty.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.saveas(path)

    prepared = prepare_drawing(str(path), drawing_profile=None)

    assert prepared.error == "未找到任何封闭图形。"
    assert prepared.parts == []


def test_assign_group_ids_uses_configured_pattern():
    profile = DrawingProfile(
        name="test",
        version="1.0.0",
        group_id_pattern=r"^(?P<group>\d{2}B)",
    )
    parts = [_part("01B-1"), _part("01B-2"), _part("P-1")]

    assign_group_ids(parts, profile, "one_set_per_sheet")

    assert parts[0].group_id == "01B"
    assert parts[1].group_id == "01B"
    assert parts[2].group_id is not None


def test_assign_group_ids_falls_back_to_number_prefix():
    parts = [_part("A-1"), _part("A-2"), _part("B-1")]

    assign_group_ids(parts, None, "one_set_per_sheet")

    assert [part.group_id for part in parts] == ["A", "A", "B"]
