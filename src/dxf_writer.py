"""DXF 输出：将排板结果写回 DXF 文件"""

import sys

import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment
from shapely.ops import polylabel
from src.models import Sheet, NestingResult, Part

# ACI 颜色
RED = 1
BLUE = 5
WHITE = 7
ORANGE = 30

# 原图轮廓缺口超过该值（mm）就视为“不规范图纸”，
# 在 CHECK_CLOSURE 图层标出提示人工确认。
CLOSURE_WARNING_GAP = 5.0


def _largest_polygon(geom):
    if geom is None:
        return None
    if geom.geom_type == "MultiPolygon":
        return max(geom.geoms, key=lambda polygon: polygon.area)
    return geom


def write_nested_dxf(result: NestingResult,
                     output_path: str,
                     unit_system: str = "metric",
                     board_size: tuple[float, float] | None = None) -> None:
    """将排板结果写入新的 DXF 文件。

    图层：
    - SHEET_BORDER：大板外框（红色）+ Sheet 编号
    - OUTER：台面板外轮廓（黑色/白色）
    - HOLES：盆孔/水龙头孔（蓝色）
    - NUMBERS：板件编号（居中）

    每个面板 + 孔洞 + 编号组成 GROUP。

    ``board_size`` 指定大板**本体**尺寸（长, 宽）。排板时若为刀缝预留收缩了
    内部可用区，零件仍写在其实际坐标，而外框按本体尺寸绘制；为 None 时用
    ``sheet.width`` / ``sheet.height``。
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

    board_w, board_h = board_size if board_size else (None, None)
    COLS = 6
    GAP = 200
    for i, sheet in enumerate(result.sheets):
        col = i % COLS
        row = i // COLS
        ox = col * (sheet.width + GAP)
        oy = row * (sheet.height + GAP)
        _write_sheet_block(doc, sheet, ox, oy,
                           layer_border, layer_outer,
                           layer_holes, layer_numbers,
                           board_size=(board_w, board_h))

    doc.saveas(output_path)


def _write_sheet_block(doc, sheet: Sheet, ox: float, oy: float,
                       lb, lo, lh, ln, label: str | None = None,
                       board_size: tuple[float, float] | None = None):
    """将一张大板的排板结果写入 DXF，含图层分离和 GROUP"""
    msp = doc.modelspace()

    # 大板外框（红色）：按大板本体尺寸绘制
    bw, bh = board_size if board_size else (None, None)
    bw = sheet.width if not bw else bw
    bh = sheet.height if not bh else bh
    msp.add_lwpolyline(
        [(ox, oy), (ox + bw, oy),
         (ox + bw, oy + bh),
         (ox, oy + bh), (ox, oy)],
        dxfattribs={"layer": lb, "color": RED},
    )

    # 大板编号
    sheet_label = msp.add_text(
        label if label is not None else f"Sheet_{sheet.index}",
        dxfattribs={"layer": lb, "color": RED, "height": 50.0},
    )
    sheet_label.set_placement(
        (ox, oy + bh + 30),
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


def _create_part_group(doc, part: Part, entities: list) -> str | None:
    """把单个面板的实体绑成一个 GROUP，返回实际使用的组名。

    ezdxf 的 ``GroupCollection.new()`` 第二个位置参数是 description，
    旧写法把实体列表放在那里，结果写出的是**空 GROUP**；重复编号还会抛
    ``DXFValueError`` 并被 ``except Exception: pass`` 静默吞掉。这里改成
    显式 ``new()`` + ``extend()``，编号重复时自动加后缀，真实失败写 stderr。
    """
    if not entities:
        return None

    base_name = f"PART_{getattr(part, 'number', '') or 'PART'}"
    name = base_name
    suffix = 2
    while True:
        try:
            group = doc.groups.new(name)
            group.extend(entities)
            return name
        except ezdxf.DXFValueError:
            # 组名已被前面的面板占用：保留 GROUP，改用带后缀的名字。
            if suffix > 1000:
                print(
                    f"警告：GROUP 名称 {base_name} 冲突过多，该面板未建组",
                    file=sys.stderr,
                )
                return None
            name = f"{base_name}_{suffix}"
            suffix += 1
        except Exception as exc:  # noqa: BLE001 - 不允许静默失败
            print(
                f"警告：创建 GROUP {name} 失败 - "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return None


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

    # 创建 GROUP（每个面板一个，实体逐个绑定）
    _create_part_group(doc, part, group_entities)


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
    layer_check = "CHECK_CLOSURE"
    for name in (layer_outer, layer_holes, layer_numbers, layer_check):
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

        b = outer.bounds if outer is not None else part.outer_polygon.bounds
        bw, bh = b[2] - b[0], b[3] - b[1]

        # 原图轮廓未闭合：额外在 CHECK_CLOSURE 图层用橙色标出，
        # 并注明缺口大小，提示用户在原位 DXF 核对。
        gap = float(getattr(part, "closure_gap", 0.0) or 0.0)
        if gap > CLOSURE_WARNING_GAP and ext is not None:
            warning_entity = msp.add_lwpolyline(
                [(x, y) for x, y in ext.coords],
                dxfattribs={"layer": layer_check, "color": ORANGE},
            )
            group_entities.append(warning_entity)
            note = msp.add_text(
                f"未闭合：缺口{gap:.0f}mm",
                dxfattribs={"layer": layer_check, "color": ORANGE,
                            "height": max(20.0, min(bh * 0.15, 60.0))},
            )
            note.set_placement(
                (b[0], b[1] - max(30.0, bh * 0.2)),
                align=TextEntityAlignment.MIDDLE_LEFT,
            )
            group_entities.append(note)

        cx, cy = _label_point(part)
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

        _create_part_group(doc, part, group_entities)

    doc.saveas(output_path)
