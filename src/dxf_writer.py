"""DXF 输出：将排板结果写回 DXF 文件"""

import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment
from shapely.ops import polylabel
from src.models import Sheet, NestingResult, Part

# ACI 颜色
RED = 1
BLUE = 5
WHITE = 7


def _largest_polygon(geom):
    if geom is None:
        return None
    if geom.geom_type == "MultiPolygon":
        return max(geom.geoms, key=lambda polygon: polygon.area)
    return geom


def write_nested_dxf(result: NestingResult,
                     output_path: str,
                     unit_system: str = "metric") -> None:
    """将排板结果写入新的 DXF 文件。

    图层：
    - SHEET_BORDER：大板外框（红色）+ Sheet 编号
    - OUTER：台面板外轮廓（黑色/白色）
    - HOLES：盆孔/水龙头孔（蓝色）
    - NUMBERS：板件编号（居中）

    每个面板 + 孔洞 + 编号组成 GROUP。
    """
    doc = ezdxf.new()
    doc.units = units.MM if unit_system != "imperial" else units.IN

    msp = doc.modelspace()

    # 创建图层
    layer_border = "SHEET_BORDER"
    layer_outer = "OUTER"
    layer_holes = "HOLES"
    layer_numbers = "NUMBERS"
    for name in (layer_border, layer_outer, layer_holes, layer_numbers):
        doc.layers.add(name)

    COLS = 6
    GAP = 200
    for i, sheet in enumerate(result.sheets):
        col = i % COLS
        row = i // COLS
        ox = col * (sheet.width + GAP)
        oy = row * (sheet.height + GAP)
        _write_sheet_block(doc, sheet, ox, oy,
                           layer_border, layer_outer,
                           layer_holes, layer_numbers)

    doc.saveas(output_path)


def _write_sheet_block(doc, sheet: Sheet, ox: float, oy: float,
                       lb, lo, lh, ln, label: str | None = None):
    """将一张大板的排板结果写入 DXF，含图层分离和 GROUP"""
    msp = doc.modelspace()

    # 大板外框（红色）
    msp.add_lwpolyline(
        [(ox, oy), (ox + sheet.width, oy),
         (ox + sheet.width, oy + sheet.height),
         (ox, oy + sheet.height), (ox, oy)],
        dxfattribs={"layer": lb, "color": RED},
    )

    # 大板编号
    sheet_label = msp.add_text(
        label if label is not None else f"Sheet_{sheet.index}",
        dxfattribs={"layer": lb, "color": RED, "height": 50.0},
    )
    sheet_label.set_placement(
        (ox, oy + sheet.height + 30),
        align=TextEntityAlignment.BOTTOM_LEFT,
    )

    for part in sheet.parts:
        _write_part_group(msp, part, ox, oy, lo, lh, ln, doc)


def _write_sheet_quantity(doc, sheet: Sheet, ox: float, oy: float,
                          count: int, layer: str) -> None:
    """在板图旁边写入该排板模式的数量。"""
    msp = doc.modelspace()
    label = msp.add_text(
        f"数量 x {count}",
        dxfattribs={"layer": layer, "color": RED, "height": 60.0},
    )
    label.set_placement(
        (ox + sheet.width + 30, oy),
        align=TextEntityAlignment.BOTTOM_LEFT,
    )


def _layout_signature(sheet: Sheet) -> tuple:
    """把一个板面的零件布局转成可哈希签名，忽略零件编号顺序。"""
    parts = []
    for part in sheet.parts:
        min_x, min_y, max_x, max_y = part.outer_polygon.bounds
        parts.append(
            (
                round(min_x, 2),
                round(min_y, 2),
                round(max_x, 2),
                round(max_y, 2),
            )
        )
    return (
        round(sheet.width, 2),
        round(sheet.height, 2),
        tuple(sorted(parts)),
    )


def _group_sheets_by_layout(sheets: list[Sheet]) -> list[list[Sheet]]:
    """把布局完全相同的板归为一组，只保留首张作为代表。"""
    grouped: dict[tuple, list[Sheet]] = {}
    for sheet in sheets:
        grouped.setdefault(_layout_signature(sheet), []).append(sheet)
    return list(grouped.values())


