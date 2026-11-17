"""Convert analyzed vector-PDF pages into one multi-layout in-place DXF."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import bulge_to_arc
from shapely.geometry import Polygon

from .models import ArcCurve, BBox, BoardCell, PageModel, PdfIntakeModel, Point

MM_PER_PAPER_UNIT = 1.0
PAPER_WIDTH_MM = 210.0
PAPER_HEIGHT_MM = 297.0
PAPER_MARGIN_MM = 8.0


@dataclass(frozen=True)
class ArcGeometry:
    center: Point
    radius: float
    start: Point
    middle: Point
    end: Point

    @property
    def start_angle(self) -> float:
        return math.degrees(
            math.atan2(self.start[1] - self.center[1], self.start[0] - self.center[0])
        ) % 360.0

    @property
    def end_angle(self) -> float:
        return math.degrees(
            math.atan2(self.end[1] - self.center[1], self.end[0] - self.center[0])
        ) % 360.0

    def bulge(self, start: Point, end: Point) -> float:
        start_angle = math.atan2(
            start[1] - self.center[1], start[0] - self.center[0]
        )
        end_angle = math.atan2(end[1] - self.center[1], end[0] - self.center[0])
        middle_angle = math.atan2(
            self.middle[1] - self.center[1], self.middle[0] - self.center[0]
        )
        ccw_sweep = (end_angle - start_angle) % (2.0 * math.pi)
        middle_delta = (middle_angle - start_angle) % (2.0 * math.pi)
        if middle_delta <= ccw_sweep + 1e-9:
            sweep = ccw_sweep
        else:
            sweep = -((start_angle - end_angle) % (2.0 * math.pi))
        return math.tan(sweep / 4.0)


@dataclass(frozen=True)
class PartGeometry:
    number: str
    source_number: str
    floor: str
    material: str
    vertices: tuple[tuple[float, float, float], ...]
    centroid: Point
    source_row: int
    source_column: int
    is_r450: bool = False


@dataclass
class ConversionAudit:
    output_path: str
    output_sha256: str
    page_count: int
    layout_count: int
    floor_block_count: int
    board_label_count: int
    finished_part_count: int
    r450_boundary_count: int
    vector_entity_count: int
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    page_summaries: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "output_path": self.output_path,
            "output_sha256": self.output_sha256,
            "page_count": self.page_count,
            "layout_count": self.layout_count,
            "floor_block_count": self.floor_block_count,
            "board_label_count": self.board_label_count,
            "finished_part_count": self.finished_part_count,
            "r450_boundary_count": self.r450_boundary_count,
            "vector_entity_count": self.vector_entity_count,
            "issues": self.issues,
            "warnings": self.warnings,
            "page_summaries": self.page_summaries,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _circumcircle(start: Point, middle: Point, end: Point) -> ArcGeometry:
    ax, ay = start
    bx, by = middle
    cx, cy = end
    denominator = 2.0 * (
        ax * (by - cy) + bx * (cy - ay) + cx * (ay - by)
    )
    if abs(denominator) < 1e-9:
        raise ValueError("R450 曲线三点共线，不能生成真圆弧")
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / denominator
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / denominator
    radius = math.hypot(ax - ux, ay - uy)
    return ArcGeometry(
        center=(ux, uy),
        radius=radius,
        start=start,
        middle=middle,
        end=end,
    )


def _transform_point(point: Point, page: PageModel) -> Point:
    return (
        point[0] * page.mm_per_pt,
        (page.height_pt - point[1]) * page.mm_per_pt,
    )


def _transform_rect(rect: BBox, page: PageModel) -> tuple[float, float, float, float]:
    return (
        rect[0] * page.mm_per_pt,
        (page.height_pt - rect[3]) * page.mm_per_pt,
        rect[2] * page.mm_per_pt,
        (page.height_pt - rect[1]) * page.mm_per_pt,
    )


def _sample_curve(points: tuple[Point, ...], page: PageModel) -> list[Point]:
    return [_transform_point(point, page) for point in points]


def _polygon_centroid(points: list[Point]) -> Point:
    polygon = Polygon(points)
    if polygon.is_empty:
        return points[0]
    point = polygon.representative_point()
    return (float(point.x), float(point.y))


def _rectangle_part(cell: BoardCell, page: PageModel) -> PartGeometry:
    left, bottom, _, _ = _transform_rect(cell.rect_pt, page)
    width = cell.label.width_mm
    height = cell.label.height_mm
    vertices = (
        (left, bottom, 0.0),
        (left + width, bottom, 0.0),
        (left + width, bottom + height, 0.0),
        (left, bottom + height, 0.0),
    )
    return PartGeometry(
        number=f"{page.page_map.number_prefix}-{cell.label.number}",
        source_number=str(cell.label.number),
        floor=page.page_map.floor,
        material=cell.material,
        vertices=vertices,
        centroid=(left + width / 2.0, bottom + height / 2.0),
        source_row=cell.row,
        source_column=cell.column,
    )


def _r450_parts(
    cell: BoardCell,
    page: PageModel,
) -> tuple[PartGeometry, PartGeometry, ArcGeometry]:
    if cell.arc is None:
        raise ValueError("没有 R450 曲线")
    left, bottom, right, _ = _transform_rect(cell.rect_pt, page)
    top = bottom + cell.label.height_mm
    curve = _sample_curve(cell.arc.points, page)
    raw_start = curve[0]
    raw_end = curve[-1]
    if cell.arc.side == "left":
        boundary = (left, raw_start[1])
        flat_x = left + 450.0
    else:
        boundary = (right, raw_start[1])
        flat_x = right - 450.0
    direction = 1.0 if raw_end[1] >= boundary[1] else -1.0
    if direction > 0:
        flat_y = bottom + 725.0
        boundary_y = flat_y - 450.0
    else:
        flat_y = top - 725.0
        boundary_y = flat_y + 450.0
    boundary = (boundary[0], boundary_y)
    flat = (flat_x, flat_y)
    center = (flat[0], boundary[1])
    if cell.arc.side == "left":
        sweep_direction = -direction
    else:
        sweep_direction = direction
    start_angle = math.atan2(
        boundary[1] - center[1], boundary[0] - center[0]
    )
    middle_angle = start_angle + sweep_direction * math.pi / 4.0
    middle = (
        center[0] + 450.0 * math.cos(middle_angle),
        center[1] + 450.0 * math.sin(middle_angle),
    )
    geometry = ArcGeometry(
        center=center,
        radius=450.0,
        start=boundary,
        middle=middle,
        end=flat,
    )

    if cell.arc.side == "left":
        reverse_bulge = geometry.bulge(flat, boundary)
        forward_bulge = geometry.bulge(boundary, flat)
        upper_vertices = (
            (left, top, 0.0),
            (right, top, 0.0),
            (right, flat[1], 0.0),
            (flat[0], flat[1], reverse_bulge),
            (left, boundary[1], 0.0),
        )
        lower_vertices = (
            (left, boundary[1], forward_bulge),
            (flat[0], flat[1], 0.0),
            (right, flat[1], 0.0),
            (right, bottom, 0.0),
            (left, bottom, 0.0),
        )
    else:
        reverse_bulge = geometry.bulge(flat, boundary)
        forward_bulge = geometry.bulge(boundary, flat)
        upper_vertices = (
            (left, top, 0.0),
            (right, top, 0.0),
            (right, boundary[1], forward_bulge),
            (flat[0], flat[1], 0.0),
            (left, flat[1], 0.0),
        )
        lower_vertices = (
            (left, flat[1], 0.0),
            (flat[0], flat[1], reverse_bulge),
            (right, boundary[1], 0.0),
            (right, bottom, 0.0),
            (left, bottom, 0.0),
        )

    upper_centroid = _polygon_centroid([(x, y) for x, y, _ in upper_vertices])
    lower_centroid = _polygon_centroid([(x, y) for x, y, _ in lower_vertices])
    if cell.material == "UNKNOWN":
        raise ValueError(
            f"{page.page_map.floor}-{cell.label.number} 材料不确定，R450 拒绝猜测"
        )
    source_number = f"{page.page_map.number_prefix}-{cell.label.number}"
    upper = PartGeometry(
        number=f"{source_number}-ST101",
        source_number=str(cell.label.number),
        floor=page.page_map.floor,
        material="ST-101",
        vertices=upper_vertices,
        centroid=upper_centroid,
        source_row=cell.row,
        source_column=cell.column,
        is_r450=True,
    )
    lower = PartGeometry(
        number=source_number,
        source_number=str(cell.label.number),
        floor=page.page_map.floor,
        material=cell.material,
        vertices=lower_vertices,
        centroid=lower_centroid,
        source_row=cell.row,
        source_column=cell.column,
        is_r450=True,
    )
    return upper, lower, geometry


def _build_parts(page: PageModel) -> tuple[list[PartGeometry], list[ArcGeometry]]:
    parts: list[PartGeometry] = []
    arcs: list[ArcGeometry] = []
    for cell in page.cells:
        if cell.arc is None:
            parts.append(_rectangle_part(cell, page))
            continue
        upper, lower, arc = _r450_parts(cell, page)
        parts.extend((upper, lower))
        arcs.append(arc)
    return parts, arcs


def _sample_bulged_vertices(
    vertices: tuple[tuple[float, float, float], ...],
    segments: int = 32,
) -> list[Point]:
    result: list[Point] = []
    for index, (x, y, bulge) in enumerate(vertices):
        start = (x, y)
        next_vertex = vertices[(index + 1) % len(vertices)]
        end = (next_vertex[0], next_vertex[1])
        if abs(bulge) < 1e-12:
            result.append(start)
            continue
        center, _, _, radius = bulge_to_arc(start, end, bulge)
        start_angle = math.atan2(start[1] - center.y, start[0] - center.x)
        included = 4.0 * math.atan(abs(bulge))
        direction = 1.0 if bulge > 0 else -1.0
        for step in range(segments):
            angle = start_angle + direction * included * step / segments
            result.append(
                (
                    center.x + radius * math.cos(angle),
                    center.y + radius * math.sin(angle),
                )
            )
    return result


def validate_model_geometry(model: PdfIntakeModel) -> list[str]:
    """Validate finished-part envelopes and R450 pair partitioning."""

    issues: list[str] = []
    numbers: set[str] = set()
    for page in model.pages:
        for cell in page.cells:
            if cell.arc is None:
                part = _rectangle_part(cell, page)
                xs = [vertex[0] for vertex in part.vertices]
                ys = [vertex[1] for vertex in part.vertices]
                width = max(xs) - min(xs)
                height = max(ys) - min(ys)
                if abs(width - cell.label.width_mm) > 0.01:
                    issues.append(f"{part.number}: 零件长度不等于标注值")
                if abs(height - cell.label.height_mm) > 0.01:
                    issues.append(f"{part.number}: 零件宽度不等于标注值")
                if part.number in numbers:
                    issues.append(f"{part.number}: 板号重复")
                numbers.add(part.number)
                continue

            upper, lower, arc = _r450_parts(cell, page)
            if abs(arc.radius - 450.0) > 3.0:
                issues.append(f"{lower.number}: R450 半径超出容差")
            envelope = Polygon(
                [
                    (upper.vertices[0][0], upper.vertices[0][1]),
                    (upper.vertices[1][0], upper.vertices[1][1]),
                    (lower.vertices[3][0], lower.vertices[3][1]),
                    (lower.vertices[4][0], lower.vertices[4][1]),
                ]
            )
            upper_polygon = Polygon(_sample_bulged_vertices(upper.vertices))
            lower_polygon = Polygon(_sample_bulged_vertices(lower.vertices))
            if upper_polygon.is_empty or lower_polygon.is_empty:
                issues.append(f"{lower.number}: R450 分块为空")
                continue
            if not envelope.buffer(0.1).covers(upper_polygon):
                issues.append(f"{upper.number}: 弧形件超出包络")
            if not envelope.buffer(0.1).covers(lower_polygon):
                issues.append(f"{lower.number}: 纹理件超出包络")
            if upper_polygon.intersection(lower_polygon).area > 0.5:
                issues.append(f"{lower.number}: R450 两块面积重叠")
            area_delta = abs(
                upper_polygon.area
                + lower_polygon.area
                - envelope.area
            )
            if area_delta > 5.0:
                issues.append(
                    f"{lower.number}: R450 两块面积和偏离包络 {area_delta:.3f}mm²"
                )
            upper_bounds = [
                (vertex[0], vertex[1]) for vertex in upper.vertices
            ]
            lower_bounds = [
                (vertex[0], vertex[1]) for vertex in lower.vertices
            ]
            upper_height = max(y for _, y in upper_bounds) - min(
                y for _, y in upper_bounds
            )
            lower_height = max(y for _, y in lower_bounds) - min(
                y for _, y in lower_bounds
            )
            if min(upper_height, lower_height) < 725.0 - 1e-6 or min(
                upper_height, lower_height
            ) > 725.0 + 1e-6:
                issues.append(
                    f"{lower.number}: 725mm 弧形分块高度不等于 725.000mm"
                )
            for part_number in (upper.number, lower.number):
                if part_number in numbers:
                    issues.append(f"{part_number}: 板号重复")
                numbers.add(part_number)
    return issues


def _layer(doc, name: str, color: int, linetype: str = "CONTINUOUS") -> None:
    if name not in doc.layers:
        doc.layers.add(name, color=color, linetype=linetype)


def _add_vector_item(layout, item, page: PageModel, layer: str) -> int:
    operation = item[0]
    if operation == "l":
        start = _transform_point(
            (float(item[1].x), float(item[1].y)),
            page,
        )
        end = _transform_point(
            (float(item[2].x), float(item[2].y)),
            page,
        )
        layout.add_line(start, end, dxfattribs={"layer": layer})
        return 1
    if operation == "re":
        x0, y0, x1, y1 = item[1]
        points = [
            _transform_point((float(x0), float(y0)), page),
            _transform_point((float(x1), float(y0)), page),
            _transform_point((float(x1), float(y1)), page),
            _transform_point((float(x0), float(y1)), page),
        ]
        layout.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})
        return 1
    if operation == "qu":
        quad = item[1]
        if hasattr(quad, "ll"):
            raw = [quad.ll, quad.lr, quad.ur, quad.ul]
        else:
            raw = [item[1], item[2], item[3]]
        points = [
            _transform_point((float(point.x), float(point.y)), page)
            for point in raw
        ]
        layout.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})
        return 1
    if operation == "c":
        controls = [
            _transform_point((float(item[index].x), float(item[index].y)), page)
            for index in range(1, 5)
        ]
        layout.add_open_spline(controls, degree=3, dxfattribs={"layer": layer})
        return 1
    raise ValueError(f"不支持的 PDF 矢量操作：{operation}")


def _flush_line_chain(
    layout,
    chain: list[Point],
    layer: str,
) -> int:
    if len(chain) < 2:
        return 0
    if len(chain) == 2:
        layout.add_line(chain[0], chain[1], dxfattribs={"layer": layer})
        return 1
    closed = math.dist(chain[0], chain[-1]) < 1e-6
    points = chain[:-1] if closed else chain
    layout.add_lwpolyline(
        points,
        close=closed,
        dxfattribs={"layer": layer},
    )
    return 1


def _vector_layer(drawing) -> str:
    width = float(drawing.get("width") or 0.0)
    if width < 0.2:
        return "PDF_TEXT_VECTOR"
    if drawing.get("fill") is not None and width <= 0.25:
        return "HATCH"
    return "PDF_VECTOR"


def _write_pdf_vectors(layout, page_model: PageModel, source_page) -> int:
    count = 0
    for drawing in source_page.get_drawings():
        layer = _vector_layer(drawing)
        chain: list[Point] = []
        for item in drawing["items"]:
            if item[0] != "l":
                count += _flush_line_chain(layout, chain, layer)
                chain = []
                count += _add_vector_item(layout, item, page_model, layer)
                continue
            start = _transform_point(
                (float(item[1].x), float(item[1].y)),
                page_model,
            )
            end = _transform_point(
                (float(item[2].x), float(item[2].y)),
                page_model,
            )
            if not chain:
                chain = [start, end]
            elif math.dist(chain[-1], start) < 1e-6:
                chain.append(end)
            elif math.dist(chain[-1], end) < 1e-6:
                chain.append(start)
            else:
                count += _flush_line_chain(layout, chain, layer)
                chain = [start, end]
        count += _flush_line_chain(layout, chain, layer)
    return count


def _set_part_xdata(entity, part: PartGeometry, source_pdf: str) -> None:
    entity.set_xdata(
        "STONEBOT",
        [
            (1000, part.number),
            (1000, part.source_number),
            (1000, part.floor),
            (1000, part.material),
            (1000, source_pdf),
            (1070, 1 if part.is_r450 else 0),
            (1070, part.source_row),
            (1070, part.source_column),
        ],
    )


def _add_part(
    doc,
    layout,
    part: PartGeometry,
    source_pdf: str,
) -> tuple:
    outline = layout.add_lwpolyline(
        part.vertices,
        format="xyb",
        close=True,
        dxfattribs={"layer": "OUTER", "color": 7},
    )
    _set_part_xdata(outline, part, source_pdf)
    number = layout.add_text(
        part.number,
        height=58.0,
        dxfattribs={"layer": "NUMBERS", "color": 2},
    )
    number.set_placement(part.centroid, align=TextEntityAlignment.MIDDLE_CENTER)
    material = layout.add_text(
        f"{part.material} 20mm",
        height=38.0,
        dxfattribs={"layer": "MATERIALS", "color": 3},
    )
    material.set_placement(
        (part.centroid[0], part.centroid[1] - 75.0),
        align=TextEntityAlignment.MIDDLE_CENTER,
    )
    group = doc.groups.new(part.number)
    group.extend((outline, number, material))
    return outline, number, material


def _add_arc_boundary(layout, arc: ArcGeometry):
    start = arc.start_angle
    end = arc.end_angle
    cross = math.sin(math.radians(end - start))
    if arc.middle[0] < min(arc.start[0], arc.end[0]) or arc.middle[1] < min(
        arc.start[1], arc.end[1]
    ):
        pass
    return layout.add_arc(
        center=arc.center,
        radius=arc.radius,
        start_angle=start,
        end_angle=end,
        is_counter_clockwise=cross >= 0,
        dxfattribs={"layer": "R450_BOUNDARY", "color": 1},
    )


def _offset_part(
    part: PartGeometry,
    dx: float,
    dy: float,
) -> PartGeometry:
    return PartGeometry(
        number=part.number,
        source_number=part.source_number,
        floor=part.floor,
        material=part.material,
        vertices=tuple((x + dx, y + dy, bulge) for x, y, bulge in part.vertices),
        centroid=(part.centroid[0] + dx, part.centroid[1] + dy),
        source_row=part.source_row,
        source_column=part.source_column,
        is_r450=part.is_r450,
    )


def _offset_arc(arc: ArcGeometry, dx: float, dy: float) -> ArcGeometry:
    def move(point: Point) -> Point:
        return (point[0] + dx, point[1] + dy)

    return ArcGeometry(
        center=move(arc.center),
        radius=arc.radius,
        start=move(arc.start),
        middle=move(arc.middle),
        end=move(arc.end),
    )


def _add_native_words(layout, page: PageModel) -> None:
    label_number_boxes = [
        cell.label.number_bbox for cell in page.cells
    ] + [cell.label.size_bbox for cell in page.cells]

    def is_label_box(bbox: BBox) -> bool:
        center = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
        return any(
            label[0] - 1.0 <= center[0] <= label[2] + 1.0
            and label[1] - 1.0 <= center[1] <= label[3] + 1.0
            for label in label_number_boxes
        )

    for word in page.words:
        if is_label_box(word.bbox):
            continue
        raw_height_pt = word.bbox[3] - word.bbox[1]
        if raw_height_pt > 10.0:
            # Rotated title-block watermarks and 90-degree labels have a page-sized
            # bounding box in PyMuPDF and would be enlarged into unusable text.
            continue
        position = _transform_point(word.center, page)
        height = min(
            160.0,
            max(18.0, raw_height_pt * page.mm_per_pt),
        )
        text = word.text
        if any(character.isalpha() or "\u4e00" <= character <= "\u9fff" for character in text):
            layer = "TEXT"
        elif any(character.isdigit() for character in text):
            layer = "DIMENSIONS"
        else:
            continue
        entity = layout.add_text(
            text,
            height=height,
            dxfattribs={"layer": layer, "color": 4},
        )
        entity.set_placement(position, align=TextEntityAlignment.MIDDLE_CENTER)


def _configure_layers(doc) -> None:
    _layer(doc, "PDF_VECTOR", 8)
    _layer(doc, "PDF_TEXT_VECTOR", 8)
    _layer(doc, "PDF_BACKDROP", 8)
    _layer(doc, "OUTER", 7)
    _layer(doc, "R450_BOUNDARY", 1)
    _layer(doc, "R450_ENVELOPE", 6)
    _layer(doc, "NUMBERS", 2)
    _layer(doc, "MATERIALS", 3)
    _layer(doc, "DIMENSIONS", 4)
    _layer(doc, "TEXT", 5)
    _layer(doc, "HATCH", 8)
    _layer(doc, "CHECK_ISSUES", 1)
    control_layer = doc.layers.get("R450_BOUNDARY")
    control_layer.dxf.color = 7
    control_layer.off()
    control_layer.freeze()
    if "STONEBOT" not in doc.appids:
        doc.appids.add("STONEBOT")


def convert_model_to_dxf(
    model: PdfIntakeModel,
    output_path: str | Path,
) -> ConversionAudit:
    """Write all PDF pages as floor blocks and matching A4 paper layouts."""

    import pymupdf

    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    geometry_issues = validate_model_geometry(model)
    if geometry_issues:
        joined = "\n".join(geometry_issues[:20])
        raise ValueError(f"加工几何预检失败：\n{joined}")
    doc = ezdxf.new("R2018", setup=True)
    doc.units = ezdxf.units.MM
    doc.header["$MEASUREMENT"] = 1
    doc.header["$INSUNITS"] = 4
    doc.header["$LUNITS"] = 2
    doc.header["$LUPREC"] = 2
    doc.header["$DWGCODEPAGE"] = "ANSI_936"
    _configure_layers(doc)
    dimstyle = doc.dimstyles.add("STONEBOT_MM")
    dimstyle.dxf.dimlfac = 1.0
    dimstyle.dxf.dimscale = 1.0
    dimstyle.dxf.dimlunit = 2
    dimstyle.dxf.dimdec = 2
    dimstyle.dxf.dimzin = 8
    dimstyle.dxf.dimtxt = 60.0
    dimstyle.dxf.dimasz = 40.0
    doc.header["$DIMSTYLE"] = "STONEBOT_MM"
    existing_paper_layouts = [
        name for name in doc.layouts.names() if name.lower() != "model"
    ]

    msp = doc.modelspace()
    placements: list[tuple[PageModel, str, tuple[float, float], tuple[float, float]]] = []
    vector_entity_count = 0
    part_count = 0
    arc_count = 0
    issues: list[str] = []
    warnings: list[str] = []
    page_summaries: list[dict] = []

    with pymupdf.open(model.source_path) as source_doc:
        y_offset = 0.0
        max_page_width = 0.0
        for page in model.pages:
            block_name = page.page_map.block_name
            if block_name in doc.blocks:
                raise ValueError(f"楼层块重复：{block_name}")
            block = doc.blocks.new(name=block_name)
            source_page = source_doc[page.page_map.page_number - 1]
            vector_entity_count += _write_pdf_vectors(block, page, source_page)

            parts, arcs = _build_parts(page)
            _add_native_words(block, page)

            for cell in page.cells:
                if cell.material == "UNKNOWN":
                    issue = (
                        f"{page.page_map.floor}-{cell.label.number}: "
                        "材料纹理无法确定"
                    )
                    issues.append(issue)
                    center = _transform_point(cell.label.center, page)
                    block.add_text(
                        issue,
                        height=70.0,
                        dxfattribs={"layer": "CHECK_ISSUES", "color": 1},
                    ).set_placement(center)
                for issue_code in cell.issues:
                    if issue_code == "material_pattern_ambiguous":
                        continue
                    message_sink = (
                        warnings
                        if issue_code == "material_inferred_from_floor_template"
                        else issues
                    )
                    message_sink.append(
                        f"{page.page_map.floor}-{cell.label.number}: {issue_code}"
                    )

            page_width = page.width_pt * page.mm_per_pt
            page_height = page.height_pt * page.mm_per_pt
            max_page_width = max(max_page_width, page_width)
            insert = (0.0, y_offset)
            msp.add_blockref(
                block_name,
                insert=insert,
                dxfattribs={"layer": "0"},
            )
            r450_groups: dict[tuple[int, int], list[PartGeometry]] = {}
            for part in parts:
                if part.is_r450:
                    r450_groups.setdefault(
                        (part.source_row, part.source_column), []
                    ).append(part)
                _add_part(
                    doc,
                    msp,
                    _offset_part(part, insert[0], insert[1]),
                    model.source_path,
                )
                part_count += 1
            for arc, paired_parts in zip(arcs, r450_groups.values()):
                arc_entity = _add_arc_boundary(
                    msp,
                    _offset_arc(arc, insert[0], insert[1]),
                )
                arc_count += 1
                for part in paired_parts:
                    part_group = doc.groups.get(part.number)
                    if part_group is not None:
                        part_group.extend((arc_entity,))
            for paired_parts in r450_groups.values():
                xs = [
                    vertex[0] + insert[0]
                    for part in paired_parts
                    for vertex in part.vertices
                ]
                ys = [
                    vertex[1] + insert[1]
                    for part in paired_parts
                    for vertex in part.vertices
                ]
                msp.add_lwpolyline(
                    [
                        (min(xs), min(ys)),
                        (max(xs), min(ys)),
                        (max(xs), max(ys)),
                        (min(xs), max(ys)),
                    ],
                    close=True,
                    dxfattribs={"layer": "R450_ENVELOPE", "color": 6},
                )
            placements.append(
                (
                    page,
                    block_name,
                    insert,
                    (page_width, page_height),
                )
            )
            page_summaries.append(
                {
                    "page_number": page.page_map.page_number,
                    "floor": page.page_map.floor,
                    "number_prefix": page.page_map.number_prefix,
                    "block_name": block_name,
                    "board_labels": len(page.cells),
                    "finished_parts": len(parts),
                    "r450_boundaries": len(arcs),
                    "mm_per_pdf_point": round(page.mm_per_pt, 6),
                }
            )
            y_offset -= page_height + 2000.0

    reusable_layout = (
        existing_paper_layouts[0] if existing_paper_layouts else None
    )
    for index, (page, block_name, insert, (page_width, page_height)) in enumerate(
        placements
    ):
        if index == 0 and reusable_layout is not None:
            doc.layouts.rename(reusable_layout, page.page_map.floor)
            layout = doc.layouts.get(page.page_map.floor)
        else:
            layout = doc.layouts.new(page.page_map.floor)
        layout.page_setup(
            size=(PAPER_WIDTH_MM, PAPER_HEIGHT_MM),
            margins=(
                PAPER_MARGIN_MM,
                PAPER_MARGIN_MM,
                PAPER_MARGIN_MM,
                PAPER_MARGIN_MM,
            ),
        )
        layout.reset_main_viewport(
            center=(PAPER_WIDTH_MM / 2.0, PAPER_HEIGHT_MM / 2.0),
            size=(
                PAPER_WIDTH_MM - 2.0 * PAPER_MARGIN_MM,
                PAPER_HEIGHT_MM - 2.0 * PAPER_MARGIN_MM,
            ),
        )
        viewport = layout.main_viewport()
        if viewport is None:
            raise RuntimeError(f"布局 {page.page_map.floor} 未能创建主视口")
        viewport.dxf.view_center_point = (
            insert[0] + page_width / 2.0,
            insert[1] + page_height / 2.0,
            0.0,
        )
        viewport.dxf.view_height = page_height * 1.03
        viewport.dxf.status = 2
        layout.add_text(
            page.page_map.floor,
            height=3.5,
            dxfattribs={"layer": "TEXT", "color": 7},
        ).set_placement(
            (PAPER_MARGIN_MM, PAPER_HEIGHT_MM - PAPER_MARGIN_MM / 2.0)
        )

    width = max_page_width * 1.05 if max_page_width else 1000.0
    doc.set_modelspace_vport(
        height=max(abs(y_offset), 1000.0),
        center=(max_page_width / 2.0, y_offset / 2.0),
    )
    doc.saveas(target)

    return ConversionAudit(
        output_path=str(target),
        output_sha256=_sha256(target),
        page_count=len(model.pages),
        layout_count=len(placements),
        floor_block_count=len(placements),
        board_label_count=model.board_label_count,
        finished_part_count=part_count,
        r450_boundary_count=arc_count,
        vector_entity_count=vector_entity_count,
        issues=issues,
        warnings=warnings,
        page_summaries=page_summaries,
    )


def validate_converted_dxf(
    path: str | Path,
    *,
    expected_pages: int | None = None,
    expected_board_labels: int | None = None,
    expected_r450: int | None = None,
) -> list[str]:
    """Read back the delivered DXF and return explicit validation failures."""

    issues: list[str] = []
    doc = ezdxf.readfile(str(Path(path).expanduser().resolve()))
    layout_names = [
        name for name in doc.layouts.names() if name.lower() != "model"
    ]
    floor_blocks = [
        block.name
        for block in doc.blocks
        if block.name.upper().startswith("FLOOR_")
    ]
    if expected_pages is not None and len(layout_names) != expected_pages:
        issues.append(
            f"图纸布局数量 {len(layout_names)}，期望 {expected_pages}"
        )
    if expected_pages is not None and len(floor_blocks) != expected_pages:
        issues.append(
            f"楼层块数量 {len(floor_blocks)}，期望 {expected_pages}"
        )

    total_parts = 0
    total_arcs = 0
    msp = doc.modelspace()
    outlines = [
        entity
        for entity in msp
        if entity.dxftype() == "LWPOLYLINE"
        and entity.dxf.layer == "OUTER"
    ]
    arcs = [
        entity
        for entity in msp
        if entity.dxftype() == "ARC"
        and entity.dxf.layer == "R450_BOUNDARY"
    ]
    total_parts = len(outlines)
    total_arcs = len(arcs)
    for block_name in floor_blocks:
        block = doc.blocks[block_name]
        if not list(block):
            issues.append(f"{block_name} 楼层块没有 PDF 矢量内容")
    for outline in outlines:
        if not outline.has_xdata("STONEBOT"):
            issues.append("存在缺少 STONEBOT XDATA 的 OUTER")
            break

    for layout_name in layout_names:
        layout = doc.layouts.get(layout_name)
        if not layout.viewports():
            issues.append(f"布局 {layout_name} 没有视口")

    if expected_r450 is not None and total_arcs != expected_r450:
        issues.append(
            f"R450 真圆弧数量 {total_arcs}，期望 {expected_r450}"
        )
    if expected_board_labels is not None and total_parts < expected_board_labels:
        issues.append(
            f"加工件数量 {total_parts} 少于原图板号 {expected_board_labels}"
        )
    return issues
