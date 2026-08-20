from pathlib import Path

import ezdxf

from src.dxf_reader import (
    _collect_number_texts,
    _looks_like_number,
    extract_closed_polygons,
    read_dxf,
)


def _write_panel_dxf(path: Path, number_layers):
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    for layer in number_layers:
        if layer not in doc.layers:
            doc.layers.add(layer)
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 0)],
        dxfattribs={"layer": "PANEL"},
    )
    doc.saveas(path)
    return doc


def test_looks_like_number_filters_common_shapes():
    assert _looks_like_number("01B-7")
    assert _looks_like_number("523-C")
    assert not _looks_like_number("D1 bathroom")
    assert not _looks_like_number("kitchen")


def test_collect_number_texts_uses_explicit_layers(tmp_path):
    path = tmp_path / "explicit.dxf"
    doc = _write_panel_dxf(path, ["NUM_A", "NUM_B"])
    msp = doc.modelspace()
    text = msp.add_text("01B-7", dxfattribs={"layer": "NUM_A"})
    text.set_placement((500, 500))

    texts = _collect_number_texts(doc, number_layers=["NUM_A", "NUM_B"])
    assert texts == [(500.0, 500.0, "01B-7")]


def test_read_dxf_uses_multiple_number_layers_and_pattern(tmp_path):
    path = tmp_path / "numbered.dxf"
    doc = _write_panel_dxf(path, ["A", "B"])
    msp = doc.modelspace()
    good = msp.add_text("01B-7", dxfattribs={"layer": "A"})
    good.set_placement((500, 500))
    bad = msp.add_text("ST-ignore", dxfattribs={"layer": "A"})
    bad.set_placement((700, 700))
    doc.saveas(path)

    parts, _ = read_dxf(
        str(path),
        number_layers=["A", "B"],
        label_pattern=r"^\d{2}B-?\d+$",
    )
    assert len(parts) == 1
    assert parts[0]["original_number"] == "01B-7"


def test_read_dxf_keeps_number_layer_argument_backward_compatible(tmp_path):
    path = tmp_path / "legacy.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("my_bian_layer")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 0)],
        dxfattribs={"layer": "PANEL"},
    )
    text = msp.add_text("99", dxfattribs={"layer": "my_bian_layer"})
    text.set_placement((500, 500))
    doc.saveas(path)

    parts, _ = read_dxf(str(path), number_layer="my_bian_layer")
    assert parts[0]["original_number"] == "99"


def test_extract_closed_polygons_recovers_l_shaped_self_touching_outline(tmp_path):
    path = tmp_path / "l_shape_self_touch.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [
            (0, 0),
            (100, 0),
            (100, 100),
            (70, 100),
            (70, 150),
            (0, 150),
            (0, 0),
            (70, 0),
        ],
        dxfattribs={"layer": "PANEL"},
    )
    doc.saveas(path)

    doc = ezdxf.readfile(path)
    polygons = extract_closed_polygons(
        doc,
        panel_layers=["PANEL"],
        exclude_linetypes=[],
        closed_tolerance=0.1,
    )
    assert len(polygons) == 1


def test_extract_closed_polygons_does_not_recover_plain_open_polyline(tmp_path):
    path = tmp_path / "plain_open.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (100, 0), (100, 100)],
        dxfattribs={"layer": "PANEL"},
    )
    doc.saveas(path)

    doc = ezdxf.readfile(path)
    polygons = extract_closed_polygons(
        doc,
        panel_layers=["PANEL"],
        exclude_linetypes=[],
        closed_tolerance=0.1,
    )
    assert polygons == []


def test_extract_closed_polygons_excludes_handles(tmp_path):
    path = tmp_path / "exclude_handle.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    msp = doc.modelspace()
    entity = msp.add_lwpolyline(
        [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)],
        dxfattribs={"layer": "PANEL"},
    )
    doc.saveas(path)

    doc = ezdxf.readfile(path)
    polygons = extract_closed_polygons(
        doc,
        panel_layers=["PANEL"],
        exclude_linetypes=[],
        exclude_handles={entity.dxf.handle},
    )
    assert polygons == []
