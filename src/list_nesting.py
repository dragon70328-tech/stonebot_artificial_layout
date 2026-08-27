"""清单排板：从 Excel/PDF 规格尺寸与数量清单生成矩形零件并排板。

本模块不依赖 openpyxl/pandas：
- Excel(.xlsx) 使用标准库 zipfile + ElementTree 读取共享字符串和工作表。
- PDF 使用 PyMuPDF 的表格检测提取；无表格时退回按文本行切分。

排板复用 src.nesting.nest_parts，输出仅保留结论文本：使用板数、出材率和
各板占用率。
"""

from __future__ import annotations

import re
import itertools
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from shapely.geometry import box

from src.models import NestingResult, Part, Sheet
from src.strip_packing import pack_residual_strips


DEFAULT_ROTATIONS = (0, 90)
QUICK_CONFIGS = [
    ("short", "skyline", 0),
    ("short", "col", 0),
    ("area", "skyline", 0),
    ("long", "col", 0),
]

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


def _item_fits(
    item: ListItem,
    sheet_width_mm: float,
    sheet_height_mm: float,
    rotations: tuple[int, ...],
) -> bool:
    """判断一个矩形零件在给定旋转集合下能否放入大板。"""
    for angle in rotations:
        if angle % 180 == 0:
            length, width = item.length_mm, item.width_mm
        else:
            length, width = item.width_mm, item.length_mm
        if length <= sheet_width_mm + 1e-6 and width <= sheet_height_mm + 1e-6:
            return True
    return False

def _orientations(
    item: ListItem,
    rotations: tuple[int, ...],
) -> list[tuple[float, float]]:
    """返回去重后的矩形方向 (width, height)。"""
    orientations: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for angle in rotations:
        if angle % 180 == 0:
            pair = (item.length_mm, item.width_mm)
        else:
            pair = (item.width_mm, item.length_mm)
        if pair not in seen:
            seen.add(pair)
            orientations.append(pair)
    return orientations


def _strip_first_orientations(
    item: ListItem,
    rotations: tuple[int, ...],
) -> list[tuple[float, float]]:
    """窄条排板时优先短边朝下，形成密集竖列。"""
    return sorted(
        _orientations(item, rotations),
        key=lambda pair: (pair[0], pair[1]),
    )


