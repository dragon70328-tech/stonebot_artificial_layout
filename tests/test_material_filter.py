"""分单排板的材料筛选回归测试（`--material`）。

不同材料绝不共板：一次只排一个材料组时用 `--material ST-6` 过滤零件，
材料组写错要直接报错，不能静默排出空结果或排出别家的料。
"""

import ezdxf
import pytest
from shapely.geometry import Polygon

import main as app
from src.drawing_profile import DrawingProfile
from src.models import Part
from src.units import UnitSystem


def _profile() -> DrawingProfile:
    return DrawingProfile(
        name="material_filter_test",
        version="1.0.0",
        panel_layer="0",
        panel_layers=["0"],
        number_layers=["0"],
        hatch_layer=None,
        use_hatch=False,
        build_hierarchy=False,
        region_min_side_mm=0.0,
        small_area_threshold=0.0,
        exclude_linetypes=[],
        material_column_enabled=True,
        material_layers=["0"],
    )


def _make_dxf(path) -> None:
    """两列两材料：第 1 列 ST-6（2 件），第 2 列 ST-7/ST-8（2 件）。"""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_text("L-10 LEVEL", dxfattribs={"layer": "0", "height": 200})
    msp.add_text(
        "材料：ST-6",
        dxfattribs={"layer": "0", "height": 200},
    ).set_placement((3000, 0))
    msp.add_text(
        "L-11 LEVEL",
        dxfattribs={"layer": "0", "height": 200},
    ).set_placement((100000, 0))
    msp.add_text(
        "材料：ST-7/ST-8",
        dxfattribs={"layer": "0", "height": 200},
    ).set_placement((103000, 0))

    for x_min in (2000, 3000, 102000, 103000):
        msp.add_lwpolyline(
            [
                (x_min, 0),
                (x_min + 600, 0),
                (x_min + 600, 400),
                (x_min, 400),
            ],
            close=True,
            dxfattribs={"layer": "0"},
        )
    doc.saveas(path)


def _part(part_id: int, number: str, material: str | None) -> Part:
    outer = Polygon([(0, 0), (400, 0), (400, 300), (0, 300)])
    return Part(
        id=part_id,
        number=number,
        polygon=outer,
        outer_polygon=outer,
        holes=[],
        label_position=(200, 150),
        material_group=material,
    )


def test_parse_material_filter():
    assert app.parse_material_filter(None) is None
    assert app.parse_material_filter("") is None
    assert app.parse_material_filter("ST-6") == ["ST-6"]
    assert app.parse_material_filter(" ST-6 , ST-7 ") == ["ST-6", "ST-7"]


def test_filter_keeps_only_requested_material_group():
    parts = [
        _part(1, "1001-01", "ST-6"),
        _part(2, "1001-02", "ST-6"),
        _part(3, "1002-01", "ST-7/ST-8"),
    ]

    kept, groups = app._filter_parts_by_material(parts, ["ST-6"])

    assert [part.number for part in kept] == ["1001-01", "1001-02"]
    assert groups == ["ST-6"]


def test_filter_rejects_unknown_material_group(capsys):
    parts = [_part(1, "1001-01", "ST-6")]

    with pytest.raises(SystemExit) as excinfo:
        app._filter_parts_by_material(parts, ["ST-99"])

    assert excinfo.value.code == 1
    assert "没有材料组 ST-99" in capsys.readouterr().out


def _run(dxf_path, output_root, **overrides):
    kwargs = dict(
        unit=UnitSystem.METRIC,
        trials=1,
        seed=0,
        budget=0,
        skip_unnumbered=False,
        layers=None,
        exclude_layers=None,
        drawing_profile=_profile(),
        profile=app.PROFILES["quick"],
        confirm_sheet_count=False,
        quick=True,
        report_only=True,
        output_root=output_root,
    )
    kwargs.update(overrides)
    return app.run(str(dxf_path), 1000, 800, 20, **kwargs)


def test_run_nests_only_requested_material(tmp_path, capsys):
    dxf_path = tmp_path / "two_materials.dxf"
    _make_dxf(dxf_path)

    outcome = _run(dxf_path, tmp_path, material_filter=["ST-6"])

    printed = capsys.readouterr().out
    assert "材料筛选：只排 ST-6 —— 2 件，跳过其他材料 2 件" in printed
    assert "只含材料 ST-6 的零件" in printed
    assert outcome["report_only"] is True


def test_run_without_filter_nests_all_materials(tmp_path, capsys):
    dxf_path = tmp_path / "two_materials.dxf"
    _make_dxf(dxf_path)

    _run(dxf_path, tmp_path)

    printed = capsys.readouterr().out
    assert "材料筛选" not in printed
