"""DXF 输出：将排板结果写回 DXF 文件"""

import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment
from shapely.ops import polylabel
from src.models import Sheet, NestingResult, Part

# ACI 颜色
RED = 1


def write_nested_dxf(result: NestingResult,
                     output_path: str,
                     unit_system: str = "metric") -> None:
    """
    将排板结果写入新的 DXF 文件。

    只包含 NESTED_RESULT 一个 Layer：
    - 每张大板一个红色外框 + Sheet 标签
    - 每块规格板的外轮廓（含挖孔）+ 居中编号 TEXT
    """
    doc = ezdxf.new()

    # 设置单位
    if unit_system == "imperial":
        doc.units = units.Imperial
    else:
        doc.units = units.MM

    msp = doc.modelspace()

    # Layer: NESTED_RESULT - 排板结果
    nested_layer = "NESTED_RESULT"
    doc.layers.add(nested_layer)

    COLS = 6
    GAP = 200
    for i, sheet in enumerate(result.sheets):
        col = i % COLS
        row = i // COLS
        ox = col * (sheet.width + GAP)
        oy = row * (sheet.height + GAP)
        _write_sheet_block(doc, sheet, nested_layer, ox, oy)

    doc.saveas(output_path)


def _write_sheet_block(doc, sheet: Sheet, layer_name: str, ox: float, oy: float):
    """将一张大板的排板结果写入 DXF"""
    msp = doc.modelspace()

    # 大板外框（红色）
    msp.add_lwpolyline(
        [(ox, oy), (ox + sheet.width, oy), (ox + sheet.width, oy + sheet.height), (ox, oy + sheet.height), (ox, oy)],
        dxfattribs={'layer': layer_name, 'color': RED}
    )

    # 大板编号（红色，放在外框上方避免与规格板编号混淆）
    sheet_label = msp.add_text(
        f"Sheet_{sheet.index}",
        dxfattribs={
            'layer': layer_name,
            'color': RED,
            'height': 50.0,
        }
    )
    sheet_label.set_placement(
        (ox, oy + sheet.height + 30),
        align=TextEntityAlignment.BOTTOM_LEFT,
    )

    # 每个规格板
    for part in sheet.parts:
        _write_part(msp, part, layer_name, ox, oy)


def _write_part(msp, part: Part, layer_name: str, ox: float = 0, oy: float = 0):
    """将单个规格板写入 DXF"""
    # 外轮廓
    exterior = part.outer_polygon.exterior
    if exterior is not None:
        points = [(x + ox, y + oy) for x, y in exterior.coords]
        msp.add_lwpolyline(points, dxfattribs={'layer': layer_name})

    # 挖孔（排板完成后几何坐标已是最终位置，直接写入）
    for hole in part.holes:
        h_exterior = hole.exterior
        if h_exterior is not None:
            points = [(x + ox, y + oy) for x, y in h_exterior.coords]
            msp.add_lwpolyline(points, dxfattribs={'layer': layer_name})

    # 编号标注：置于规格板视觉中心（矩形=几何中心；异形/带孔=最大内切圆圆心，保证落在板内）
    cx, cy = _label_point(part)
    b = part.outer_polygon.bounds
    bw, bh = b[2] - b[0], b[3] - b[1]
    height = max(30.0, min(min(bw, bh) * 0.25, 80.0))
    height = min(height, bw / (0.72 * max(len(part.number), 1)))
    label = msp.add_text(
        part.number,
        dxfattribs={
            'layer': layer_name,
            'height': height,
        }
    )
    label.set_placement(
        (cx + ox, cy + oy),
        align=TextEntityAlignment.MIDDLE_CENTER,
    )


def _label_point(part: Part) -> tuple[float, float]:
    """编号的放置点：用最大内切圆圆心（pole of inaccessibility），
    对矩形即为几何中心，对 L 形/带孔件保证点落在实料上。"""
    try:
        pt = polylabel(part.polygon, tolerance=0.5)
        return pt.x, pt.y
    except Exception:
        return part.label_position
