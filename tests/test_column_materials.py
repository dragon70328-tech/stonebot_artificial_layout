"""列头材料（多层平面图：每列一个楼层）回归测试。

citizen.dxf 的结构：x 方向每列 = 一个楼层，列头同时写 `L-XX LEVEL` 与
`材料：ST-x`；规格板画在列内。零件的材料号必须来自它所在的列，
并且要能随原位 DXF 输出（零件级 MATERIALS + 列头级 MATERIAL_HEADER）。
"""

import ezdxf
import pytest

from src.drawing_profile import (
    DrawingProfile,
    collect_column_materials,
    read_dxf_with_profile,
)
from src.dxf_writer import write_numbered_parts_dxf
from src.numbering import assign_numbers

LAYER = "0"


def _profile() -> DrawingProfile:
    return DrawingProfile(
        name="columns",
        version="1.0.0",
        panel_layer=LAYER,
        panel_layers=[LAYER],
        number_layers=[LAYER],
        hatch_layer=None,
        use_hatch=False,
        build_hierarchy=False,
        region_min_side_mm=0.0,
        small_area_threshold=0.0,
        material_column_enabled=True,
        material_layers=[LAYER],
    )


def _make_dxf(path) -> None:
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_text("L-10 LEVEL", dxfattribs={"layer": LAYER, "height": 200})
    msp.add_text(
        "材料：ST-6",
        dxfattribs={"layer": LAYER, "height": 200},
    ).set_placement((3000, 0))
    msp.add_text(
        "L-11 LEVEL",
        dxfattribs={"layer": LAYER, "height": 200},
    ).set_placement((100000, 0))
    msp.add_text(
        "材料：ST-7/ST-8",
        dxfattribs={"layer": LAYER, "height": 200},
    ).set_placement((103000, 0))

    def panel(x_min: float, y_min: float) -> None:
        msp.add_lwpolyline(
            [
                (x_min, y_min),
                (x_min + 600, y_min),
                (x_min + 600, y_min + 400),
                (x_min, y_min + 400),
            ],
            close=True,
            dxfattribs={"layer": LAYER},
        )

    panel(-5000, 0)      # 所有列之外
    panel(2000, 0)       # 第 1 列 -> ST-6
    panel(102000, 0)     # 第 2 列 -> ST-7/ST-8
    doc.saveas(path)


def _materials_by_x(parts_data: list[dict]) -> dict[float, str | None]:
    return {
        round(float(part["centroid"][0])): part["material_group"]
        for part in parts_data
    }


def test_collect_column_materials_pairs_level_and_material_labels(tmp_path):
    path = tmp_path / "columns.dxf"
    _make_dxf(path)
    doc = ezdxf.readfile(path)

    columns = collect_column_materials(doc, _profile())

    assert [column["level"] for column in columns] == ["L-10", "L-11"]
    assert columns[0]["materials"] == ["ST-6"]
    assert columns[0]["material_group"] == "ST-6"
    assert columns[1]["materials"] == ["ST-7", "ST-8"]
    assert columns[1]["material_group"] == "ST-7/ST-8"
    assert columns[1]["label"] == "材料：ST-7/ST-8"
    assert columns[1]["x_max"] == float("inf")
    assert "warning" not in columns[0]
    assert "warning" not in columns[1]


def test_read_assigns_material_from_containing_column(tmp_path):
    path = tmp_path / "columns.dxf"
    _make_dxf(path)

    parts_data, _doc = read_dxf_with_profile(str(path), _profile())

    materials = _materials_by_x(parts_data)
    # 列内零件必须拿到该列材料号；列外零件保持空，不猜也不丢。
    assert materials[2300] == "ST-6"
    assert materials[102300] == "ST-7/ST-8"
    assert materials[-4700] is None


def test_column_boundary_is_level_label_left_edge(tmp_path):
    """列边界 = 楼层标签左边缘（citizen 的标签是左对齐 TEXT）。

    历史问题：用包围盒中心当边界会让每列整体右移半个字宽，把边界附近
    的零件错分到相邻列，甚至落到第一列之外变成"无材料"。
    """
    path = tmp_path / "boundary.dxf"
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_text("L-10 LEVEL", dxfattribs={"layer": LAYER, "height": 200})
    msp.add_text(
        "材料：ST-6",
        dxfattribs={"layer": LAYER, "height": 200},
    ).set_placement((3000, 0))
    msp.add_text(
        "L-11 LEVEL",
        dxfattribs={"layer": LAYER, "height": 200},
    ).set_placement((100000, 0))
    msp.add_text(
        "材料：ST-7/ST-8",
        dxfattribs={"layer": LAYER, "height": 200},
    ).set_placement((103000, 0))
    # 质心 x=100400：在 L-11 标签左边缘右侧、包围盒中心左侧。
    msp.add_lwpolyline(
        [(100100, 0), (100700, 0), (100700, 400), (100100, 400)],
        close=True,
        dxfattribs={"layer": LAYER},
    )
    doc.saveas(path)

    parts_data, _doc = read_dxf_with_profile(str(path), _profile())

    assert _materials_by_x(parts_data) == {100400: "ST-7/ST-8"}


def test_numbered_dxf_writes_part_material_and_header_labels(tmp_path):
    path = tmp_path / "columns.dxf"
    _make_dxf(path)
    profile = _profile()

    parts_data, doc = read_dxf_with_profile(str(path), profile)
    parts = assign_numbers(parts_data, skip_unnumbered=False)
    columns = collect_column_materials(doc, profile)
    headers = [
        {"text": column["label"], "point": column["point"],
         "height": column["height"]}
        for column in columns
        if column["label"]
    ]

    output = tmp_path / "numbered_material.dxf"
    write_numbered_parts_dxf(parts, str(output), material_headers=headers)

    written = ezdxf.readfile(output)
    tags = [
        entity.dxf.text
        for entity in written.modelspace()
        if entity.dxf.layer == "MATERIALS"
    ]
    assert sorted(tags) == ["ST-6", "ST-7/ST-8"]

    header_layer = [
        entity
        for entity in written.modelspace()
        if entity.dxf.layer == "MATERIAL_HEADER"
    ]
    assert sorted(entity.dxf.text for entity in header_layer) == [
        "材料：ST-6",
        "材料：ST-7/ST-8",
    ]
    for entity, column in zip(
        sorted(header_layer, key=lambda item: item.dxf.insert.x),
        columns,
    ):
        assert float(entity.dxf.insert.x) == pytest.approx(
            float(column["point"][0])
        )
        assert float(entity.dxf.insert.y) == pytest.approx(
            float(column["point"][1])
        )

    # 材料号跟随零件成为同一个 GROUP（选中即全选）。
    tagged = {
        name: len(list(group))
        for name, group in written.groups
        if any(
            entity.dxf.layer == "MATERIALS" for entity in group
        )
    }
    assert len(tagged) == 2
    assert set(tagged.values()) == {3}


def test_numbered_dxf_without_materials_has_no_material_layers(tmp_path):
    path = tmp_path / "columns.dxf"
    _make_dxf(path)

    parts_data, _doc = read_dxf_with_profile(str(path), _profile())
    parts = assign_numbers(parts_data, skip_unnumbered=False)
    for part in parts:
        part.material_group = None

    output = tmp_path / "numbered.dxf"
    write_numbered_parts_dxf(parts, str(output))

    layers = {entity.dxf.layer for entity in ezdxf.readfile(output).modelspace()}
    assert "MATERIALS" not in layers
    assert "MATERIAL_HEADER" not in layers
