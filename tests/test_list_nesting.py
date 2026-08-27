"""清单排板模块测试：解析 Excel/PDF 与矩形排板。"""

from pathlib import Path
import zipfile

import pytest

from src.list_nesting import (
    ListItem,
    build_conclusion_text,
    nest_list_items,
    parse_pdf,
    parse_xlsx,
)
from src.nesting import validate_nesting


def _column_letter(index: int) -> str:
    value = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        value = chr(65 + rem) + value
    return value


def _write_xlsx(path: Path, rows: list[list[object]]) -> None:
    shared: list[str] = []
    shared_index: dict[str, int] = {}

    def shared_id(value: str) -> int:
        if value not in shared_index:
            shared_index[value] = len(shared)
            shared.append(value)
        return shared_index[value]

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    row_xml = f'<worksheet xmlns="{ns}"><sheetData>'
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row):
            ref = f"{_column_letter(col_index)}{row_index}"
            if isinstance(value, str):
                sid = shared_id(value)
                cells.append(f'<c r="{ref}" t="s"><v>{sid}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        row_xml += f'<row r="{row_index}">{"".join(cells)}</row>'
    row_xml += "</sheetData></worksheet>"

    shared_xml = f'<sst xmlns="{ns}" count="{len(shared)}" uniqueCount="{len(shared)}">'
    for text in shared:
        shared_xml += f"<si><t>{text}</t></si>"
    shared_xml += "</sst>"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared_xml)
        archive.writestr("xl/worksheets/sheet1.xml", row_xml)


def _write_pdf(path: Path) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=240)
    page.draw_rect(pymupdf.Rect(30, 30, 390, 120), color=(0, 0, 0))
    page.draw_line((30, 55), (390, 55), color=(0, 0, 0))
    for x in (110, 190, 270):
        page.draw_line((x, 30), (x, 120), color=(0, 0, 0))
    texts = [
        (40, 48, "材料编号"),
        (120, 48, "长度(mm)"),
        (200, 48, "宽度(mm)"),
        (280, 48, "数量"),
        (40, 80, "ST-101"),
        (120, 80, "1197"),
        (200, 80, "900"),
        (280, 80, "8"),
    ]
    for x, y, text in texts:
        page.insert_text((x, y), text, fontsize=10)
    doc.save(path)


def test_parse_xlsx(tmp_path):
    path = tmp_path / "list.xlsx"
    _write_xlsx(
        path,
        [
            ["材料编号", "位置", "长度(mm)", "宽度(mm)", "数量(件)"],
            ["ST-101", "D区", 1197, 900, 8],
            ["ST-102", "E区", 1397, 1247, 2],
        ],
    )
    items = parse_xlsx(path)
    assert len(items) == 2
    assert items[0].material == "ST-101"
    assert items[0].length_mm == 1197.0
    assert items[0].width_mm == 900.0
    assert items[0].qty == 8


def test_parse_pdf_table(tmp_path):
    path = tmp_path / "list.pdf"
    _write_pdf(path)
    items = parse_pdf(path)
    assert len(items) == 1
    assert items[0].material == "ST-101"
    assert items[0].length_mm == 1197.0
    assert items[0].width_mm == 900.0
    assert items[0].qty == 8


def test_nest_groups_repeated_material(tmp_path):
    items = [
        ListItem("ST-101", 1000, 600, 3, "a"),
        ListItem("ST-101", 800, 500, 2, "b"),
        ListItem("ST-102", 1200, 800, 2, "c"),
    ]
    results = nest_list_items(
        items, sheet_width_mm=2500, sheet_height_mm=1400, thickness_mm=20
    )
    assert {material for material, _, _ in results} == {"ST-101", "ST-102"}
    for _, _, result in results:
        assert validate_nesting(result, 2500, 1400) == []


def test_nest_unique_codes_share_sheets(tmp_path):
    items = [
        ListItem("A1-3-01", 1000, 600, 1, "a"),
        ListItem("A1-3-02", 800, 500, 1, "b"),
        ListItem("A1-3-03", 1200, 800, 1, "c"),
    ]
    results = nest_list_items(
        items, sheet_width_mm=2500, sheet_height_mm=1400, thickness_mm=20
    )
    assert len(results) == 1
    result = results[0][2]
    assert result.total_sheets == 1
    assert validate_nesting(result, 2500, 1400) == []


def test_unfit_item_raises():
    items = [ListItem("BIG", 3000, 2000, 1, "a")]
    with pytest.raises(ValueError, match="无法放入"):
        nest_list_items(
            items, sheet_width_mm=2500, sheet_height_mm=1400, thickness_mm=20
        )


def test_pairing_uses_two_large_pieces_per_sheet():
    items = [ListItem("PAIR", 1397, 1247, 2, "a")]
    results = nest_list_items(
        items, sheet_width_mm=2500, sheet_height_mm=1400, thickness_mm=20
    )
    result = results[0][2]
    assert result.total_sheets == 1
    assert validate_nesting(result, 2500, 1400) == []


def test_conclusion_text_summary_and_detail():
    items = [ListItem("ST-101", 1000, 600, 2, "a")]
    results = nest_list_items(
        items, sheet_width_mm=2500, sheet_height_mm=1400, thickness_mm=20
    )
    text = build_conclusion_text(items, results, [(2500, 1400)])
    assert "使用大板" in text
    assert "总出材率" in text
    assert "板 1" not in text

    detailed = build_conclusion_text(items, results, [(2500, 1400)], show_sheets=True)
    assert "板" in detailed
    assert "使用面积" in detailed
