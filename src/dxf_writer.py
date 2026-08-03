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


def write_numbered_parts_dxf(parts: list, output_path: str,
                             unit_system: str = "metric") -> None:
    """生成排板前的规格板带编号检查 DXF（网格排列，每板一图一女号）"""
    doc = ezdxf.new()
    if unit_system == "imperial":
        doc.units = units.Imperial
    else:
        doc.units = units.MM
    msp = doc.modelspace()
    layer = "PARTS_CHECK"
    doc.layers.add(layer)

    # 计算最大包围盒，用作网格格子尺寸
    max_w = max_h = 0.0
    for p in parts:
        b = p.outer_polygon.bounds
        max_w = max(max_w, b[2] - b[0])
        max_h = max(max_h, b[3] - b[1])
    cell_w = max_w + 120.0
    cell_h = max_h + 120.0

    COLS = 5
    for i, part in enumerate(parts):
        col = i % COLS
        row = i // COLS
        ox = col * cell_w
        oy = row * cell_h
        b = part.outer_polygon.bounds
        bx, by = b[0], b[1]

        # 外轮廓
        ext = part.outer_polygon.exterior
        if ext is not None:
            pts = [(x - bx + ox, y - by + oy) for x, y in ext.coords]
            msp.add_lwpolyline(pts, dxfattribs={'layer': layer})

        # 挖孔
        for h in part.holes:
            he = h.exterior
            if he is not None:
                pts = [(x - bx + ox, y - by + oy) for x, y in he.coords]
                msp.add_lwpolyline(pts, dxfattribs={'layer': layer})

        # 编号（置于板件几何中心）
        cx, cy = _label_point(part)
        cx_moved = cx - bx + ox
        cy_moved = cy - by + oy
        bbox_w, bbox_h = b[2] - b[0], b[3] - b[1]
        h = max(30.0, min(min(bbox_w, bbox_h) * 0.25, 80.0))
        text_h = min(h, bbox_w / (0.72 * max(len(part.number), 1)))
        label = msp.add_text(part.number, dxfattribs={
            'layer': layer,
            'height': text_h,
        })
        label.set_placement((cx_moved, cy_moved),
                           align=TextEntityAlignment.MIDDLE_CENTER)

    doc.saveas(output_path)


def write_inplace_check_dxf(parts, original_dxf_path, output_path,
                             unit_system="metric") -> None:
    """Generate in-place numbering check DXF.

    Parts stay at original coordinates. Each panel + holes + number
    forms a GROUP for easy selection in CAD.

    Layers:
      - OUTER: panel outer contours (black)
      - HOLES: faucet/sink holes (blue)
      - NUMBERS: part numbers (green)
    """
    import ezdxf
    from ezdxf import units
    from ezdxf.enums import TextEntityAlignment
    from shapely.ops import polylabel

    # Read original DXF to copy entities by handle
    orig_doc = ezdxf.readfile(original_dxf_path)
    orig_msp = orig_doc.modelspace()

    # Build handle -> entity map
    handle_map = {}
    for e in orig_msp:
        handle_map[e.dxf.handle] = e

    # Create output DXF
    doc = ezdxf.new()
    if unit_system == "imperial":
        doc.units = units.Imperial
    else:
        doc.units = units.MM
    msp = doc.modelspace()

    # Layers
    layer_outer = "OUTER"
    layer_holes = "HOLES"
    layer_numbers = "NUMBERS"
    for name, color in [(layer_outer, 7), (layer_holes, 5), (layer_numbers, 3)]:
        if name not in [l.dxf.name for l in doc.layers]:
            doc.layers.add(name, dxfattribs={"color": color})

    for part in parts:
        group_entities = []

        # Copy outer contour from original
        outer_handle = part.outer_handle if hasattr(part, "outer_handle") else None
        if outer_handle and outer_handle in handle_map:
            src = handle_map[outer_handle]
            copied = _copy_entity(src, doc, msp, layer_outer)
            if copied:
                group_entities.append(copied)
        else:
            # Fallback: write from polygon
            ext = part.outer_polygon.exterior
            if ext is not None:
                pts = [(x, y) for x, y in ext.coords]
                e = msp.add_lwpolyline(pts, dxfattribs={"layer": layer_outer})
                group_entities.append(e)

        # Copy holes from original
        hole_handles = part.hole_handles if hasattr(part, "hole_handles") else []
        if hole_handles:
            for hh in hole_handles:
                if hh in handle_map:
                    src = handle_map[hh]
                    copied = _copy_entity(src, doc, msp, layer_holes)
                    if copied:
                        group_entities.append(copied)
        else:
            # Fallback: write from polygon
            for hole in part.holes:
                h_ext = hole.exterior
                if h_ext is not None:
                    pts = [(x, y) for x, y in h_ext.coords]
                    e = msp.add_lwpolyline(pts, dxfattribs={"layer": layer_holes})
                    group_entities.append(e)

        # Number text at centroid
        cx, cy = _label_point(part)
        b = part.outer_polygon.bounds
        bw, bh = b[2] - b[0], b[3] - b[1]
        height = max(30.0, min(min(bw, bh) * 0.25, 80.0))
        height = min(height, bw / (0.72 * max(len(part.number), 1)))
        label = msp.add_text(
            part.number,
            dxfattribs={
                "layer": layer_numbers,
                "height": height,
            }
        )
        label.set_placement(
            (cx, cy),
            align=TextEntityAlignment.MIDDLE_CENTER,
        )
        group_entities.append(label)

        # Create GROUP
        if group_entities:
            group_name = f"PART_{part.number}"
            try:
                doc.groups.new(group_name, group_entities)
            except Exception:
                pass

    doc.saveas(output_path)


