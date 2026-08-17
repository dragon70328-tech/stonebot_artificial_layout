from pathlib import Path

import ezdxf

import main as app


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