def write_list_nesting_dxf(
    group_results: list,
    output_path: str,
    unit_system: str = "metric",
) -> None:
    """将清单排板结果写为 DXF。

    布局完全相同的板只画一张代表图，并在图旁标注数量。
    group_results 格式与 `nest_list_items` 返回值一致：
    [(material, group_items, NestingResult), ...]
    """
    doc = ezdxf.new()
    doc.units = units.MM if unit_system != "imperial" else units.IN
    msp = doc.modelspace()

    layer_border = "SHEET_BORDER"
    layer_outer = "OUTER"
    layer_holes = "HOLES"
    layer_numbers = "NUMBERS"
    for name in (layer_border, layer_outer, layer_holes, layer_numbers):
        doc.layers.add(name)

    COLS = 6
    GAP = 300
    max_width = max(
        (sheet.width for _, _, result in group_results for sheet in result.sheets),
        default=2400.0,
    )
    max_height = max(
        (sheet.height for _, _, result in group_results for sheet in result.sheets),
        default=1200.0,
    )
    col_step = max_width + GAP
    row_step = max_height + GAP
    block_index = 0
    for material, _, result in group_results:
        for sheets in _group_sheets_by_layout(result.sheets):
            representative = sheets[0]
            count = len(sheets)
            col = block_index % COLS
            row = block_index // COLS
            ox = col * col_step
            oy = row * row_step

            size_label = f"{representative.width:.0f}x{representative.height:.0f}"
            if material:
                size_label = f"{material} | {size_label}"
            _write_sheet_block(
                doc,
                representative,
                ox,
                oy,
                layer_border,
                layer_outer,
                layer_holes,
                layer_numbers,
                label=size_label,
            )
            _write_sheet_quantity(
                doc,
                representative,
                ox,
                oy,
                count,
                layer_border,
            )
            block_index += 1

    doc.saveas(output_path)


def _write_part_group(msp, part: Part, ox: float, oy: float,
                      layer_outer, layer_holes, layer_numbers, doc):
    """写单个面板：外轮廓 + 孔洞 + 编号 → GROUP"""
    group_entities = []

    # 外轮廓
    outer = _largest_polygon(part.outer_polygon)
    ext = outer.exterior if outer is not None else None
    if ext is not None:
        pts = [(x + ox, y + oy) for x, y in ext.coords]
        e = msp.add_lwpolyline(pts, dxfattribs={"layer": layer_outer})
        group_entities.append(e)

    # 孔洞（蓝色）
    for hole in part.holes:
        h_ext = hole.exterior
        if h_ext is not None:
            pts = [(x + ox, y + oy) for x, y in h_ext.coords]
            e = msp.add_lwpolyline(pts, dxfattribs={"layer": layer_holes, "color": BLUE})
            group_entities.append(e)

    # 编号标注
    cx, cy = _label_point(part)
    b = outer.bounds if outer is not None else part.outer_polygon.bounds
    bw, bh = b[2] - b[0], b[3] - b[1]
    height = max(30.0, min(min(bw, bh) * 0.25, 80.0))
    height = min(height, bw / (0.72 * max(len(part.number), 1)))
    label = msp.add_text(
        part.number,
        dxfattribs={"layer": layer_numbers, "height": height},
    )
    label.set_placement(
        (cx + ox, cy + oy),
        align=TextEntityAlignment.MIDDLE_CENTER,
    )
    group_entities.append(label)

    # 创建 GROUP
    if group_entities:
        group_name = f"PART_{part.number}"
        try:
            doc.groups.new(group_name, group_entities)
        except Exception:
            pass


def _label_point(part: Part) -> tuple[float, float]:
    """编号放置点：最大内切圆圆心"""
    try:
        pt = polylabel(part.polygon, tolerance=0.5)
        return pt.x, pt.y
    except Exception:
        return part.label_position


def write_numbered_parts_dxf(parts: list, output_path: str,
                             unit_system: str = "metric") -> None:
    """生成排板前的规格板带编号检查 DXF（保持原始坐标）。

    图层：
    - OUTER：台面板外轮廓
    - HOLES：盆孔/水龙头孔（蓝色）
    - NUMBERS：板件编号（居中）
    """
    doc = ezdxf.new()
    doc.units = units.MM if unit_system != "imperial" else units.IN
    msp = doc.modelspace()

    layer_outer = "OUTER"
    layer_holes = "HOLES"
    layer_numbers = "NUMBERS"
    for name in (layer_outer, layer_holes, layer_numbers):
        doc.layers.add(name)

    for part in parts:
        group_entities = []

        outer = _largest_polygon(part.outer_polygon)
        ext = outer.exterior if outer is not None else None
        if ext is not None:
            pts = [(x, y) for x, y in ext.coords]
            entity = msp.add_lwpolyline(pts, dxfattribs={"layer": layer_outer})
            group_entities.append(entity)

        for hole in part.holes:
            h_ext = hole.exterior
            if h_ext is not None:
                pts = [(x, y) for x, y in h_ext.coords]
                entity = msp.add_lwpolyline(
                    pts,
                    dxfattribs={"layer": layer_holes, "color": BLUE},
                )
                group_entities.append(entity)

        cx, cy = _label_point(part)
        b = outer.bounds if outer is not None else part.outer_polygon.bounds
        bw, bh = b[2] - b[0], b[3] - b[1]
        height = max(30.0, min(min(bw, bh) * 0.25, 80.0))
        height = min(height, bw / (0.72 * max(len(part.number), 1)))
        label = msp.add_text(
            part.number,
            dxfattribs={"layer": layer_numbers, "height": height},
        )
        label.set_placement(
            (cx, cy),
            align=TextEntityAlignment.MIDDLE_CENTER,
        )
        group_entities.append(label)

        if group_entities:
            try:
                doc.groups.new(f"PART_{part.number}", group_entities)
            except Exception:
                pass

    doc.saveas(output_path)
