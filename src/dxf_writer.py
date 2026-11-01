"""DXF 输出：将排板结果写回 DXF 文件"""

import math
import sys

import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import Polygon
from shapely.ops import polylabel
from src.models import Sheet, NestingResult, Part
from src.dxf_reader import SOURCE_CURVE_TOL_MM
from src.source_curves import placed_specs

# ACI 颜色
RED = 1
BLUE = 5
WHITE = 7
ORANGE = 30
GREEN = 3

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
    """可复现输出包装：固定 ezdxf 文档身份元数据（GUID/创建与保存时间戳）。

    同一布局重复写出的排板 DXF 逐字节一致——内容寻址、回滚验收和 diff
    都依赖这一点。这些元数据仅用于 CAD 的 xref 变更检测，对一次性交付
    文件无影响。注意必须覆盖文档创建阶段（CREATED_BY 在建档时写入）。
    """
    previous_fixed_meta = ezdxf.options.write_fixed_meta_data_for_testing
    ezdxf.options.write_fixed_meta_data_for_testing = True
    try:
        _write_nested_dxf_impl(result, output_path, unit_system, board_size)
    finally:
        ezdxf.options.write_fixed_meta_data_for_testing = previous_fixed_meta


def _write_nested_dxf_impl(result: NestingResult,
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


def _translate_spec(spec: dict | None, dx: float, dy: float) -> dict | None:
    if not isinstance(spec, dict):
        return None
    kind = spec.get("type")
    if kind in {"circle", "arc"}:
        cx, cy = spec["center"]
        return {**spec, "center": (float(cx) + dx, float(cy) + dy)}
    if kind == "polyline":
        return {
            **spec,
            "vertices": [
                [float(raw[0]) + dx, float(raw[1]) + dy, float(raw[2])]
                for raw in (spec.get("vertices") or [])
            ],
        }
    if kind == "ellipse":
        cx, cy = spec["center"]
        return {**spec, "center": (float(cx) + dx, float(cy) + dy)}
    if kind == "spline":
        return {
            **spec,
            "control_points": [
                [
                    float(raw[0]) + dx,
                    float(raw[1]) + dy,
                    float(raw[2]) if len(raw) > 2 else 0.0,
                ]
                for raw in (spec.get("control_points") or [])
            ],
        }
    return None


def _add_curve_entity(msp, spec: dict, layer: str, color: int | None = None):
    """按谱写回椭圆 / 样条实体（机床直接加工需要真曲线）。"""
    kind = spec.get("type")
    attribs: dict = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    if kind == "ellipse":
        return msp.add_ellipse(
            spec["center"],
            spec["major_axis"],
            spec["ratio"],
            start_param=spec.get("start_param", 0.0),
            end_param=spec.get("end_param", 2 * math.pi),
            dxfattribs=attribs,
        )
    if kind == "spline":
        from ezdxf.math import BSpline

        bspline = BSpline(
            [tuple(point) for point in spec["control_points"]],
            order=int(spec["degree"]) + 1,
            knots=[float(k) for k in spec["knots"]],
            weights=[float(w) for w in spec["weights"]] or None,
        )
        spline = msp.add_spline(dxfattribs=attribs)
        spline.apply_construction_tool(bspline)
        if spec.get("closed"):
            spline.closed = True
        return spline
    return None


def _emit_outer_entities(
    msp,
    part: Part,
    outer,
    ox: float,
    oy: float,
    layer: str,
    specs: dict | None,
) -> list:
    """按源实体形态写外轮廓；没有源谱时退回一条折线（原行为）。"""
    specs = specs or {}
    shape = _translate_spec(specs.get("shape"), ox, oy)
    if shape and shape.get("type") == "circle":
        return [
            msp.add_circle(
                shape["center"],
                shape["radius"],
                dxfattribs={"layer": layer},
            )
        ]

    polyline = _translate_spec(specs.get("polyline"), ox, oy)
    if polyline:
        points = _polyline_points_with_bulges(polyline)
        if points:
            return [
                msp.add_lwpolyline(
                    points,
                    format="xyb",
                    close=bool(polyline.get("closed", True)),
                    dxfattribs={"layer": layer},
                )
            ]

    curve = _translate_spec(specs.get("curve"), ox, oy)
    if curve and curve.get("type") in {"ellipse", "spline"}:
        entity = _add_curve_entity(msp, curve, layer)
        if entity is not None:
            return [entity]

    arcs = [
        item
        for item in (
            _translate_spec(arc, ox, oy) for arc in specs.get("arcs") or []
        )
        if item
    ]
    ext = outer.exterior if outer is not None else None
    if arcs and ext is not None:
        shifted = Polygon([(x + ox, y + oy) for x, y in ext.coords])
        entities: list = []
        for arc in arcs:
            entities.append(
                msp.add_arc(
                    arc["center"],
                    arc["radius"],
                    arc["start_angle"],
                    arc["end_angle"],
                    dxfattribs={"layer": layer},
                )
            )
        for run in _ring_runs_outside_arcs(shifted.exterior, arcs):
            entities.append(msp.add_lwpolyline(run, dxfattribs={"layer": layer}))
        return entities

    if ext is None:
        return []
    pts = [(x + ox, y + oy) for x, y in ext.coords]
    return [msp.add_lwpolyline(pts, dxfattribs={"layer": layer})]


def _emit_hole_entity(msp, hole, spec: dict | None, ox: float, oy: float, layer: str):
    """按源实体形态写孔洞（圆/带 bulge 多段线/折线）。"""
    h_ext = hole.exterior
    if h_ext is None:
        return None
    shifted = _translate_spec(spec, ox, oy)
    if shifted and shifted.get("type") == "circle":
        return msp.add_circle(
            shifted["center"],
            shifted["radius"],
            dxfattribs={"layer": layer, "color": BLUE},
        )
    if shifted and shifted.get("type") in {"ellipse", "spline"}:
        return _add_curve_entity(msp, shifted, layer, color=BLUE)
    if shifted and shifted.get("type") == "polyline":
        points = _polyline_points_with_bulges(shifted)
        return msp.add_lwpolyline(
            points,
            format="xyb",
            close=bool(shifted.get("closed", True)),
            dxfattribs={"layer": layer, "color": BLUE},
        )
    pts = [(x + ox, y + oy) for x, y in h_ext.coords]
    return msp.add_lwpolyline(
        pts, dxfattribs={"layer": layer, "color": BLUE}
    )


def _write_part_group(msp, part: Part, ox: float, oy: float,
                      layer_outer, layer_holes, layer_numbers, doc):
    """写单个面板：外轮廓 + 孔洞 + 编号 → GROUP"""
    group_entities = []

    # 外轮廓：有源曲线谱就按源实体形态复写（圆/圆弧/带 bulge 的多段线），
    # 机床按 DXF 直接加工时必须拿到真圆弧。
    outer = _largest_polygon(part.outer_polygon)
    outer_specs, hole_specs = placed_specs(part)
    group_entities.extend(
        _emit_outer_entities(
            msp, part, outer, ox, oy, layer_outer, outer_specs
        )
    )

    # 孔洞（蓝色）
    for index, hole in enumerate(part.holes):
        spec = hole_specs[index] if index < len(hole_specs) else None
        entity = _emit_hole_entity(msp, hole, spec, ox, oy, layer_holes)
        if entity is not None:
            group_entities.append(entity)

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


def _polyline_points_with_bulges(
    spec: dict,
) -> list[tuple[float, float, float]]:
    """源多段线谱 → (x, y, bulge) 顶点列。

    ``close=True`` 的多段线不重复写首点；源数据若首尾重合要去掉尾点，
    避免 CAD 里出现零长度段。
    """
    points = [
        (float(raw[0]), float(raw[1]), float(raw[2]))
        for raw in (spec.get("vertices") or [])
        if len(raw) >= 2
    ]
    if len(points) < 3:
        return []
    if spec.get("closed", True) and len(points) > 3:
        first, last = points[0], points[-1]
        if abs(first[0] - last[0]) <= 1e-9 and abs(first[1] - last[1]) <= 1e-9:
            points = points[:-1]
    return points


# 原位 DXF 的标准图层（英文）：颜色按层设置，便于 CAD 里开关与识别。
_NUMBERED_LAYERS: dict[str, int] = {
    "OUTER": WHITE,
    "HOLES": BLUE,
    "NUMBERS": GREEN,
    "MATERIALS": 2,
    "MATERIAL_HEADER": 2,
    "DIMENSIONS": 4,
    "TEXT": WHITE,
    "HATCH": 8,
    "BLOCKS": 6,
    "OTHER": 8,
    "CHECK_CLOSURE": ORANGE,
}

# 不同材料用不同颜色（实线）：红/绿/蓝/品红/青/橙/黄/灰，循环使用。
_MATERIAL_COLORS = [1, 3, 5, 6, 4, 30, 2, 8]


def _ensure_numbered_layers(doc) -> None:
    for name, color in _NUMBERED_LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=color)
        else:
            doc.layers.get(name).dxf.color = color