def _copy_entity(src_entity, target_doc, target_msp, target_layer):
    """Copy a single DXF entity from source doc to target doc.

    Supports LWPOLYLINE (with bulges), CIRCLE, LINE, ARC.
    Returns the new entity or None.
    """
    try:
        etype = src_entity.dxftype()
        if etype == "LWPOLYLINE":
            pts = []
            try:
                with src_entity.points() as sp:
                    for p in sp:
                        pts.append((p[0], p[1], p[3] if len(p) > 3 else 0.0, p[4] if len(p) > 4 else 0.0))
            except Exception:
                pts2 = list(src_entity.get_points())
                pts = []
                for p in pts2:
                    x, y = p[0], p[1]
                    bulge = p[4] if len(p) > 4 else 0.0
                    pts.append((x, y, 0.0, bulge))

            e = target_msp.add_lwpolyline(
                [(x, y) for x, y, _, _ in pts],
                dxfattribs={"layer": target_layer}
            )
            # Set bulges
            for i, (_, _, _, bulge) in enumerate(pts):
                if bulge != 0.0:
                    try:
                        e.set_bulge(i, bulge)
                    except Exception:
                        pass
            # Copy closed flag
            if src_entity.closed:
                e.closed = True
            return e

        elif etype == "CIRCLE":
            cx = src_entity.dxf.center.x
            cy = src_entity.dxf.center.y
            r = src_entity.dxf.radius
            return target_msp.add_circle(
                (cx, cy), r,
                dxfattribs={"layer": target_layer}
            )

        elif etype == "LINE":
            sx = src_entity.dxf.start.x
            sy = src_entity.dxf.start.y
            ex = src_entity.dxf.end.x
            ey = src_entity.dxf.end.y
            return target_msp.add_line(
                (sx, sy), (ex, ey),
                dxfattribs={"layer": target_layer}
            )

        elif etype == "ARC":
            cx = src_entity.dxf.center.x
            cy = src_entity.dxf.center.y
            r = src_entity.dxf.radius
            sa = src_entity.dxf.start_angle
            ea = src_entity.dxf.end_angle
            return target_msp.add_arc(
                (cx, cy), r, sa, ea,
                dxfattribs={"layer": target_layer}
            )

        elif etype == "SPLINE":
            # Approximate with polyline
            pts = list(src_entity.flattening(0.1))
            coords = [(p.x, p.y) for p in pts]
            if len(coords) >= 2:
                return target_msp.add_lwpolyline(
                    coords,
                    dxfattribs={"layer": target_layer}
                )

    except Exception:
        pass
    return None
