"""INSERT block expansion coverage for generic DXF recognition."""

from __future__ import annotations

import ezdxf
import pytest

from src.drawing_profile import DrawingProfile, read_dxf_with_profile
from src.dxf_reader import iter_expanded_entities, read_dxf
from src.dxf_writer import write_numbered_parts_dxf
from src.numbering import assign_numbers


PROFILE = DrawingProfile.from_dict(
    {
        "name": "insert_block_probe",
        "version": "1.0.0",
        "panel_layers": ["PANEL"],
        "hole_layers": ["PANEL"],
        "number_layers": ["NUMBERS"],
        "label_pattern": r"^A-\d+$",
        "use_hatch": False,
        "build_hierarchy": True,
        "expand_insert_blocks": True,
        "network_mode": False,
        "exclude_linetypes": [],
    }
)


def _add_part_block(doc, name: str = "PART_BLOCK") -> None:
    block = doc.blocks.new(name)
    panel = block.add_lwpolyline(
        [(0, 0), (200, 0), (200, 100), (0, 100)],
        close=True,
        dxfattribs={"layer": "0", "color": 0},
    )
    panel.closed = True
    block.add_circle(
        (50, 50),
        10,
        dxfattribs={"layer": "0", "color": 0},
    )
    label = block.add_text(
        "A-1",
        dxfattribs={"layer": "NUMBERS", "color": 0, "height": 12},
    )
    label.set_placement((100, 50))


def _save(doc, path) -> None:
    doc.header["$INSUNITS"] = 4
    doc.saveas(path)


def test_nested_insert_is_recognised_and_numbered(tmp_path):
    source = tmp_path / "nested_insert.dxf"
    doc = ezdxf.new("R2010")
    _add_part_block(doc)
    wrapper = doc.blocks.new("WRAPPER")
    wrapper.add_blockref(
        "PART_BLOCK",
        (0, 0),
        dxfattribs={
            "layer": "0",
            "rotation": 0,
            "xscale": 2,
            "yscale": 1,
        },
    )
    doc.modelspace().add_blockref(
        "WRAPPER",
        (1000, 500),
        dxfattribs={"layer": "PANEL", "rotation": 30},
    )
    _save(doc, source)

    parts_data, _ = read_dxf_with_profile(source, PROFILE)

    assert len(parts_data) == 1
    part = parts_data[0]
    assert part["original_number"] == "A-1"
    assert len(part["holes"]) == 1
    assert part["hole_shapes"][0]["type"] == "ellipse"
    assert part["area"] > 0

    parts = assign_numbers(parts_data, skip_unnumbered=False)
    output = tmp_path / "nested_insert_numbered.dxf"
    write_numbered_parts_dxf(parts, str(output), source_path=str(source))

    result = ezdxf.readfile(output)
    msp = result.modelspace()
    assert len(list(msp.query('INSERT[layer=="BLOCKS"]'))) == 1
    assert len(list(msp.query('*[layer=="OUTER"]'))) == 1
    assert len(list(msp.query('*[layer=="HOLES"]'))) == 1
    assert [entity.dxf.text for entity in msp.query('TEXT[layer=="NUMBERS"]')] == [
        "A-1"
    ]


def test_unprofiled_reader_expands_blocks_by_default(tmp_path):
    source = tmp_path / "unprofiled_block.dxf"
    doc = ezdxf.new("R2010")
    _add_part_block(doc)
    doc.modelspace().add_blockref(
        "PART_BLOCK",
        (200, 100),
        dxfattribs={"layer": "PANEL"},
    )
    _save(doc, source)

    parts_data, _ = read_dxf(
        str(source),
        panel_layers=["PANEL"],
        number_layers=["NUMBERS"],
        label_pattern=r"^A-\d+$",
    )

    assert len(parts_data) == 1
    assert parts_data[0]["original_number"] == "A-1"
    assert len(parts_data[0]["holes"]) == 1


def test_minsert_expands_every_array_instance(tmp_path):
    source = tmp_path / "minsert.dxf"
    doc = ezdxf.new("R2010")
    _add_part_block(doc)
    doc.modelspace().add_blockref(
        "PART_BLOCK",
        (0, 0),
        dxfattribs={
            "layer": "PANEL",
            "row_count": 2,
            "column_count": 2,
            "row_spacing": 300,
            "column_spacing": 250,
        },
    )
    _save(doc, source)

    parts_data, _ = read_dxf_with_profile(source, PROFILE)

    assert len(parts_data) == 4
    assert len({part["outer_handle"] for part in parts_data}) == 4
    assert all(part["original_number"] == "A-1" for part in parts_data)

    parts = assign_numbers(parts_data, skip_unnumbered=False)
    assert [part.number for part in parts] == ["A-1", "A-1-2", "A-1-3", "A-1-4"]


def test_block_cycle_fails_closed_instead_of_recursing():
    doc = ezdxf.new("R2010")
    first = doc.blocks.new("FIRST")
    first.add_blockref("SECOND", (0, 0))
    second = doc.blocks.new("SECOND")
    second.add_blockref("FIRST", (0, 0))
    doc.modelspace().add_blockref("FIRST", (0, 0))

    with pytest.raises(ValueError, match="cyclic DXF block reference"):
        list(iter_expanded_entities(doc))