def _material_color_map(parts: list) -> dict[str, int]:
    names = sorted(
        {
            str(getattr(part, "material_group", "") or "").strip()
            for part in parts
            if str(getattr(part, "material_group", "") or "").strip()
        }
    )
    return {
        name: _MATERIAL_COLORS[index % len(_MATERIAL_COLORS)]
        for index, name in enumerate(names)
    }


def _relayer_source_entities(doc, parts: list) -> dict[int, dict[str, list]]:
    """把源图实体按语义重新归层，返回每个零件的实体清单。

    几何一律不改动：尺寸标注、文字、填充、图块只是换到标准图层，
    单位、精度、样式随文档原样保留。
    """
    outer_owner: dict[str, tuple[int, str]] = {}
    hole_owner: dict[str, int] = {}
    for index, part in enumerate(parts):
        handle = str(getattr(part, "outer_handle", "") or "")
        if handle:
            outer_owner[handle] = (
                index,
                str(getattr(part, "material_group", "") or "").strip(),
            )
        for hole_handle in getattr(part, "hole_handles", None) or []:
            handle = str(hole_handle or "")
            if handle:
                hole_owner[handle] = index

    colors = _material_color_map(parts)
    by_part: dict[int, dict[str, list]] = {
        index: {"outer": [], "holes": []} for index in range(len(parts))
    }

    for entity in doc.modelspace():
        handle = str(getattr(entity.dxf, "handle", "") or "")
        dtype = entity.dxftype()
        if handle in outer_owner:
            index, material = outer_owner[handle]
            entity.dxf.layer = "OUTER"
            if material:
                entity.dxf.color = colors.get(material, WHITE)
            by_part[index]["outer"].append(entity)
            continue
        if handle in hole_owner:
            index = hole_owner[handle]
            entity.dxf.layer = "HOLES"
            entity.dxf.color = 256  # BYLAYER
            by_part[index]["holes"].append(entity)
            continue
        if dtype == "DIMENSION":
            entity.dxf.layer = "DIMENSIONS"
        elif dtype in {"TEXT", "MTEXT"}:
            entity.dxf.layer = "TEXT"
        elif dtype == "HATCH":
            entity.dxf.layer = "HATCH"
        elif dtype == "INSERT":
            entity.dxf.layer = "BLOCKS"
        else:
            entity.dxf.layer = "OTHER"
    return by_part


