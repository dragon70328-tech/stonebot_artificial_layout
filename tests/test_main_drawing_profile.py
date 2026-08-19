from pathlib import Path

import ezdxf

import main as app
from src.drawing_profile import DrawingProfile
from src.units import UnitSystem


def test_resolve_drawing_profile_matches_dongguan_layers(tmp_path):
    path = tmp_path / "dongguan_like.dxf"
    doc = ezdxf.new("R2010")
    layer_names = [
        "\u77f3\u6750\u5206\u7f1d",
        "\u77f3\u6750\u6587\u5b57",
        "\u77f3\u6750\u7f16\u53f7",
        "\u77f3\u6750\u7bb1\u53f7",
    ]
    for layer in layer_names:
        doc.layers.add(layer)
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 0)],
        dxfattribs={"layer": layer_names[0]},
    )
    for index, layer in enumerate(layer_names[1:], 1):
        text = msp.add_text(f"01B-{index}", dxfattribs={"layer": layer})
        text.set_placement((100 * index, 100 * index))
    doc.saveas(path)

    profile = app.resolve_drawing_profile(str(path))
    assert profile is not None
    assert profile.name == "dongguan_outlets"


def test_run_uses_profile_read_path_when_drawing_profile_matched(tmp_path):
    path = tmp_path / "profile_panel.dxf"
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

    drawing_profile = DrawingProfile(
        name="integration_test",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )

    app.run(
        str(path),
        100,
        100,
        20,
        unit=UnitSystem.METRIC,
        trials=1,
        seed=0,
        budget=0,
        skip_unnumbered=False,
        layers=None,
        exclude_layers=None,
        drawing_profile=drawing_profile,
        profile=app.PROFILES["quick"],
        confirm_sheet_count=False,
        report_only=True,
        quick=True,
    )