def _merge_skyline(
    skyline: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """合并相邻且高度相同的 skyline 段，降低后续搜索规模。"""
    skyline.sort(key=lambda seg: seg[0])
    merged: list[list[float]] = []
    for x, y, width in skyline:
        if not merged:
            merged.append([x, y, width])
            continue
        px, py, pw = merged[-1]
        if abs(py - y) <= 1e-6 and abs(px + pw - x) <= 1e-6:
            merged[-1][2] += width
        else:
            merged.append([x, y, width])
    return [(x, y, width) for x, y, width in merged]


def _find_skyline_index(
    skyline: list[tuple[float, float, float]],
    width: float,
    height: float,
    sheet_width: float,
    sheet_height: float,
) -> int | None:
    best_index: int | None = None
    best_key: tuple[float, float, float] | None = None
    for index, (x, y, seg_width) in enumerate(skyline):
        if width > seg_width + 1e-6:
            continue
        if y + height > sheet_height + 1e-6:
            continue
        key = (y, x, min(width, height))
        if best_key is None or key < best_key:
            best_key = key
            best_index = index
    return best_index


def _place_item_in_skyline(
    item: ListItem,
    skyline: list[tuple[float, float, float]],
    sheet_width: float,
    sheet_height: float,
    rotations: tuple[int, ...],
    next_id: int,
    orientations: list[tuple[float, float]] | None = None,
) -> Part | None:
    best: tuple[tuple[float, float, float], int, float, float, float, float] | None = None
    for width, height in orientations or _orientations(item, rotations):
        index = _find_skyline_index(
            skyline, width, height, sheet_width, sheet_height
        )
        if index is None:
            continue
        x, y, _ = skyline[index]
        key = (y, x, min(width, height))
        if best is None or key < best[0]:
            best = (key, index, x, y, width, height)

    if best is None:
        return None

    _, index, x, y, width, height = best
    seg_width = skyline[index][2]
    replacement = [(x, y + height, width)]
    if x + width < x + seg_width:
        replacement.append((x + width, y, seg_width - width))
    skyline[index : index + 1] = replacement
    skyline[:] = _merge_skyline(skyline)

    polygon = box(x, y, x + width, y + height)
    number = f"{item.material or 'P'}-{next_id:04d}"
    return Part(
        id=next_id,
        number=number,
        polygon=polygon,
        outer_polygon=polygon,
        material_group=item.material or None,
        area=item.length_mm * item.width_mm,
        label_position=(x + width / 2.0, y + height / 2.0),
    )


def _pack_group_with_preference(
    items: list[ListItem],
    start_id: int,
    sizes: list[tuple[float, float]],
    thickness_mm: float,
    rotations: tuple[int, ...],
    prefer_index: int,
    prefer_vertical_strips: bool = False,
) -> NestingResult:
    """使用给定开新板偏好执行一次可变尺寸 skyline 排板。"""
    flats: list[ListItem] = []
    for item in items:
        flats.extend([item] * max(1, item.qty))

    flats.sort(
        key=lambda item: (
            -(item.length_mm * item.width_mm),
            -max(item.length_mm, item.width_mm),
            -min(item.length_mm, item.width_mm),
            item.material or "",
        )
    )

    sheets_by_size: list[list[dict[str, Any]]] = [[] for _ in sizes]
    next_id = start_id

    area_asc = sorted(range(len(sizes)), key=lambda i: sizes[i][0] * sizes[i][1])
    opening_order = [prefer_index] + [
        i for i in area_asc if i != prefer_index
    ]

    for item in flats:
        placed = False

        for size_index in area_asc:
            sheet_width, sheet_height = sizes[size_index]
            if not _item_fits(item, sheet_width, sheet_height, rotations):
                continue
            for sheet in sheets_by_size[size_index]:
                orientations = (
                    _strip_first_orientations(item, rotations)
                    if prefer_vertical_strips and _is_strip_item(item)
                    else None
                )
                part = _place_item_in_skyline(
                    item,
                    sheet["skyline"],
                    sheet_width,
                    sheet_height,
                    rotations,
                    next_id,
                    orientations,
                )
                if part is not None:
                    sheet["parts"].append(part)
                    next_id += 1
                    placed = True
                    break
            if placed:
                break

        if placed:
            continue

        for size_index in opening_order:
            sheet_width, sheet_height = sizes[size_index]
            if not _item_fits(item, sheet_width, sheet_height, rotations):
                continue
            skyline = [(0.0, 0.0, sheet_width)]
            orientations = (
                _strip_first_orientations(item, rotations)
                if prefer_vertical_strips and _is_strip_item(item)
                else None
            )
            part = _place_item_in_skyline(
                item, skyline, sheet_width, sheet_height, rotations, next_id,
                orientations,
            )
            if part is not None:
                sheets_by_size[size_index].append(
                    {
                        "skyline": skyline,
                        "parts": [part],
                    }
                )
                next_id += 1
                placed = True
                break

        if not placed:
            raise ValueError(
                f"零件 {item.length_mm:.0f}x{item.width_mm:.0f} 无法放入任何可用大板"
            )

    sheets: list[Sheet] = []
    index = 1
    for size_index, sheet_list in enumerate(sheets_by_size):
        sheet_width, sheet_height = sizes[size_index]
        for sheet in sheet_list:
            sheets.append(
                Sheet(
                    index=index,
                    width=sheet_width,
                    height=sheet_height,
                    thickness=thickness_mm,
                    parts=sheet["parts"],
                )
            )
            index += 1

    total_part_area = sum(
        item.length_mm * item.width_mm * max(1, item.qty) for item in items
    )
    total_sheet_area = sum(sheet.total_area for sheet in sheets)
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(item.qty for item in items),
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


def _pack_group_limited(
    items: list[ListItem],
    start_id: int,
    sizes: list[tuple[float, float]],
    thickness_mm: float,
    rotations: tuple[int, ...],
    limited_index: int,
    max_limited_sheets: int,
) -> NestingResult:
    """限定某一种大板的最大张数，其余尺寸不限，再做 skyline 排板。"""
    flats: list[ListItem] = []
    for item in items:
        flats.extend([item] * max(1, item.qty))

    flats.sort(
        key=lambda item: (
            -(item.length_mm * item.width_mm),
            -max(item.length_mm, item.width_mm),
            -min(item.length_mm, item.width_mm),
            item.material or "",
        )
    )

    sheets_by_size: list[list[dict[str, Any]]] = [[] for _ in sizes]
    area_asc = sorted(range(len(sizes)), key=lambda i: sizes[i][0] * sizes[i][1])
    opening_order = [limited_index] + [
        i for i in area_asc if i != limited_index
    ]
    next_id = start_id

    for item in flats:
        placed = False

        for size_index in area_asc:
            sheet_width, sheet_height = sizes[size_index]
            if not _item_fits(item, sheet_width, sheet_height, rotations):
                continue
            for sheet in sheets_by_size[size_index]:
                part = _place_item_in_skyline(
                    item,
                    sheet["skyline"],
                    sheet_width,
                    sheet_height,
                    rotations,
                    next_id,
                )
                if part is not None:
                    sheet["parts"].append(part)
                    next_id += 1
                    placed = True
                    break
            if placed:
                break

        if placed:
            continue

        for size_index in opening_order:
            if (
                size_index == limited_index
                and len(sheets_by_size[limited_index]) >= max_limited_sheets
            ):
                continue
            sheet_width, sheet_height = sizes[size_index]
            if not _item_fits(item, sheet_width, sheet_height, rotations):
                continue
            skyline = [(0.0, 0.0, sheet_width)]
            part = _place_item_in_skyline(
                item, skyline, sheet_width, sheet_height, rotations, next_id
            )
            if part is not None:
                sheets_by_size[size_index].append(
                    {
                        "skyline": skyline,
                        "parts": [part],
                    }
                )
                next_id += 1
                placed = True
                break

        if not placed:
            raise ValueError(
                f"零件 {item.length_mm:.0f}x{item.width_mm:.0f} 无法放入任何可用大板"
            )

    sheets: list[Sheet] = []
    index = 1
    for size_index, sheet_list in enumerate(sheets_by_size):
        sheet_width, sheet_height = sizes[size_index]
        for sheet in sheet_list:
            sheets.append(
                Sheet(
                    index=index,
                    width=sheet_width,
                    height=sheet_height,
                    thickness=thickness_mm,
                    parts=sheet["parts"],
                )
            )
            index += 1

    total_part_area = sum(
        item.length_mm * item.width_mm * max(1, item.qty) for item in items
    )
    total_sheet_area = sum(sheet.total_area for sheet in sheets)
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(item.qty for item in items),
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


def _build_pair_patterns(
    item_types: list[ListItem],
    sizes: list[tuple[float, float]],
    rotations: tuple[int, ...],
) -> list[tuple[int, tuple[int, ...], tuple[tuple[float, float], ...], float, float, float]] | None:
    """枚举所有横向并排模式，不设固定件数，只要总宽和高不超过大板。"""
    patterns: list[
        tuple[
            int,
            tuple[int, ...],
            tuple[tuple[float, float], ...],
            float,
            float,
            float,
        ]
    ] = []
    seen: set[tuple[int, tuple[tuple[int, tuple[float, float]], ...]]] = set()
    aborted = [False]

    for size_index, (sheet_width, sheet_height) in enumerate(sizes):
        sheet_area = sheet_width * sheet_height

        def add_pattern(
            occ_indices: tuple[int, ...],
            occ_orientations: tuple[tuple[float, float], ...],
        ) -> None:
            if aborted[0]:
                return
            key = (
                size_index,
                tuple(sorted(zip(occ_indices, occ_orientations))),
            )
            if key in seen:
                return
            seen.add(key)

            part_area = sum(
                item_types[item_index].length_mm
                * item_types[item_index].width_mm
                for item_index in occ_indices
            )
            utilization = part_area / sheet_area if sheet_area else 0.0
            waste = sheet_area - part_area
            patterns.append(
                (
                    size_index,
                    occ_indices,
                    occ_orientations,
                    part_area,
                    utilization,
                    waste,
                )
            )
            if len(patterns) >= _MAX_PAIR_PATTERNS:
                aborted[0] = True

        def build(
            start_index: int,
            occ_indices: tuple[int, ...],
            occ_orientations: tuple[tuple[float, float], ...],
            total_width: float,
            max_height: float,
        ) -> None:
            if aborted[0]:
                return
            for item_index in range(start_index, len(item_types)):
                if aborted[0]:
                    break
                item = item_types[item_index]
                options = [
                    orientation
                    for orientation in _orientations(item, rotations)
                    if orientation[0] <= sheet_width + 1e-6
                    and orientation[1] <= sheet_height + 1e-6
                ]
                for orientation in options:
                    if aborted[0]:
                        break
                    next_width = total_width + orientation[0]
                    next_height = max(max_height, orientation[1])
                    if next_width > sheet_width + 1e-6:
                        continue
                    if next_height > sheet_height + 1e-6:
                        continue

                    next_indices = occ_indices + (item_index,)
                    next_orientations = occ_orientations + (orientation,)
                    add_pattern(next_indices, next_orientations)
                    build(
                        item_index,
                        next_indices,
                        next_orientations,
                        next_width,
                        next_height,
                    )

        build(0, (), (), 0.0, 0.0)
        if aborted[0]:
            break

    if aborted[0]:
        return None
    return patterns


def _pack_group_pairing(
    items: list[ListItem],
    start_id: int,
    sizes: list[tuple[float, float]],
    thickness_mm: float,
    rotations: tuple[int, ...],
) -> NestingResult | None:
    """按高利用率先选配对模式，失败或结果不理想时可退回 skyline。"""
    if not items:
        return None

    patterns = _build_pair_patterns(items, sizes, rotations)
    if patterns is None:
        return None
    remaining = [max(1, item.qty) for item in items]
    selected: list[
        tuple[
            int,
            tuple[int, ...],
            tuple[tuple[float, float], ...],
            float,
            float,
            float,
        ]
    ] = []

    while any(remaining):
        best = None
        best_key: tuple[float, float, int, float] | None = None
        for pattern in patterns:
            size_index, occ_indices, occ_orientations, _, utilization, waste = pattern
            needed: dict[int, int] = defaultdict(int)
            for item_index in occ_indices:
                needed[item_index] += 1
            if any(remaining[item_index] < count for item_index, count in needed.items()):
                continue
            key = (
                -utilization,
                waste,
                -len(occ_indices),
                sizes[size_index][0] * sizes[size_index][1],
            )
            if best_key is None or key < best_key:
                best_key = key
                best = pattern

        if best is None:
            return None

        selected.append(best)
        size_index, occ_indices, occ_orientations, _, _, _ = best
        for item_index in occ_indices:
            remaining[item_index] -= 1

    selected = _improve_selected_patterns(selected, items, sizes, rotations)

    sheets: list[Sheet] = []
    next_id = start_id
    sheet_index = 1
    for size_index, occ_indices, occ_orientations, _, _, _ in selected:
        sheet_width, sheet_height = sizes[size_index]
        parts: list[Part] = []
        cursor_x = 0.0
        for item_index, (width, height) in zip(occ_indices, occ_orientations):
            item = items[item_index]
            polygon = box(
                cursor_x,
                0.0,
                cursor_x + width,
                height,
            )
            number = f"{item.material or 'P'}-{next_id:04d}"
            parts.append(
                Part(
                    id=next_id,
                    number=number,
                    polygon=polygon,
                    outer_polygon=polygon,
                    material_group=item.material or None,
                    area=item.length_mm * item.width_mm,
                    label_position=(cursor_x + width / 2.0, height / 2.0),
                )
            )
            next_id += 1
            cursor_x += width
        sheets.append(
            Sheet(
                index=sheet_index,
                width=sheet_width,
                height=sheet_height,
                thickness=thickness_mm,
                parts=parts,
            )
        )
        sheet_index += 1

    total_part_area = sum(
        item.length_mm * item.width_mm * max(1, item.qty) for item in items
    )
    total_sheet_area = sum(sheet.total_area for sheet in sheets)
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(item.qty for item in items),
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


_EXACT_RESIDUAL_LIMIT = 48
_BULK_MIN_MULTIPLICITY = 2
_MAX_PAIR_PATTERNS = 5000
_RARE_LARGE_QTY = 2


def _pack_group_pairing_spread(
    items: list[ListItem],
    start_id: int,
    sizes: list[tuple[float, float]],
    thickness_mm: float,
    rotations: tuple[int, ...],
) -> NestingResult | None:
    """将数量极少的大件单张放置，其余大件仍走配对排板。

    罕见大件配对成双时会几乎占满整张大板，反而挤压窄条余料；单张放置
    虽多开一张大板，但留下的连续余料往往能让后续窄条少开更多小板。
    """
    if not items:
        return None

    flats: list[ListItem] = []
    for item in items:
        flats.extend([item] * max(1, item.qty))

    def type_key(item: ListItem) -> tuple[float, float, str]:
        return round(item.length_mm), round(item.width_mm), item.material or ""

    type_counts = Counter(type_key(item) for item in flats)
    rare_keys = {
        key for key, count in type_counts.items() if count == _RARE_LARGE_QTY
    }
    rare_flat = [item for item in flats if type_key(item) in rare_keys]
    regular_flat = [item for item in flats if type_key(item) not in rare_keys]

    if not rare_flat:
        return None

    rare_sheets: list[Sheet] = []
    next_id = start_id
    for item in rare_flat:
        fitting_sizes = [
            size
            for size in sizes
            if _item_fits(item, size[0], size[1], rotations)
        ]
        if not fitting_sizes:
            return None
        sheet_width, sheet_height = min(
            fitting_sizes, key=lambda size: size[0] * size[1]
        )
        orientation = next(
            (width, height)
            for width, height in _orientations(item, rotations)
            if width <= sheet_width + 1e-6 and height <= sheet_height + 1e-6
        )
        polygon = box(0.0, 0.0, orientation[0], orientation[1])
        rare_sheets.append(
            Sheet(
                index=0,
                width=sheet_width,
                height=sheet_height,
                thickness=thickness_mm,
                parts=[
                    Part(
                        id=next_id,
                        number=f"{item.material or 'P'}-{next_id:04d}",
                        polygon=polygon,
                        outer_polygon=polygon,
                        material_group=item.material or None,
                        area=item.length_mm * item.width_mm,
                        label_position=(
                            orientation[0] / 2.0,
                            orientation[1] / 2.0,
                        ),
                    )
                ],
            )
        )
        next_id += 1

    regular_counts: Counter[tuple[float, float, str]] = Counter(
        type_key(item) for item in regular_flat
    )
    regular_items = [
        ListItem(material, length, width, qty, "spread")
        for (length, width, material), qty in regular_counts.items()
    ]
    regular_result = _pack_group_pairing_target_like(
        regular_items,
        start_id=next_id,
        sizes=sizes,
        thickness_mm=thickness_mm,
        rotations=rotations,
    )
    if regular_result is None:
        return None

    sheets = rare_sheets + list(regular_result.sheets)
    total_part_area = sum(
        item.length_mm * item.width_mm * max(1, item.qty) for item in items
    )
    total_sheet_area = sum(sheet.total_area for sheet in sheets)
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(item.qty for item in items),
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


def _pack_group_pairing_target_like(
    items: list[ListItem],
    start_id: int,
    sizes: list[tuple[float, float]],
    thickness_mm: float,
    rotations: tuple[int, ...],
) -> NestingResult | None:
    """构造类似仁霸参考方案的大板配对：同短边先高矮混排，方板走小板。"""
    if not items:
        return None

    next_id = start_id
    sheets: list[Sheet] = []

    max_sheet_width = max(size[0] for size in sizes)
    max_sheet_height = max(size[1] for size in sizes)

    def piece_orientation(item: ListItem) -> tuple[float, float]:
        candidates = [
            (width, height)
            for width, height in _orientations(item, rotations)
            if 2.0 * width <= max_sheet_width + 1e-6
            and height <= max_sheet_height + 1e-6
        ]
        return max(candidates, key=lambda size: (size[0], size[1]))

    def best_sheet(piece_sizes: list[tuple[float, float]]) -> tuple[float, float]:
        usable = []
        for sheet_width, sheet_height in sizes:
            total_width = sum(width for width, _ in piece_sizes)
            max_height = max((height for _, height in piece_sizes), default=0.0)
            if total_width <= sheet_width + 1e-6 and max_height <= sheet_height + 1e-6:
                usable.append((sheet_width, sheet_height))
        return min(usable, key=lambda size: size[0] * size[1])

    def make_pair_sheet(
        first: ListItem,
        first_size: tuple[float, float],
        second: ListItem,
        second_size: tuple[float, float],
    ) -> Sheet:
        nonlocal next_id
        sheet_width, sheet_height = best_sheet([first_size, second_size])
        parts: list[Part] = []
        cursor_x = 0.0
        for item, (width, height) in (
            (first, first_size),
            (second, second_size),
        ):
            polygon = box(cursor_x, 0.0, cursor_x + width, height)
            parts.append(
                Part(
                    id=next_id,
                    number=f"{item.material or 'P'}-{next_id:04d}",
                    polygon=polygon,
                    outer_polygon=polygon,
                    material_group=item.material or None,
                    area=item.length_mm * item.width_mm,
                    label_position=(cursor_x + width / 2.0, height / 2.0),
                )
            )
            next_id += 1
            cursor_x += width
        return Sheet(
            index=0,
            width=sheet_width,
            height=sheet_height,
            thickness=thickness_mm,
            parts=parts,
        )

    squares: list[tuple[ListItem, tuple[float, float], int]] = []
    nonsquares: defaultdict[
        float, list[tuple[ListItem, tuple[float, float], int]]
    ] = defaultdict(list)
    for item in items:
        orientation = piece_orientation(item)
        width, height = orientation
        if abs(width - height) < 1e-6:
            squares.append((item, orientation, max(1, item.qty)))
        else:
            nonsquares[width].append((item, orientation, max(1, item.qty)))

    for side in sorted(nonsquares):
        group = sorted(
            nonsquares[side], key=lambda entry: entry[1][1], reverse=True
        )
        while group and len(group) >= 2 and group[-1][2] == 1 and squares:
            short_item, short_size, short_qty = group.pop()
            square_item, square_size, square_qty = squares.pop()
            sheets.append(
                make_pair_sheet(short_item, short_size, square_item, square_size)
            )
            if square_qty > 1:
                squares.append((square_item, square_size, square_qty - 1))
        if not group:
            continue

        while len(group) >= 2 and group[0][1][1] != group[-1][1][1]:
            tall_item, tall_size, tall_qty = group[0]
            short_item, short_size, short_qty = group[-1]
            if tall_size[1] == side or short_size[1] == side:
                break
            take = min(tall_qty, short_qty)
            for _ in range(take):
                sheets.append(
                    make_pair_sheet(tall_item, tall_size, short_item, short_size)
                )
            if tall_qty == take:
                group.pop(0)
            else:
                group[0] = (tall_item, tall_size, tall_qty - take)
            if short_qty == take:
                group.pop()
            else:
                group[-1] = (short_item, short_size, short_qty - take)

        for item, size, qty in group:
            while qty >= 2:
                sheets.append(make_pair_sheet(item, size, item, size))
                qty -= 2
            if qty == 1 and squares:
                square_item, square_size, square_qty = squares.pop()
                sheets.append(
                    make_pair_sheet(item, size, square_item, square_size)
                )
                if square_qty > 1:
                    squares.append((square_item, square_size, square_qty - 1))
            elif qty == 1:
                sheet_width, sheet_height = best_sheet([size])
                polygon = box(0.0, 0.0, size[0], size[1])
                sheets.append(
                    Sheet(
                        index=0,
                        width=sheet_width,
                        height=sheet_height,
                        thickness=thickness_mm,
                        parts=[
                            Part(
                                id=next_id,
                                number=f"{item.material or 'P'}-{next_id:04d}",
                                polygon=polygon,
                                outer_polygon=polygon,
                                material_group=item.material or None,
                                area=item.length_mm * item.width_mm,
                                label_position=(size[0] / 2.0, size[1] / 2.0),
                            )
                        ],
                    )
                )
                next_id += 1

    square_by_side: defaultdict[float, list[tuple[ListItem, tuple[float, float], int]]] = defaultdict(list)
    for item, size, qty in squares:
        square_by_side[size[0]].append((item, size, qty))
    for side in sorted(square_by_side):
        entries = square_by_side[side]
        for item, size, qty in entries:
            while qty >= 2:
                sheets.append(make_pair_sheet(item, size, item, size))
                qty -= 2
            if qty == 1:
                sheet_width, sheet_height = best_sheet([size])
                polygon = box(0.0, 0.0, size[0], size[1])
                sheets.append(
                    Sheet(
                        index=0,
                        width=sheet_width,
                        height=sheet_height,
                        thickness=thickness_mm,
                        parts=[
                            Part(
                                id=next_id,
                                number=f"{item.material or 'P'}-{next_id:04d}",
                                polygon=polygon,
                                outer_polygon=polygon,
                                material_group=item.material or None,
                                area=item.length_mm * item.width_mm,
                                label_position=(size[0] / 2.0, size[1] / 2.0),
                            )
                        ],
                    )
                )
                next_id += 1

    total_part_area = sum(
        item.length_mm * item.width_mm * max(1, item.qty) for item in items
    )
    total_sheet_area = sum(sheet.total_area for sheet in sheets)
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(item.qty for item in items),
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


def _mix_large_sheets_for_strips(sheets: list[Sheet]) -> list[Sheet]:
    """把“高+高”与“矮+矮”的同短边大板两两交换成“高+矮”混合板。

    混合板虽然单张利用率略降，但会留下可放窄条的连续余料，尤其适合
    大面积件排完后继续填宽窄条的场景。
    """
    if not sheets:
        return sheets

    def part_size(part: Part) -> tuple[int, int]:
        min_x, min_y, max_x, max_y = part.outer_polygon.bounds
        return round(max_x - min_x), round(max_y - min_y)

    def make_sheet(template: Sheet, left: Part, right: Part, width: int) -> Sheet:
        left_w, left_h = part_size(left)
        right_w, right_h = part_size(right)
        left_polygon = box(0.0, 0.0, left_w, left_h)
        right_polygon = box(float(width), 0.0, float(width) + right_w, right_h)

        def clone(part: Part, polygon: Any, label: tuple[float, float]) -> Part:
            return Part(
                id=part.id,
                number=part.number,
                polygon=polygon,
                outer_polygon=polygon,
                material_group=part.material_group,
                area=part.area,
                label_position=label,
            )

        return Sheet(
            index=0,
            width=template.width,
            height=template.height,
            thickness=template.thickness,
            parts=[
                clone(
                    left,
                    left_polygon,
                    (left_w / 2.0, left_h / 2.0),
                ),
                clone(
                    right,
                    right_polygon,
                    (float(width) + right_w / 2.0, right_h / 2.0),
                ),
            ],
        )

    groups: defaultdict[tuple[int, int, int, int], list[Sheet]] = defaultdict(list)
    untouched: list[Sheet] = []
    for sheet in sheets:
        if len(sheet.parts) != 2:
            untouched.append(sheet)
            continue
        left, right = sheet.parts
        left_size = part_size(left)
        right_size = part_size(right)
        if left_size[0] == right_size[0] and left_size[1] == right_size[1]:
            groups[
                (
                    round(sheet.width),
                    round(sheet.height),
                    left_size[0],
                    left_size[1],
                )
            ].append(sheet)
        else:
            untouched.append(sheet)

    result = list(untouched)
    triples = sorted(
        {(sheet_width, sheet_height, width) for sheet_width, sheet_height, width, _ in groups}
    )
    for sheet_width, sheet_height, width in triples:
        heights = sorted(
            {
                size[3]
                for size in groups
                if size[0] == sheet_width
                and size[1] == sheet_height
                and size[2] == width
            }
        )
        while len(heights) >= 2:
            tall_height = heights[-1]
            short_height = heights[0]
            if tall_height == short_height:
                break
            if short_height == width or tall_height == width:
                break
            tall_sheets = groups.get((sheet_width, sheet_height, width, tall_height), [])
            short_sheets = groups.get((sheet_width, sheet_height, width, short_height), [])
            if not tall_sheets or not short_sheets:
                break
            tall_sheet = tall_sheets.pop()
            short_sheet = short_sheets.pop()
            tall_left, tall_right = tall_sheet.parts
            short_left, short_right = short_sheet.parts
            result.append(
                make_sheet(tall_sheet, tall_left, short_left, width)
            )
            result.append(
                make_sheet(tall_sheet, tall_right, short_right, width)
            )
            if not tall_sheets:
                heights = [value for value in heights if value != tall_height]
            if not short_sheets:
                heights = [value for value in heights if value != short_height]

    for group_sheets in groups.values():
        result.extend(group_sheets)
    return result


def _pattern_signature(
    pattern: tuple[
        int,
        tuple[int, ...],
        tuple[tuple[float, float], ...],
        float,
        float,
        float,
    ],
) -> tuple[int, tuple[tuple[int, tuple[float, float]], ...]]:
    size_index, occ_indices, occ_orientations, _, _, _ = pattern
    return size_index, tuple(sorted(zip(occ_indices, occ_orientations)))


def _split_bulk_and_residual(
    selected: list[
        tuple[
            int,
            tuple[int, ...],
            tuple[tuple[float, float], ...],
            float,
            float,
            float,
        ]
    ],
    items: list[ListItem],
) -> tuple[
    list[
        tuple[
            int,
            tuple[int, ...],
            tuple[tuple[float, float], ...],
            float,
            float,
            float,
        ]
    ],
    list[
        tuple[
            int,
            tuple[int, ...],
            tuple[tuple[float, float], ...],
            float,
            float,
            float,
        ]
    ],
    list[int],
]:
    """把重复出现的大批量模式先固定，只对零散小批量做精确搜索。"""
    signature_counts = Counter(_pattern_signature(pattern) for pattern in selected)
    committed = [
        pattern
        for pattern in selected
        if signature_counts[_pattern_signature(pattern)] >= _BULK_MIN_MULTIPLICITY
    ]
    residual = [
        pattern
        for pattern in selected
        if signature_counts[_pattern_signature(pattern)] < _BULK_MIN_MULTIPLICITY
    ]

    remaining = [max(1, item.qty) for item in items]
    for pattern in committed:
        for item_index in pattern[1]:
            remaining[item_index] -= 1

    return committed, residual, remaining


def _exact_residual_patterns(
    items: list[ListItem],
    sizes: list[tuple[float, float]],
    rotations: tuple[int, ...],
    remaining: list[int],
) -> tuple[
    list[
        tuple[
            int,
            tuple[int, ...],
            tuple[tuple[float, float], ...],
            float,
            float,
            float,
        ]
    ],
    tuple[float, int],
] | None:
    """对少量剩余零件做带记忆的回溯搜索，返回精确最优的板面积/张数与模式。"""
    if sum(remaining) <= 0 or sum(remaining) > _EXACT_RESIDUAL_LIMIT:
        return None

    patterns = _build_pair_patterns(items, sizes, rotations)
    if patterns is None:
        return None
    initial_state = tuple(remaining)
    choices: dict[tuple[int, ...], int] = {}

    @lru_cache(None)
    def search(state: tuple[int, ...]) -> tuple[float, int] | None:
        if not any(state):
            return 0.0, 0

        first_index = next(index for index, count in enumerate(state) if count)
        best: tuple[float, int] | None = None
        best_pattern_index: int | None = None

        for pattern_index, pattern in enumerate(patterns):
            size_index, occ_indices, _, _, _, _ = pattern
            needed = Counter(occ_indices)
            if needed.get(first_index, 0) == 0:
                continue
            if any(state[index] < count for index, count in needed.items()):
                continue

            next_state = list(state)
            for index, count in needed.items():
                next_state[index] -= count

            child = search(tuple(next_state))
            if child is None:
                continue

            sheet_area = sizes[size_index][0] * sizes[size_index][1]
            candidate = child[0] + sheet_area, child[1] + 1
            if best is None or candidate < best:
                best = candidate
                best_pattern_index = pattern_index

        if best_pattern_index is not None:
            choices[state] = best_pattern_index
        return best

    best_result = search(initial_state)
    if best_result is None:
        return None

    state = initial_state
    selected: list[
        tuple[
            int,
            tuple[int, ...],
            tuple[tuple[float, float], ...],
            float,
            float,
            float,
        ]
    ] = []
    while any(state):
        pattern = patterns[choices[state]]
        selected.append(pattern)
        next_state = list(state)
        for item_index, count in Counter(pattern[1]).items():
            next_state[item_index] -= count
        state = tuple(next_state)

    return selected, best_result


def _improve_selected_patterns(
    selected: list[
        tuple[
            int,
            tuple[int, ...],
            tuple[tuple[float, float], ...],
            float,
            float,
            float,
        ]
    ],
    items: list[ListItem],
    sizes: list[tuple[float, float]],
    rotations: tuple[int, ...],
) -> list[
    tuple[
        int,
        tuple[int, ...],
        tuple[tuple[float, float], ...],
        float,
        float,
        float,
    ]
]:
    committed, residual_greedy, remaining = _split_bulk_and_residual(
        selected, items
    )
    if not residual_greedy:
        return selected

    exact = _exact_residual_patterns(items, sizes, rotations, remaining)
    if exact is None:
        return selected

    exact_patterns, (exact_area, exact_sheets) = exact
    greedy_area = sum(
        sizes[pattern[0]][0] * sizes[pattern[0]][1] for pattern in residual_greedy
    )
    greedy_sheets = len(residual_greedy)
    if (exact_area, exact_sheets) < (greedy_area, greedy_sheets):
        return committed + exact_patterns
    return selected


_STRIP_MIN_SIDE = 200.0


def _is_strip_item(item: ListItem) -> bool:
    return min(item.length_mm, item.width_mm) < _STRIP_MIN_SIDE


def _subtract_occupied_rect(
    free_rects: list[tuple[float, float, float, float]],
    occupied: tuple[float, float, float, float],
) -> list[tuple[float, float, float, float]]:
    result: list[tuple[float, float, float, float]] = []
    occupied_x, occupied_y, occupied_width, occupied_height = occupied
    for free_x, free_y, free_width, free_height in free_rects:
        if (
            occupied_x >= free_x + free_width - 1e-6
            or occupied_x + occupied_width <= free_x + 1e-6
            or occupied_y >= free_y + free_height - 1e-6
            or occupied_y + occupied_height <= free_y + 1e-6
        ):
            result.append((free_x, free_y, free_width, free_height))
            continue

        if occupied_y > free_y + 1e-6:
            result.append(
                (free_x, free_y, free_width, occupied_y - free_y)
            )
        if occupied_y + occupied_height < free_y + free_height - 1e-6:
            result.append(
                (
                    free_x,
                    occupied_y + occupied_height,
                    free_width,
                    free_y + free_height - (occupied_y + occupied_height),
                )
            )
        middle_height = min(
            occupied_y + occupied_height,
            free_y + free_height,
        ) - max(occupied_y, free_y)
        if occupied_x > free_x + 1e-6 and middle_height > 1e-6:
            result.append(
                (
                    free_x,
                    max(occupied_y, free_y),
                    occupied_x - free_x,
                    middle_height,
                )
            )
        if occupied_x + occupied_width < free_x + free_width - 1e-6 and middle_height > 1e-6:
            result.append(
                (
                    occupied_x + occupied_width,
                    max(occupied_y, free_y),
                    free_x + free_width - (occupied_x + occupied_width),
                    middle_height,
                )
            )

    return [
        rect
        for rect in result
        if rect[2] > 1e-6 and rect[3] > 1e-6
    ]


def _best_strip_subset(
    counts: dict[int, int],
    capacity: int,
) -> tuple[list[int], int]:
    """从剩余窄条长度计数中选一组总长不超过 capacity 的最优子集。"""
    capacity = int(capacity)
    dp = [-1] * (capacity + 1)
    dp[0] = 0
    parent: list[tuple[int, int, int] | None] = [None] * (capacity + 1)

    for length, available in counts.items():
        if available <= 0 or length > capacity:
            continue
        remaining = available
        chunk_size = 1
        while remaining > 0:
            chunk = min(chunk_size, remaining)
            weight = length * chunk
            if weight > capacity:
                chunk = capacity // length
                if chunk <= 0:
                    break
                weight = length * chunk
            for current in range(capacity, weight - 1, -1):
                previous = current - weight
                if dp[previous] < 0:
                    continue
                candidate = dp[previous] + weight
                if candidate > dp[current]:
                    dp[current] = candidate
                    parent[current] = (previous, chunk, length)
            remaining -= chunk
            chunk_size *= 2

    best_capacity = max(range(capacity + 1), key=lambda index: dp[index])
    best_sum = dp[best_capacity]
    chosen: list[int] = []
    current = best_capacity
    while current and parent[current] is not None:
        previous, chunk, length = parent[current]
        chosen.extend([length] * chunk)
        current = previous
    return chosen, best_sum


def _best_strip_subset_power(
    counts: dict[int, int],
    capacity: int,
    power: float,
) -> tuple[list[int], int]:
    """同 `_best_strip_subset`，但评分优先长条（长度 ** power）。"""
    capacity = int(capacity)
    neg = (-1e30, -1e30)
    dp = [neg] * (capacity + 1)
    dp[0] = (0.0, 0)
    parent: list[tuple[int, int, int] | None] = [None] * (capacity + 1)

    for length, available in counts.items():
        if available <= 0 or length > capacity:
            continue
        remaining = available
        chunk_size = 1
        while remaining > 0:
            chunk = min(chunk_size, remaining)
            weight = length * chunk
            if weight > capacity:
                chunk = capacity // length
                if chunk <= 0:
                    break
                weight = length * chunk
            value = (float(length) ** power) * chunk
            for current in range(capacity, weight - 1, -1):
                previous = current - weight
                if dp[previous][0] < 0:
                    continue
                candidate = (dp[previous][0] + value, dp[previous][1] + weight)
                if candidate > dp[current]:
                    dp[current] = candidate
                    parent[current] = (previous, chunk, length)
            remaining -= chunk
            chunk_size *= 2

    best_capacity = max(range(capacity + 1), key=lambda index: dp[index])
    chosen: list[int] = []
    current = best_capacity
    while current and parent[current] is not None:
        previous, chunk, length = parent[current]
        chosen.extend([length] * chunk)
        current = previous
    return chosen, dp[best_capacity][1]


def _greedy_strip_subset(
    counts: dict[int, int],
    capacity: int,
) -> tuple[list[int], int]:
    """快速贪心选择窄条长度，优先放入长条，避免大量自由矩形上的 DP。"""
    capacity = int(capacity)
    chosen: list[int] = []
    total = 0
    for length in sorted(counts, reverse=True):
        available = counts[length]
        if available <= 0 or length > capacity:
            continue
        take = min(available, (capacity - total) // length)
        if take <= 0:
            continue
        chosen.extend([length] * take)
        total += length * take
        if total == capacity:
            break
    return chosen, total


def _fill_free_rect_renba(
    counts: dict[int, int],
    rect: tuple[float, float, float, float],
    power: float,
    lane_width: float = 95.0,
) -> tuple[dict[int, int], list[list[int]], list[list[int]]]:
    """在一个自由矩形内做“上方横排 + 下方竖列”的窄条填充。"""
    free_width = rect[2]
    free_height = rect[3]
    best_score = -1
    best_used: dict[int, int] = {}
    best_rows: list[list[int]] = []
    best_cols: list[list[int]] = []

    for row_count in range(int(free_height // lane_width) + 1):
        counts_after_rows = dict(counts)
        used_rows: dict[int, int] = {}
        rows: list[list[int]] = []
        rows_ok = True
        for _ in range(row_count):
            lane_counts = {
                length: count
                for length, count in counts_after_rows.items()
                if count > 0
            }
            chosen, _ = _best_strip_subset_power(
                lane_counts, int(free_width), power
            )
            if not chosen:
                rows_ok = False
                break
            rows.append(chosen)
            for length in chosen:
                used_rows[length] = used_rows.get(length, 0) + 1
                counts_after_rows[length] -= 1
                if counts_after_rows[length] <= 0:
                    del counts_after_rows[length]
        if not rows_ok:
            continue

        bottom_height = free_height - row_count * lane_width
        for col_count in range(int(free_width // lane_width) + 1):
            counts_after_cols = dict(counts_after_rows)
            used_cols = dict(used_rows)
            cols: list[list[int]] = []
            cols_ok = True
            for _ in range(col_count):
                lane_counts = {
                    length: count
                    for length, count in counts_after_cols.items()
                    if count > 0
                }
                chosen, _ = _best_strip_subset_power(
                    lane_counts, int(bottom_height), power
                )
                if not chosen:
                    cols_ok = False
                    break
                cols.append(chosen)
                for length in chosen:
                    used_cols[length] = used_cols.get(length, 0) + 1
                    counts_after_cols[length] -= 1
                    if counts_after_cols[length] <= 0:
                        del counts_after_cols[length]
            if not cols_ok:
                continue

            score = sum(length * count for length, count in used_cols.items())
            if score > best_score:
                best_score = score
                best_used = used_cols
                best_rows = rows
                best_cols = cols

    return best_used, best_rows, best_cols


def _fill_strips_into_sheets_renba(
    sheets: list[Sheet],
    strip_items: list[ListItem],
    next_id: int,
    rotations: tuple[int, ...],
) -> tuple[list[ListItem], int]:
    """窄条填充增强：对统一短边窄条使用仁霸式横排+竖列余料填充。"""
    flat_strips: list[ListItem] = []
    for item in strip_items:
        flat_strips.extend([item] * max(1, item.qty))
    if not flat_strips:
        return [], next_id

    side_counts: dict[int, int] = defaultdict(int)
    for item in flat_strips:
        side_counts[round(min(item.length_mm, item.width_mm))] += 1

    dominant_side = max(side_counts, key=lambda side: (side_counts[side], -side))
    uniform_strips = [
        item
        for item in flat_strips
        if round(min(item.length_mm, item.width_mm)) == dominant_side
    ]
    other_strips = [
        item
        for item in flat_strips
        if round(min(item.length_mm, item.width_mm)) != dominant_side
    ]

    if len(uniform_strips) < 2:
        return _fill_strips_into_sheets_legacy(
            sheets, flat_strips, next_id, rotations
        )

    remaining, next_id = _fill_strips_into_sheets_renba_uniform(
        sheets,
        uniform_strips,
        next_id,
        rotations,
        float(dominant_side),
        power=1.5,
    )
    remaining, next_id = _fill_strips_into_sheets_renba_uniform(
        sheets,
        remaining,
        next_id,
        rotations,
        float(dominant_side),
        power=1.0,
    )
    remaining, next_id = _fill_strips_into_sheets_renba_uniform(
        sheets,
        remaining,
        next_id,
        rotations,
        float(dominant_side),
        power=1.0,
    )
    other_strips.extend(remaining)
    return other_strips, next_id


def _fill_strips_into_sheets_renba_uniform(
    sheets: list[Sheet],
    flat_strips: list[ListItem],
    next_id: int,
    rotations: tuple[int, ...],
    strip_width: float,
    power: float = 1.0,
) -> tuple[list[ListItem], int]:
    free_by_sheet: list[list[tuple[float, float, float, float]]] = []
    for sheet in sheets:
        free = [(0.0, 0.0, sheet.width, sheet.height)]
        for part in sheet.parts:
            min_x, min_y, max_x, max_y = part.outer_polygon.bounds
            free = _subtract_occupied_rect(
                free,
                (min_x, min_y, max_x - min_x, max_y - min_y),
            )
        free_by_sheet.append(free)

    free_rects: list[tuple[int, tuple[float, float, float, float]]] = []
    for sheet_index, free_list in enumerate(free_by_sheet):
        for rect in free_list:
            _, _, width, height = rect
            if width >= strip_width - 1e-6 and height >= strip_width - 1e-6:
                free_rects.append((sheet_index, rect))
    free_rects.sort(key=lambda item: item[1][2] * item[1][3], reverse=True)

    remaining = list(flat_strips)
    current_id = next_id

    def pop_item(length: int) -> ListItem | None:
        for index, item in enumerate(remaining):
            long_side = round(max(item.length_mm, item.width_mm))
            if long_side != length:
                continue
            return remaining.pop(index)
        return None

    for sheet_index, (free_x, free_y, free_width, free_height) in free_rects:
        counts: dict[int, int] = {}
        for item in remaining:
            length = round(max(item.length_mm, item.width_mm))
            counts[length] = counts.get(length, 0) + 1
        if not counts:
            break

        used, rows, cols = _fill_free_rect_renba(
            counts,
            (free_x, free_y, free_width, free_height),
            power,
            lane_width=strip_width,
        )
        if not used:
            continue

        for row_index, lengths in enumerate(rows):
            cursor_x = free_x
            cursor_y = free_y + row_index * strip_width
            for length in lengths:
                item = pop_item(length)
                if item is None:
                    continue
                polygon = box(
                    cursor_x,
                    cursor_y,
                    cursor_x + length,
                    cursor_y + strip_width,
                )
                sheets[sheet_index].parts.append(
                    Part(
                        id=current_id,
                        number=f"{item.material or 'P'}-{current_id:04d}",
                        polygon=polygon,
                        outer_polygon=polygon,
                        material_group=item.material or None,
                        area=item.length_mm * item.width_mm,
                        label_position=(
                            cursor_x + length / 2.0,
                            cursor_y + strip_width / 2.0,
                        ),
                    )
                )
                current_id += 1
                cursor_x += length

        row_zone_height = len(rows) * strip_width
        for col_index, lengths in enumerate(cols):
            cursor_x = free_x + col_index * strip_width
            cursor_y = free_y + row_zone_height
            for length in lengths:
                item = pop_item(length)
                if item is None:
                    continue
                polygon = box(
                    cursor_x,
                    cursor_y,
                    cursor_x + strip_width,
                    cursor_y + length,
                )
                sheets[sheet_index].parts.append(
                    Part(
                        id=current_id,
                        number=f"{item.material or 'P'}-{current_id:04d}",
                        polygon=polygon,
                        outer_polygon=polygon,
                        material_group=item.material or None,
                        area=item.length_mm * item.width_mm,
                        label_position=(
                            cursor_x + strip_width / 2.0,
                            cursor_y + length / 2.0,
                        ),
                    )
                )
                current_id += 1
                cursor_y += length

    return remaining, current_id


def _fill_strips_into_sheets_uniform(
    sheets: list[Sheet],
    flat_strips: list[ListItem],
    next_id: int,
    rotations: tuple[int, ...],
    strip_width: float,
) -> tuple[list[ListItem], int]:
    """统一窄条短边时，先按自由矩形做行/列装箱，尽量填满大板余料。"""
    free_by_sheet: list[list[tuple[float, float, float, float]]] = []
    for sheet in sheets:
        free = [(0.0, 0.0, sheet.width, sheet.height)]
        for part in sheet.parts:
            min_x, min_y, max_x, max_y = part.outer_polygon.bounds
            free = _subtract_occupied_rect(
                free,
                (min_x, min_y, max_x - min_x, max_y - min_y),
            )
        free_by_sheet.append(free)

    free_rects: list[tuple[int, tuple[float, float, float, float]]] = []
    for sheet_index, free_list in enumerate(free_by_sheet):
        for rect in free_list:
            _, _, width, height = rect
            if width >= strip_width - 1e-6 and height >= strip_width - 1e-6:
                free_rects.append((sheet_index, rect))
    free_rects.sort(key=lambda item: item[1][2] * item[1][3], reverse=True)

    remaining = list(flat_strips)
    current_id = next_id

    def remove_lengths(lengths: list[int], capacity: float, axis: str) -> list[ListItem]:
        used: list[ListItem] = []
        for length in lengths:
            for index, item in enumerate(remaining):
                if round(max(item.length_mm, item.width_mm)) != length:
                    continue
                if axis == "h":
                    width, height = length, strip_width
                else:
                    width, height = strip_width, length
                if width <= capacity + 1e-6 and height <= capacity + 1e-6:
                    used.append(remaining.pop(index))
                    break
        return used

    for sheet_index, (free_x, free_y, free_width, free_height) in free_rects:
        counts: dict[int, int] = {}
        for item in remaining:
            length = round(max(item.length_mm, item.width_mm))
            counts[length] = counts.get(length, 0) + 1
        if not counts:
            break

        best_mode = None
        best_bins: list[list[int]] = []
        best_used = -1
        for mode in ("horizontal", "vertical"):
            if mode == "horizontal":
                bin_count = int(free_height // strip_width)
                bin_capacity = int(free_width)
            else:
                bin_count = int(free_width // strip_width)
                bin_capacity = int(free_height)
            if bin_count <= 0 or bin_capacity <= 0:
                continue
            mode_counts = counts.copy()
            mode_bins: list[list[int]] = []
            mode_used = 0
            for _ in range(bin_count):
                chosen, used = _best_strip_subset(mode_counts, bin_capacity)
                mode_bins.append(chosen)
                mode_used += used
                for length in chosen:
                    mode_counts[length] -= 1
            if mode_used > best_used:
                best_mode = mode
                best_bins = mode_bins
                best_used = mode_used

        if best_mode is None or best_used <= 0:
            continue

        for bin_index, lengths in enumerate(best_bins):
            if best_mode == "horizontal":
                cursor_x = free_x
                cursor_y = free_y + bin_index * strip_width
                capacity = free_width
                for length in lengths:
                    item = remove_lengths([length], capacity, "h")
                    if not item:
                        continue
                    item = item[0]
                    polygon = box(
                        cursor_x,
                        cursor_y,
                        cursor_x + length,
                        cursor_y + strip_width,
                    )
                    part = Part(
                        id=current_id,
                        number=f"{item.material or 'P'}-{current_id:04d}",
                        polygon=polygon,
                        outer_polygon=polygon,
                        material_group=item.material or None,
                        area=item.length_mm * item.width_mm,
                        label_position=(cursor_x + length / 2.0, cursor_y + strip_width / 2.0),
                    )
                    sheets[sheet_index].parts.append(part)
                    current_id += 1
                    cursor_x += length
            else:
                cursor_x = free_x + bin_index * strip_width
                cursor_y = free_y
                capacity = free_height
                for length in lengths:
                    item = remove_lengths([length], capacity, "v")
                    if not item:
                        continue
                    item = item[0]
                    polygon = box(
                        cursor_x,
                        cursor_y,
                        cursor_x + strip_width,
                        cursor_y + length,
                    )
                    part = Part(
                        id=current_id,
                        number=f"{item.material or 'P'}-{current_id:04d}",
                        polygon=polygon,
                        outer_polygon=polygon,
                        material_group=item.material or None,
                        area=item.length_mm * item.width_mm,
                        label_position=(cursor_x + strip_width / 2.0, cursor_y + length / 2.0),
                    )
                    sheets[sheet_index].parts.append(part)
                    current_id += 1
                    cursor_y += length

    return remaining, current_id


def _fill_strips_into_sheets(
    sheets: list[Sheet],
    strip_items: list[ListItem],
    next_id: int,
    rotations: tuple[int, ...],
) -> tuple[list[ListItem], int]:
    flat_strips: list[ListItem] = []
    for item in strip_items:
        flat_strips.extend(
            [
                ListItem(item.material, item.length_mm, item.width_mm, 1, item.source)
                for _ in range(max(1, item.qty))
            ]
        )
    if not flat_strips:
        return [], next_id

    side_counts: dict[int, int] = defaultdict(int)
    for item in flat_strips:
        side_counts[round(min(item.length_mm, item.width_mm))] += 1

    dominant_side = max(side_counts, key=lambda side: (side_counts[side], -side))
    uniform_strips = [
        item
        for item in flat_strips
        if round(min(item.length_mm, item.width_mm)) == dominant_side
    ]
    other_strips = [
        item
        for item in flat_strips
        if round(min(item.length_mm, item.width_mm)) != dominant_side
    ]
    if len(uniform_strips) >= 2:
        remaining, next_id = _fill_strips_into_sheets_uniform(
            sheets,
            uniform_strips,
            next_id,
            rotations,
            float(dominant_side),
        )
        other_strips.extend(remaining)
        return _fill_strips_into_sheets_legacy(
            sheets,
            other_strips,
            next_id,
            rotations,
        )

    return _fill_strips_into_sheets_legacy(
        sheets,
        flat_strips,
        next_id,
        rotations,
    )


def _fill_strips_into_sheets_legacy(
    sheets: list[Sheet],
    strip_items: list[ListItem],
    next_id: int,
    rotations: tuple[int, ...],
) -> tuple[list[ListItem], int]:
    flat_strips: list[ListItem] = []
    for item in strip_items:
        flat_strips.extend([item] * max(1, item.qty))

    short_sides = {
        round(min(item.length_mm, item.width_mm))
        for item in flat_strips
    }
    if len(short_sides) == 1:
        return _fill_strips_into_sheets_uniform(
            sheets,
            flat_strips,
            next_id,
            rotations,
            float(next(iter(short_sides))),
        )

    free_by_sheet: list[list[tuple[float, float, float, float]]] = []
    for sheet in sheets:
        free = [(0.0, 0.0, sheet.width, sheet.height)]
        for part in sheet.parts:
            min_x, min_y, max_x, max_y = part.outer_polygon.bounds
            free = _subtract_occupied_rect(
                free,
                (min_x, min_y, max_x - min_x, max_y - min_y),
            )
        free_by_sheet.append(free)

    flat_strips.sort(
        key=lambda item: (
            min(item.length_mm, item.width_mm),
            max(item.length_mm, item.width_mm),
        )
    )

    remaining: list[ListItem] = []
    for item in flat_strips:
        best = None
        best_key: tuple[float, float, float, float] | None = None
        for sheet_index, free_rects in enumerate(free_by_sheet):
            for free_rect in free_rects:
                free_x, free_y, free_width, free_height = free_rect
                for width, height in _orientations(item, rotations):
                    if width > free_width + 1e-6 or height > free_height + 1e-6:
                        continue
                    waste = free_width * free_height - width * height
                    key = (
                        waste,
                        free_y,
                        free_x,
                        width + height,
                    )
                    if best_key is None or key < best_key:
                        best_key = key
                        best = (
                            sheet_index,
                            free_x,
                            free_y,
                            width,
                            height,
                        )

        if best is None:
            remaining.append(item)
            continue

        sheet_index, x, y, width, height = best
        polygon = box(x, y, x + width, y + height)
        sheets[sheet_index].parts.append(
            Part(
                id=next_id,
                number=f"{item.material or 'P'}-{next_id:04d}",
                polygon=polygon,
                outer_polygon=polygon,
                material_group=item.material or None,
                area=item.length_mm * item.width_mm,
                label_position=(x + width / 2.0, y + height / 2.0),
            )
        )
        next_id += 1
        free_by_sheet[sheet_index] = _subtract_occupied_rect(
            free_by_sheet[sheet_index],
            (x, y, width, height),
        )

    return remaining, next_id


def _pack_group_large_then_strips(
    items: list[ListItem],
    start_id: int,
    sizes: list[tuple[float, float]],
    thickness_mm: float,
    rotations: tuple[int, ...],
) -> NestingResult | None:
    large_items = [item for item in items if not _is_strip_item(item)]
    strip_items = [item for item in items if _is_strip_item(item)]
    if not large_items or not strip_items:
        return None

    large_result = _pack_group_pairing(
        large_items,
        start_id=start_id,
        sizes=sizes,
        thickness_mm=thickness_mm,
        rotations=rotations,
    )
    if large_result is None:
        return None

    sheets = list(large_result.sheets)
    next_id = start_id + sum(item.qty for item in large_items)
    remaining_strips, next_id = _fill_strips_into_sheets(
        sheets,
        strip_items,
        next_id,
        rotations,
    )

    if remaining_strips:
        aggregated: defaultdict[tuple[float, float], int] = defaultdict(int)
        for item in remaining_strips:
            aggregated[(item.length_mm, item.width_mm)] += 1
        remaining_items = [
            ListItem("", length, width, qty, "residual")
            for (length, width), qty in aggregated.items()
        ]
        candidates = [
            _pack_group_with_preference(
                remaining_items,
                start_id=next_id,
                sizes=sizes,
                thickness_mm=thickness_mm,
                rotations=rotations,
                prefer_index=prefer_index,
                prefer_vertical_strips=True,
            )
            for prefer_index in range(len(sizes))
        ]
        residual_result = min(
            candidates,
            key=lambda result: (result.total_sheet_area, result.total_sheets),
        )
        sheets.extend(residual_result.sheets)

    for index, sheet in enumerate(sheets, start=1):
        sheet.index = index

    total_part_area = sum(
        item.length_mm * item.width_mm * max(1, item.qty) for item in items
    )
    total_sheet_area = sum(sheet.total_area for sheet in sheets)
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(item.qty for item in items),
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


def _pack_group_large_then_strips_v2(
    items: list[ListItem],
    start_id: int,
    sizes: list[tuple[float, float]],
    thickness_mm: float,
    rotations: tuple[int, ...],
) -> NestingResult | None:
    """大面积优先 + 仁霸式窄条余料填充的候选排板。"""
    large_items = [item for item in items if not _is_strip_item(item)]
    strip_items = [item for item in items if _is_strip_item(item)]
    if not large_items or not strip_items:
        return None

    large_result = _pack_group_pairing_spread(
        large_items,
        start_id=start_id,
        sizes=sizes,
        thickness_mm=thickness_mm,
        rotations=rotations,
    )
    if large_result is None:
        return None

    sheets = list(large_result.sheets)
    next_id = start_id + sum(item.qty for item in large_items)
    remaining_strips, next_id = _fill_strips_into_sheets_renba(
        sheets,
        strip_items,
        next_id,
        rotations,
    )

    if remaining_strips:
        aggregated: defaultdict[tuple[float, float], int] = defaultdict(int)
        for item in remaining_strips:
            aggregated[(item.length_mm, item.width_mm)] += 1
        remaining_items = [
            ListItem("", length, width, qty, "residual")
            for (length, width), qty in aggregated.items()
        ]
        candidates = [
            _pack_group_with_preference(
                remaining_items,
                start_id=next_id,
                sizes=sizes,
                thickness_mm=thickness_mm,
                rotations=rotations,
                prefer_index=prefer_index,
                prefer_vertical_strips=True,
            )
            for prefer_index in range(len(sizes))
        ]
        residual_result = min(
            candidates,
            key=lambda result: (result.total_sheet_area, result.total_sheets),
        )
        sheets.extend(residual_result.sheets)

    for index, sheet in enumerate(sheets, start=1):
        sheet.index = index

    total_part_area = sum(
        item.length_mm * item.width_mm * max(1, item.qty) for item in items
    )
    total_sheet_area = sum(sheet.total_area for sheet in sheets)
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(item.qty for item in items),
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


def _pack_group_large_then_strips_v3(
    items: list[ListItem],
    start_id: int,
    sizes: list[tuple[float, float]],
    thickness_mm: float,
    rotations: tuple[int, ...],
) -> NestingResult | None:
    """大面积优先 + 仁霸式余料填充 + 剩余窄条精确竖列 packer。"""
    large_items = [item for item in items if not _is_strip_item(item)]
    strip_items = [item for item in items if _is_strip_item(item)]
    if not large_items or not strip_items:
        return None

    large_result = _pack_group_pairing_spread(
        large_items,
        start_id=start_id,
        sizes=sizes,
        thickness_mm=thickness_mm,
        rotations=rotations,
    )
    if large_result is None:
        return None

    sheets = list(large_result.sheets)
    next_id = start_id + sum(item.qty for item in large_items)
    remaining_strips, next_id = _fill_strips_into_sheets_renba(
        sheets,
        strip_items,
        next_id,
        rotations,
    )

    if remaining_strips:
        residual_sheets, next_id = pack_residual_strips(
            remaining_strips,
            sizes,
            next_id,
            thickness_mm,
        )
        sheets.extend(residual_sheets)

    for index, sheet in enumerate(sheets, start=1):
        sheet.index = index

    total_part_area = sum(
        item.length_mm * item.width_mm * max(1, item.qty) for item in items
    )
    total_sheet_area = sum(sheet.total_area for sheet in sheets)
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=sum(item.qty for item in items),
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


def _result_uses_all_sizes(result: NestingResult, sizes: list[tuple[float, float]]) -> bool:
    used: set[tuple[float, float]] = set()
    for sheet in result.sheets:
        used.add((sheet.width, sheet.height))
    for width, height in sizes:
        if not any(
            abs(sheet_width - width) < 1e-6 and abs(sheet_height - height) < 1e-6
            for sheet_width, sheet_height in used
        ):
            return False
    return True


def _large_sheet_counts_asc(
    result: NestingResult,
    sizes: list[tuple[float, float]],
) -> tuple[int, ...]:
    """从最大板到最小板统计用板数，作为“小尺寸板优先”的 tie-break。"""
    size_order = sorted(
        sizes,
        key=lambda size: size[0] * size[1],
        reverse=True,
    )
    counts: list[int] = []
    for width, height in size_order:
        counts.append(
            sum(
                1
                for sheet in result.sheets
                if abs(sheet.width - width) < 1e-6
                and abs(sheet.height - height) < 1e-6
            )
        )
    return tuple(counts)


def _nest_group_rect_multi(
    items: list[ListItem],
    start_id: int,
    sizes: list[tuple[float, float]],
    thickness_mm: float,
    rotations: tuple[int, ...],
) -> NestingResult:
    """在多种大板间选择，优先最大出材率（最小总面积）。

    当只有两种大板时，额外枚举较小板的不同张数上限，以便在满足
    “两种板都要使用”的前提下寻找最高出材率。
    """
    candidates: list[NestingResult] = []
    pattern_result = _pack_group_pairing(
        items,
        start_id=start_id,
        sizes=sizes,
        thickness_mm=thickness_mm,
        rotations=rotations,
    )
    if pattern_result is not None:
        candidates.append(pattern_result)

    large_then_strips_result = _pack_group_large_then_strips(
        items,
        start_id=start_id,
        sizes=sizes,
        thickness_mm=thickness_mm,
        rotations=rotations,
    )
    if large_then_strips_result is not None:
        candidates.append(large_then_strips_result)

    large_then_strips_v2_result = _pack_group_large_then_strips_v2(
        items,
        start_id=start_id,
        sizes=sizes,
        thickness_mm=thickness_mm,
        rotations=rotations,
    )
    if large_then_strips_v2_result is not None:
        candidates.append(large_then_strips_v2_result)

    large_then_strips_v3_result = _pack_group_large_then_strips_v3(
        items,
        start_id=start_id,
        sizes=sizes,
        thickness_mm=thickness_mm,
        rotations=rotations,
    )
    if large_then_strips_v3_result is not None:
        candidates.append(large_then_strips_v3_result)

    for prefer_index in range(len(sizes)):
        candidates.append(
            _pack_group_with_preference(
                items,
                start_id=start_id,
                sizes=sizes,
                thickness_mm=thickness_mm,
                rotations=rotations,
                prefer_index=prefer_index,
            )
        )

    has_mixed_pattern = bool(
        pattern_result is not None
        and _result_uses_all_sizes(pattern_result, sizes)
    )
    if len(sizes) == 2 and not has_mixed_pattern:
        small_index = min(
            range(len(sizes)), key=lambda i: sizes[i][0] * sizes[i][1]
        )
        total_pieces = sum(item.qty for item in items)
        max_small = max(1, min(total_pieces, 300))
        step = 1 if total_pieces <= 300 else max(1, total_pieces // 100)
        for limit in range(1, max_small + 1, step):
            candidates.append(
                _pack_group_limited(
                    items,
                    start_id=start_id,
                    sizes=sizes,
                    thickness_mm=thickness_mm,
                    rotations=rotations,
                    limited_index=small_index,
                    max_limited_sheets=limit,
                )
            )

    mixed = [result for result in candidates if _result_uses_all_sizes(result, sizes)]
    pool = mixed or candidates
    return min(
        pool,
        key=lambda result: (
            result.total_sheet_area,
            _large_sheet_counts_asc(result, sizes),
            result.total_sheets,
        ),
    )


def nest_list_items(
    items: list[ListItem],
    sheet_width_mm: float | None = None,
    sheet_height_mm: float | None = None,
    thickness_mm: float = 20.0,
    rotations: tuple[int, ...] = DEFAULT_ROTATIONS,
    trials: int = 1,
    seed: int = 0,
    sheet_sizes: list[tuple[float, float]] | None = None,
) -> list[tuple[str, list[ListItem], Any]]:
    """按材料分组排板，返回 [(材料, 零件清单, NestingResult)]。

    可传入 sheet_sizes 以支持混合大板尺寸；未传时使用 sheet_width_mm 与
    sheet_height_mm 组成单一尺寸。
    """
    if not items:
        raise ValueError("清单中没有可排板的规格件")

    if sheet_sizes:
        sizes = [(float(width), float(height)) for width, height in sheet_sizes]
    elif sheet_width_mm is not None and sheet_height_mm is not None:
        sizes = [(float(sheet_width_mm), float(sheet_height_mm))]
    else:
        raise ValueError("未指定可用大板尺寸")

    unfit = [
        item
        for item in items
        if not any(
            _item_fits(item, width, height, tuple(rotations))
            for width, height in sizes
        )
    ]
    if unfit:
        sample = ", ".join(
            f"{item.length_mm:.0f}x{item.width_mm:.0f}\u00d7{item.qty}"
            for item in unfit[:10]
        )
        size_text = ", ".join(f"{width:.0f}x{height:.0f}" for width, height in sizes)
        raise ValueError(
            f"以下 {len(unfit)} 行清单尺寸无法放入任一可用大板 "
            f"{size_text}\uff1a{sample}"
        )

    material_values = [item.material for item in items if item.material]
    group_by_material = len(set(material_values)) < len(material_values)

    groups: dict[str, list[ListItem]] = defaultdict(list)
    if group_by_material:
        for item in items:
            groups[item.material or "_all"].append(item)
    else:
        groups["_all"] = list(items)

    results: list[tuple[str, list[ListItem], Any]] = []
    next_id = 1
    for material in sorted(groups):
        group_items = groups[material]
        result = _nest_group_rect_multi(
            group_items,
            start_id=next_id,
            sizes=sizes,
            thickness_mm=thickness_mm,
            rotations=rotations,
        )
        results.append((material, group_items, result))
        next_id += sum(item.qty for item in group_items)
    return results


def build_conclusion_text(
    items: list[ListItem],
    group_results: list[tuple[str, list[ListItem], Any]],
    sheet_sizes: list[tuple[float, float]],
    show_sheets: bool = False,
) -> str:
    """生成可读的排板结论文本。

    show_sheets=False 时只输出总板数、总出材率和材料分组；为 True 时附加
    逐板明细。
    """
    lines: list[str] = []
    total_parts = sum(
        sum(item.qty for item in group_items) for _, group_items, _ in group_results
    )
    total_sheets = sum(result.total_sheets for _, _, result in group_results)
    total_net_area = sum(result.total_part_area for _, _, result in group_results)
    total_board_area = sum(result.total_sheet_area for _, _, result in group_results)
    total_yield = (
        total_net_area / total_board_area * 100 if total_board_area else 0.0
    )

    if len(sheet_sizes) == 1:
        width, height = sheet_sizes[0]
        size_text = f"{width:.0f} x {height:.0f} mm"
    else:
        size_text = ", ".join(
            f"{width:.0f}x{height:.0f}" for width, height in sheet_sizes
        ) + " mm"

    lines.append("=== 规格板清单排板结论 ===")
    lines.append(f"清单条目：{len(items)} 行")
    lines.append(f"零件总数：{total_parts} 件")
    lines.append(f"可用大板：{size_text}")
    lines.append(f"使用大板：{total_sheets} 张")
    lines.append(f"净面积：{total_net_area / 1e6:.3f} m\u00b2")
    lines.append(f"大板总面积：{total_board_area / 1e6:.3f} m\u00b2")
    lines.append(f"总出材率：{total_yield:.1f}%")
    lines.append("")

    if len(sheet_sizes) > 1:
        lines.append("各规格用板：")
        for width, height in sorted(sheet_sizes, key=lambda size: size[0] * size[1]):
            count = sum(
                1
                for _, _, result in group_results
                for sheet in result.sheets
                if abs(sheet.width - width) < 1e-6
                and abs(sheet.height - height) < 1e-6
            )
            lines.append(f"- {width:.0f}x{height:.0f} mm：{count} 张")
        lines.append("")

    if len(group_results) > 1:
        lines.append("各材料分组：")
        for material, group_items, result in group_results:
            group_parts = sum(item.qty for item in group_items)
            group_yield = (
                result.total_part_area / result.total_sheet_area * 100
                if result.total_sheet_area
                else 0.0
            )
            material_display = material or "未分组"
            lines.append(
                f"- {material_display}："
                f"{group_parts} 件，"
                f"{result.total_sheets} 张板，"
                f"出材率 {group_yield:.1f}%"
            )
        lines.append("")

    if show_sheets:
        global_sheet_index = 0
        for _, _, result in group_results:
            for sheet in result.sheets:
                global_sheet_index += 1
                used_area = sheet.used_area
                utilization = (
                    used_area / sheet.total_area * 100 if sheet.total_area else 0.0
                )
                lines.append(
                    f"  \u677f {global_sheet_index:>2} "
                    f"({sheet.width:.0f}x{sheet.height:.0f}): "
                    f"{len(sheet.parts):>4} \u4ef6, "
                    f"\u4f7f\u7528\u9762\u79ef {used_area / 1e6:.3f} m\u00b2, "
                    f"\u5229\u7528\u7387 {utilization:.1f}%"
                )

    if not total_sheets:
        lines.append("\u672a\u80fd\u751f\u6210\u4efb\u4f55\u6392\u677f\u65b9\u6848")
    return "\n".join(lines)


def _inflate_items(items: list[ListItem], kerf_mm: float) -> list[ListItem]:
    """锯缝放大：每件 +kerf，配合大板 +kerf 后按零间隙排，等价于件间留缝。"""
    return [
        ListItem(
            item.material,
            item.length_mm + kerf_mm,
            item.width_mm + kerf_mm,
            item.qty,
            item.source,
        )
        for item in items
    ]


def _deflate_results(group_results: list, kerf_mm: float) -> None:
    """排板结束后把放大坐标系缩回净尺寸：板 -kerf、件 -kerf，位置不变。"""
    for _, _, result in group_results:
        for sheet in result.sheets:
            sheet.width -= kerf_mm
            sheet.height -= kerf_mm
            sheet.remaining_area = sheet.width * sheet.height
            for part in sheet.parts:
                minx, miny, maxx, maxy = part.polygon.bounds
                shrunk = box(minx, miny, maxx - kerf_mm, maxy - kerf_mm)
                part.polygon = shrunk
                part.outer_polygon = shrunk
                part.area = shrunk.area
                part.label_position = (
                    (minx + maxx - kerf_mm) / 2.0,
                    (miny + maxy - kerf_mm) / 2.0,
                )
        result.total_sheet_area = sum(sheet.total_area for sheet in result.sheets)
        result.total_part_area = sum(
            part.area for sheet in result.sheets for part in sheet.parts
        )


def _billing_summary_lines(group_results: list, oversize_mm: float) -> str:
    """按标称（计价）尺寸统计用板，便于与人工/仁霸口径对比。"""
    counts: Counter = Counter()
    for _, _, result in group_results:
        for sheet in result.sheets:
            counts[
                (
                    round(sheet.width - oversize_mm),
                    round(sheet.height - oversize_mm),
                )
            ] += 1
    lines = ["", "标称大板口径："]
    total_area = 0.0
    total_sheets = 0
    for (width, height), count in sorted(counts.items(), key=lambda kv: kv[0][0] * kv[0][1]):
        total_area += width * height * count / 1e6
        total_sheets += count
        lines.append(f"  {width:.0f}x{height:.0f}：{count} 张")
    lines.append(f"  合计：{total_sheets} 张，标称总面积 {total_area:.3f} m²")
    return "\n".join(lines)


def run_list_nesting(
    file_path: str | Path,
    sheet_width_mm: float | None = None,
    sheet_height_mm: float | None = None,
    thickness_mm: float = 20.0,
    rotations: tuple[int, ...] = DEFAULT_ROTATIONS,
    trials: int = 1,
    seed: int = 0,
    show_sheets: bool = False,
    sheet_sizes: list[tuple[float, float]] | None = None,
    output_dxf_path: str | Path | None = None,
    kerf_mm: float = 0.0,
    oversize_mm: float = 0.0,
) -> str:
    """清单排板主入口，返回结论文本。

    kerf_mm: 锯缝宽度。零件按净尺寸输入，内部放大后排板，输出缩回净尺寸。
    oversize_mm: 大板让尺。可用尺寸 = 标称 + 让尺；报告同时给出标称口径。
    """
    items = parse_list_file(file_path)
    billing_sizes = sheet_sizes or [(sheet_width_mm or 0.0, sheet_height_mm or 0.0)]
    layout_sizes = [
        (width + oversize_mm, height + oversize_mm) for width, height in billing_sizes
    ]
    if kerf_mm > 0:
        effective_sizes = [
            (width + kerf_mm, height + kerf_mm) for width, height in layout_sizes
        ]
        effective_items = _inflate_items(items, kerf_mm)
    else:
        effective_sizes, effective_items = layout_sizes, items
    group_results = nest_list_items(
        effective_items,
        sheet_width_mm=sheet_width_mm,
        sheet_height_mm=sheet_height_mm,
        thickness_mm=thickness_mm,
        rotations=rotations,
        trials=trials,
        seed=seed,
        sheet_sizes=effective_sizes,
    )
    if kerf_mm > 0:
        _deflate_results(group_results, kerf_mm)
    if output_dxf_path:
        from src.dxf_writer import write_list_nesting_dxf

        write_list_nesting_dxf(
            group_results,
            str(output_dxf_path),
            unit_system="metric",
        )
    text = build_conclusion_text(items, group_results, layout_sizes, show_sheets)
    if oversize_mm > 0 or kerf_mm > 0:
        text += _billing_summary_lines(group_results, oversize_mm)
    return text