def _write_numbered_from_source(
    parts: list,
    output_path: str,
    material_headers: list[dict] | None,
    source_path: str,
) -> None:
    """源图副本 + 重分层 + 加编号/材料/厚度（几何不重画）。"""
    doc = ezdxf.readfile(source_path)
    msp = doc.modelspace()
    _ensure_numbered_layers(doc)
    by_part = _relayer_source_entities(doc, parts)
    colors = _material_color_map(parts)

    has_material = bool(colors)
    for index, part in enumerate(parts):
        group_entities: list = []
        group_entities.extend(by_part[index]["outer"])
        group_entities.extend(by_part[index]["holes"])

        outer = _largest_polygon(part.outer_polygon)
        b = outer.bounds if outer is not None else part.outer_polygon.bounds
        bw, bh = b[2] - b[0], b[3] - b[1]
        cx, cy = _label_point(part)
        height = max(30.0, min(min(bw, bh) * 0.25, 80.0))
        height = min(height, bw / (0.72 * max(len(part.number), 1)))

        label = msp.add_text(
            part.number,
            dxfattribs={
                "layer": "NUMBERS",
                "color": GREEN,
                "height": height,
            },
        )
        label.set_placement((cx, cy), align=TextEntityAlignment.MIDDLE_CENTER)
        group_entities.append(label)

        material = (getattr(part, "material_group", None) or "").strip()
        thickness = float(getattr(part, "thickness_mm", 0.0) or 0.0)
        if thickness <= 0:
            thickness = 20.0
        default_note = (
            "(默认)"
            if str(getattr(part, "thickness_source", "default")) == "default"
            else ""
        )
        if material:
            summary = f"{material} {thickness:g}mm{default_note}"
        elif has_material:
            summary = f"未标注 {thickness:g}mm{default_note}"
        else:
            summary = ""
        if summary:
            material_height = max(20.0, min(height * 0.6, 60.0))
            material_label = msp.add_text(
                summary,
                dxfattribs={
                    "layer": "MATERIALS",
                    "color": colors.get(material, 2) if material else 2,
                    "height": material_height,
                },
            )
            material_label.set_placement(
                (cx, cy - height * 0.5 - material_height * 0.6),
                align=TextEntityAlignment.MIDDLE_CENTER,
            )
            group_entities.append(material_label)

        gap = float(getattr(part, "closure_gap", 0.0) or 0.0)
        if gap > CLOSURE_WARNING_GAP and outer is not None:
            note = msp.add_text(
                f"未闭合：缺口{gap:.0f}mm",
                dxfattribs={
                    "layer": "CHECK_CLOSURE",
                    "color": ORANGE,
                    "height": max(20.0, min(bh * 0.15, 60.0)),
                },
            )
            note.set_placement(
                (b[0], b[1] - max(30.0, bh * 0.2)),
                align=TextEntityAlignment.MIDDLE_LEFT,
            )
            group_entities.append(note)

        if group_entities:
            _create_part_group(doc, part, group_entities)

    # 全图没有材料信息时给一条总说明，避免每个零件重复刷屏。
    if not has_material:
        thickness = 20.0
        for part in parts:
            value = float(getattr(part, "thickness_mm", 0.0) or 0.0)
            if value > 0:
                thickness = value
                break
        try:
            extents = list(msp.query("LWPOLYLINE ARC CIRCLE SPLINE ELLIPSE"))
            xs, ys = [], []
            for entity in extents:
                try:
                    bbox = ezdxf.bbox.extents([entity])
                    xs.extend([bbox.extmin.x, bbox.extmax.x])
                    ys.extend([bbox.extmin.y, bbox.extmax.y])
                except Exception:
                    continue
            origin = (min(xs), max(ys)) if xs and ys else (0.0, 0.0)
        except Exception:
            origin = (0.0, 0.0)
        note = msp.add_text(
            f"材料未标注；厚度按常用 {thickness:g}mm（默认）",
            dxfattribs={
                "layer": "MATERIALS",
                "color": 2,
                "height": 40.0,
            },
        )
        note.set_placement(
            (origin[0], origin[1] + 60.0),
            align=TextEntityAlignment.BOTTOM_LEFT,
        )

    for header in material_headers or []:
        text = str(header.get("text") or "").strip()
        point = header.get("point")
        if not text or not point:
            continue
        try:
            header_height = float(header.get("height") or 0.0)
        except (TypeError, ValueError):
            header_height = 0.0
        label = msp.add_text(
            text,
            dxfattribs={
                "layer": "MATERIAL_HEADER",
                "color": 2,
                "height": header_height if header_height > 0 else 40.0,
            },
        )
        label.set_placement(
            (float(point[0]), float(point[1])),
            align=TextEntityAlignment.MIDDLE_CENTER,
        )

    doc.saveas(output_path)


