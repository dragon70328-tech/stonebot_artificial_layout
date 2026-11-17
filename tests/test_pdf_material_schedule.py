from __future__ import annotations

import zipfile

import ezdxf

from src.pdf_intake.material_schedule import (
    build_material_schedule,
    write_material_schedule_xlsx,
)
from src.xlsx_writer import write_xlsx_sheets


def test_multi_sheet_workbook(tmp_path):
    path = tmp_path / "multi.xlsx"
    write_xlsx_sheets(
        path,
        [
            ("汇总", [["材料", "数量"], ["ST-101", 2]]),
            ("明细", [["板号", "规格"], ["D1F-1", "1197x900"]]),
        ],
    )
    with zipfile.ZipFile(path) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        assert 'name="汇总"' in workbook
        assert 'name="明细"' in workbook
        assert "xl/worksheets/sheet2.xml" in archive.namelist()


def test_material_schedule_uses_irregular_bounding_size(tmp_path):
    doc = ezdxf.new("R2018")
    doc.layers.add("OUTER")
    doc.layers.add("MATERIALS")
    msp = doc.modelspace()

    rectangle = msp.add_lwpolyline(
        [(0, 0), (1197, 0), (1197, 900), (0, 900)],
        close=True,
        dxfattribs={"layer": "OUTER"},
    )
    material1 = msp.add_text(
        "ST-101 20mm", dxfattribs={"layer": "MATERIALS"}
    )
    group1 = doc.groups.new("D1F-1")
    group1.extend((rectangle, material1))

    irregular = msp.add_lwpolyline(
        [
            (0, 0, -0.414213562),
            (450, 450, 0),
            (1197, 450, 0),
            (1197, -275, 0),
            (0, -275, 0),
        ],
        format="xyb",
        close=True,
        dxfattribs={"layer": "OUTER"},
    )
    material2 = msp.add_text(
        "ST-103 20mm", dxfattribs={"layer": "MATERIALS"}
    )
    group2 = doc.groups.new("D1F-2")
    group2.extend((irregular, material2))

    source = tmp_path / "source.dxf"
    doc.saveas(source)
    schedule = build_material_schedule(source)
    assert len(schedule.parts) == 2
    sizes = {
        (part.material_code, part.spec_length_mm, part.spec_width_mm)
        for part in schedule.parts
    }
    assert ("ST-101", 1197, 900) in sizes
    assert ("ST-103", 1197, 725) in sizes
    assert any(part.irregular for part in schedule.parts)

    output = write_material_schedule_xlsx(
        schedule, tmp_path / "schedule.xlsx"
    )
    assert output.is_file()
    assert output.stat().st_size > 1000
