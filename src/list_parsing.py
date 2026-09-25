"""清单解析适配层：从 Excel/PDF 提取矩形规格件尺寸与数量。

从 ``src/list_nesting.py`` 抽出的输入适配层，只负责把清单读成
``ListItem`` 列表，不参与排板与结果输出：

- ``.xlsx`` 使用标准库 zipfile + ElementTree 读取共享字符串和工作表；
- ``.xls`` 不支持，需要用户另存为 .xlsx；
- ``.pdf`` 使用 PyMuPDF 表格检测，无表格时退回按文本行切分。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass
class ListItem:
    """清单中的一行规格板尺寸与数量。"""

    material: str
    length_mm: float
    width_mm: float
    qty: int = 1
    source: str = ""


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _normalize_header(value: Any) -> str:
    text = _clean_cell(value).lower()
    text = text.replace("（", "(").replace("）", ")")
    return re.sub(r"[\s_()（）:：]", "", text)


def _cell_number(value: Any) -> float | None:
    """严格解析纯数字单元格，避免把 `ST-101` 这类编号误判为数值。"""
    text = _clean_cell(value).replace(",", "").replace("，", "")
    if not text:
        return None
    match = re.fullmatch(
        r"(-?\d+(?:\.\d+)?)\s*(?:mm|cm|m)?", text, flags=re.IGNORECASE
    )
    if not match:
        return None
    return float(match.group(1))


def _dimension_pair(value: Any) -> tuple[float, float] | None:
    """解析 `1210x90` 这类把长宽写在同一单元格中的值。"""
    text = _clean_cell(value)
    if not text:
        return None
    text = text.replace("×", "x").replace("X", "x").replace("*", "x")
    match = re.search(r"(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _col_index(ref: str | None) -> int:
    if not ref:
        return 0
    match = re.match(r"[A-Za-z]+", ref)
    if not match:
        return 0
    result = 0
    for char in match.group(0).upper():
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


_LENGTH_HINTS = ("长度", "length", "规格mm", "规格尺寸", "长mm")
_WIDTH_HINTS = ("宽度", "width", "宽mm")
_QTY_HINTS = ("数量", "件数", "块数", "qty", "quantity", "count", "个数")
_MATERIAL_HINTS = ("材料", "编号", "型号", "material", "code", "item", "名称")


def _detect_column_map(rows: list[list[str]]) -> dict[str, int | None]:
    """扫描前若干行中的表头，返回 material/length/width/qty 列下标。"""
    column_map: dict[str, int | None] = {
        "material": None,
        "length": None,
        "width": None,
        "qty": None,
    }
    for row in rows[:12]:
        for index, cell in enumerate(row):
            header = _normalize_header(cell)
            if not header:
                continue
            if column_map["length"] is None:
                if any(hint in header for hint in _LENGTH_HINTS):
                    if "周长" not in header and "perimeter" not in header:
                        column_map["length"] = index
                        continue
            if column_map["width"] is None and any(
                hint in header for hint in _WIDTH_HINTS
            ):
                column_map["width"] = index
                continue
            if column_map["qty"] is None and any(
                hint in header for hint in _QTY_HINTS
            ):
                column_map["qty"] = index
                continue
            if column_map["material"] is None and any(
                hint in header for hint in _MATERIAL_HINTS
            ):
                column_map["material"] = index
                continue
    return column_map


def _row_has_data(row: list[str]) -> bool:
    numeric_count = sum(_cell_number(cell) is not None for cell in row)
    if numeric_count >= 2:
        return True
    return any(_dimension_pair(cell) is not None for cell in row)


def _numeric_cells(row: list[str]) -> list[tuple[int, float]]:
    result = []
    for index, cell in enumerate(row):
        number = _cell_number(cell)
        if number is not None:
            result.append((index, number))
    return result


def _row_to_item(
    row: list[str],
    column_map: dict[str, int | None],
    source: str,
) -> ListItem | None:
    row = list(row)
    while len(row) < 10:
        row.append("")

    def get(index: int | None) -> str:
        if index is None:
            return ""
        return row[index]

    length = _cell_number(get(column_map["length"]))
    width = _cell_number(get(column_map["width"]))
    qty = _cell_number(get(column_map["qty"]))

    if length is None or width is None:
        for index, cell in enumerate(row):
            pair = _dimension_pair(cell)
            if pair is not None:
                length, width = pair
                break

    numeric_cells = _numeric_cells(row)
    if (length is None or width is None) and len(numeric_cells) == 2:
        length = numeric_cells[0][1]
        width = numeric_cells[1][1]
        if qty is None:
            qty = 1.0
    elif (length is None or width is None) and len(numeric_cells) >= 3:
        length = numeric_cells[0][1]
        width = numeric_cells[1][1]
        if qty is None:
            qty = numeric_cells[2][1]

    if length is None or width is None or length <= 0 or width <= 0:
        return None

    if qty is None or qty <= 0:
        qty = 1.0

    material_index = column_map["material"]
    if material_index is not None and get(material_index):
        material = get(material_index)
    else:
        material = next(
            (
                cell
                for cell in row
                if cell and _cell_number(cell) is None
                and _dimension_pair(cell) is None
                and not _normalize_header(cell).startswith(("材料", "编号", "型号"))
            ),
            "",
        )

    return ListItem(
        material=material,
        length_mm=length,
        width_mm=width,
        qty=int(round(qty)),
        source=source,
    )


def _rows_to_items(rows: list[list[str]], source: str) -> list[ListItem]:
    column_map = _detect_column_map(rows)
    items: list[ListItem] = []
    for row in rows:
        if not _row_has_data(row):
            continue
        item = _row_to_item(row, column_map, source)
        if item is not None:
            items.append(item)
    return items


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        data = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    strings = []
    for item in root.findall(_XLSX_NS + "si"):
        strings.append(
            "".join((node.text or "") for node in item.findall(_XLSX_NS + "t"))
        )
    return strings


def _parse_worksheet(data: bytes, shared_strings: list[str]) -> list[list[str]]:
    root = ET.fromstring(data)
    rows: list[list[str]] = []
    for row_node in root.iter(_XLSX_NS + "row"):
        cells: dict[int, str] = {}
        max_index = -1
        for cell_node in row_node.findall(_XLSX_NS + "c"):
            ref = cell_node.get("r")
            index = _col_index(ref)
            cell_type = cell_node.get("t")
            value_node = cell_node.find(_XLSX_NS + "v")
            inline_node = cell_node.find(_XLSX_NS + "is")
            value = ""
            if cell_type == "s" and value_node is not None:
                try:
                    value = shared_strings[int(value_node.text)]
                except (ValueError, IndexError):
                    value = value_node.text or ""
            elif cell_type == "inlineStr" and inline_node is not None:
                value = "".join(
                    (node.text or "") for node in inline_node.findall(_XLSX_NS + "t")
                )
            elif value_node is not None:
                value = value_node.text or ""
            cells[index] = value
            max_index = max(max_index, index)
        if max_index >= 0:
            rows.append([cells.get(index, "") for index in range(max_index + 1)])
    return rows


def _split_worksheet_column_groups(rows: list[list[str]]) -> list[list[str]]:
    """拆分同一工作表中重复出现的“尺寸/数量”列组。

    一些清单会在一张表里从左到右放多组列，例如 A/B 组、C/D 组、E/F 组。
    这里按数量列定位每组列范围，并把每组重新整理成独立行。
    """
    header_row: list[str] | None = None
    qty_indices: list[int] = []
    for row in rows[:20]:
        indices = [
            index
            for index, cell in enumerate(row)
            if any(hint in _normalize_header(cell) for hint in _QTY_HINTS)
        ]
        if indices:
            header_row = row
            qty_indices = indices
            break

    if not header_row or not qty_indices:
        return rows

    if len(qty_indices) == 1:
        return rows

    group_ranges: list[list[int]] = []
    previous_end = -1
    for qty_index in qty_indices:
        group_ranges.append(list(range(previous_end + 1, qty_index + 1)))
        previous_end = qty_index

    result: list[list[str]] = []
    for group_range in group_ranges:
        for row in rows:
            result.append([row[index] if index < len(row) else "" for index in group_range])
    return result


def parse_xlsx(path: str | Path) -> list[ListItem]:
    """解析 .xlsx 文件中的规格尺寸与数量。"""
    path = Path(path)
    rows: list[list[str]] = []
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheet_names = sorted(
            (
                name
                for name in archive.namelist()
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
            )
        )
        for sheet_name in sheet_names:
            rows.extend(
                _split_worksheet_column_groups(
                    _parse_worksheet(archive.read(sheet_name), shared_strings)
                )
            )
    return _rows_to_items(rows, str(path))


def parse_xls(path: str | Path) -> list[ListItem]:
    """旧版 .xls 二进制格式不支持，提示用户转换。"""
    raise ValueError("暂不支持 .xls，请先在 Excel 中另存为 .xlsx 后重试")


def _pdf_text_rows(page) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in page.get_text("text").splitlines():
        line = line.strip()
        if not line:
            continue
        cells = re.split(r"\s{2,}|\t", line)
        rows.append([_clean_cell(cell) for cell in cells])
    return rows


def parse_pdf(path: str | Path) -> list[ListItem]:
    """解析 PDF 表格中的规格尺寸与数量。"""
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("解析 PDF 需要 PyMuPDF，请先安装 pymupdf") from exc

    rows: list[list[str]] = []
    with pymupdf.open(str(path)) as doc:
        for page in doc:
            tables = page.find_tables()
            if tables.tables:
                for table in tables.tables:
                    rows.extend(
                        [_clean_cell(cell) for cell in row]
                        for row in table.extract()
                    )
            else:
                rows.extend(_pdf_text_rows(page))
    return _rows_to_items(rows, str(path))


def parse_list_file(path: str | Path) -> list[ListItem]:
    """按扩展名解析 Excel/PDF 清单。"""
    suffix = Path(path).suffix.lower()
    if suffix == ".xlsx":
        return parse_xlsx(path)
    if suffix == ".xls":
        return parse_xls(path)
    if suffix == ".pdf":
        return parse_pdf(path)
    raise ValueError(f"不支持的清单格式：{suffix or '无扩展名'}；仅支持 .xlsx/.pdf")