def _add_outline_entities(msp, part: Part, ring, layer: str) -> list:
    """按**源实体类型**输出外轮廓（原位 DXF 是给人核对、量尺寸的）。

    * 源图整件就是一个 CIRCLE → 写 CIRCLE（CAD 里直接量半径）；
    * 源图轮廓上有 ARC → 这些圆弧写成 ARC，其余边界仍旧折线；
    * 源图是多段线/直线拼接的（哪怕画成圆）→ 一条闭合多段线（原行为）。

    判断在读图阶段做好挂在 ``Part.outer_shape`` / ``Part.outer_arcs`` 上
    （见 ``dxf_reader.attach_source_curves``），写入端不重新解析源图。
    """
    spec = getattr(part, "outer_shape", None)
    if isinstance(spec, dict) and spec.get("type") == "circle":
        return [
            msp.add_circle(
                spec["center"], spec["radius"], dxfattribs={"layer": layer}
            )
        ]

    # 源图外轮廓本身就是带圆弧段的多段线：原样写回 bulge，
    # 保证 CAD 里能直接量半径、机床按真圆弧走刀。
    polyline = getattr(part, "outer_polyline", None)
    if isinstance(polyline, dict) and polyline.get("type") == "polyline":
        points = _polyline_points_with_bulges(polyline)
        if points:
            return [
                msp.add_lwpolyline(
                    points,
                    format="xyb",
                    close=bool(polyline.get("closed", True)),
                    dxfattribs={"layer": layer},
                )
            ]

    curve = getattr(part, "outer_curve", None)
    if isinstance(curve, dict) and curve.get("type") in {"ellipse", "spline"}:
        entity = _add_curve_entity(msp, curve, layer)
        if entity is not None:
            return [entity]

    arcs = [
        item
        for item in (getattr(part, "outer_arcs", None) or [])
        if isinstance(item, dict) and item.get("type") == "arc"
    ]
    if not arcs:
        return [
            msp.add_lwpolyline(
                [(x, y) for x, y in ring.coords], dxfattribs={"layer": layer}
            )
        ]

    entities: list = []
    for arc in arcs:
        entities.append(
            msp.add_arc(
                arc["center"],
                arc["radius"],
                arc["start_angle"],
                arc["end_angle"],
                dxfattribs={"layer": layer},
            )
        )
    for run in _ring_runs_outside_arcs(ring, arcs):
        entities.append(msp.add_lwpolyline(run, dxfattribs={"layer": layer}))
    return entities


def _vertex_inside_arc(spec: dict, x: float, y: float,
                       angle_margin: float = 0.5) -> bool:
    """顶点是否落在该圆弧的**内部**（严格在起止角之间）。

    不能用"到采样折线的距离"判断：采样再密，第一个采样点离端点也有
    半径×步长 的差距（r25、32 段采样时是 1.3mm），于是圆弧端点与相邻直线的
    端点一起被判成"在圆弧上"，整段直线被吃掉——2026-10-16 台面板边缘线消失
    就是这么来的。按角度区间判定才准：半径对得上 + 角度严格落在起止角之间
    （留一点角余量）；圆弧端点归相邻直线。
    """
    cx, cy = spec["center"]
    radius = float(spec["radius"])
    dx, dy = x - cx, y - cy
    if abs(math.hypot(dx, dy) - radius) > SOURCE_CURVE_TOL_MM:
        return False
    span = (float(spec["end_angle"]) - float(spec["start_angle"])) % 360.0
    if span <= 0.0:
        span += 360.0
    offset = (math.degrees(math.atan2(dy, dx)) - float(spec["start_angle"])) % 360.0
    return angle_margin < offset < span - angle_margin


def _ring_runs_outside_arcs(ring, arcs: list[dict]) -> list[list[tuple[float, float]]]:
    """把环的顶点序列按"落在已复写圆弧上"切成若干段折线（圆弧部分不再画）。"""
    points = [(float(x), float(y)) for x, y in ring.coords]
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    if not points:
        return []

    covered = [
        any(_vertex_inside_arc(arc, x, y) for arc in arcs) for x, y in points
    ]
    if all(covered):
        return []
    if not any(covered):
        return [points]

    # 从一个"覆盖 → 未覆盖"的边界起算：这样折线段不会跨越列表首尾，
    # 也不会把环首尾相连的那一段拆成"尾巴一个点 + 开头一段"（漏掉整条边——
    # 2026-10-16 台面板左边线消失就是踩在这里）。
    total = len(points)
    start = 0
    for index in range(total):
        if covered[index] and not covered[(index + 1) % total]:
            start = (index + 1) % total
            break
    order = [(start + step) % total for step in range(total)]
    points = [points[index] for index in order]
    covered = [covered[index] for index in order]

    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for point, is_covered in zip(points, covered):
        if is_covered:
            if len(current) >= 2:
                runs.append(current)
            current = []
        else:
            current.append(point)
    if len(current) >= 2:
        runs.append(current)
    return runs


def write_numbered_parts_dxf(parts: list, output_path: str,
                             unit_system: str = "metric",
                             material_headers: list[dict] | None = None,
                             source_path: str | None = None) -> None:
    """生成排板前的规格板带编号检查 DXF（保持原始坐标）。

    图层：
    - OUTER：台面板外轮廓
    - HOLES：盆孔/水龙头孔（蓝色）
    - NUMBERS：板件编号（居中）
    - MATERIALS：板件材料号（`Part.material_group`，绿色，居编号下方）
    - MATERIAL_HEADER：原图列头材料标签（原位复写，不改坐标）

    ``material_headers`` 为 `{"text", "point", "height"}` 列表，来自
    `drawing_profile.collect_column_materials()`：原图的「材料：ST-x」标签
    按原坐标、原字高写回独立图层，便于在 CAD 里开关对照。

    ``source_path`` 给出时采用"源图副本 + 重分层 + 加编号"：原图的尺寸
    标注、文字说明、填充、图块、单位与精度**全部原样保留**（不重画几何），
    只按语义重新归层并叠加编号/材料/厚度注释。这样人工核对看到的就是
    原图，不存在复写偏差。
    """
    if source_path:
        _write_numbered_from_source(
            parts, output_path, material_headers, str(source_path)
        )
        return
    doc = ezdxf.new()
    doc.units = units.MM if unit_system != "imperial" else units.IN
    msp = doc.modelspace()

    layer_outer = "OUTER"
    layer_holes = "HOLES"
    layer_numbers = "NUMBERS"
    layer_check = "CHECK_CLOSURE"
    for name in (layer_outer, layer_holes, layer_numbers, layer_check):
        doc.layers.add(name)

    layer_materials = "MATERIALS"
    if any((getattr(part, "material_group", None) or "").strip() for part in parts):
        doc.layers.add(layer_materials)
    else:
        layer_materials = None

    headers = [header for header in (material_headers or []) if header.get("text")]
    layer_material_header = "MATERIAL_HEADER"
    if headers:
        doc.layers.add(layer_material_header)

    for part in parts:
        group_entities = []

        outer = _largest_polygon(part.outer_polygon)
        ext = outer.exterior if outer is not None else None
        if ext is not None:
            group_entities.extend(
                _add_outline_entities(msp, part, ext, layer_outer)
            )

        hole_shapes = list(getattr(part, "hole_shapes", None) or [])
        for index, hole in enumerate(part.holes):
            h_ext = hole.exterior
            if h_ext is None:
                continue
            spec = hole_shapes[index] if index < len(hole_shapes) else None
            if isinstance(spec, dict) and spec.get("type") == "circle":
                # 源图就是圆孔 → 写 CIRCLE（能直接量半径）
                entity = msp.add_circle(
                    spec["center"],
                    spec["radius"],
                    dxfattribs={"layer": layer_holes, "color": BLUE},
                )
            elif (
                isinstance(spec, dict)
                and spec.get("type") in {"ellipse", "spline"}
            ):
                entity = _add_curve_entity(
                    msp, spec, layer_holes, color=BLUE
                )
            elif isinstance(spec, dict) and spec.get("type") == "polyline":
                # 源图孔洞是带圆弧段的多段线 → 原样写回 bulge
                points = _polyline_points_with_bulges(spec)
                entity = msp.add_lwpolyline(
                    points,
                    format="xyb",
                    close=bool(spec.get("closed", True)),
                    dxfattribs={"layer": layer_holes, "color": BLUE},
                )
            else:
                entity = msp.add_lwpolyline(
                    [(x, y) for x, y in h_ext.coords],
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

        material = (getattr(part, "material_group", None) or "").strip()
        if material and layer_materials is not None:
            material_height = max(20.0, min(height * 0.6, 60.0))
            material_label = msp.add_text(
                material,
                dxfattribs={
                    "layer": layer_materials,
                    "color": GREEN,
                    "height": material_height,
                },
            )
            material_label.set_placement(
                (cx, cy - height * 0.5 - material_height * 0.6),
                align=TextEntityAlignment.MIDDLE_CENTER,
            )
            group_entities.append(material_label)

        _create_part_group(doc, part, group_entities)

    for header in headers:
        text = str(header["text"]).strip()
        point = header.get("point")
        if not text or not point:
            continue
        try:
            height = float(header.get("height") or 0.0)
        except (TypeError, ValueError):
            height = 0.0
        header_label = msp.add_text(
            text,
            dxfattribs={
                "layer": layer_material_header,
                "height": height if height > 0.0 else 40.0,
            },
        )
        header_label.set_placement(
            (float(point[0]), float(point[1])),
            align=TextEntityAlignment.MIDDLE_CENTER,
        )

    doc.saveas(output_path)
