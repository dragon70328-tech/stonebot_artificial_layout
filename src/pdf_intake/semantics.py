"""Board, material and R450 semantics recovered from vector-PDF pages."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path
from statistics import median

from .models import (
    ArcCurve,
    BBox,
    BoardCell,
    BoardLabel,
    PageMap,
    PageModel,
    PdfIntakeModel,
    PdfWord,
    Point,
)
from .preflight import inspect_pdf

SIZE_RE = re.compile(
    r"\(?\s*(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)\s*\)?"
)
POINT_EPS = 0.05


def _bbox(values: list[Point]) -> BBox:
    return (
        min(point[0] for point in values),
        min(point[1] for point in values),
        max(point[0] for point in values),
        max(point[1] for point in values),
    )


def _bbox_center(bbox: BBox) -> Point:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _point_distance(left: Point, right: Point) -> float:
    return math.hypot(left[0] - right[0], left[1] - right[1])


def _contains(rect: BBox, point: Point, tolerance: float = 0.0) -> bool:
    return (
        rect[0] - tolerance <= point[0] <= rect[2] + tolerance
        and rect[1] - tolerance <= point[1] <= rect[3] + tolerance
    )


def _cluster(values: list[float], tolerance: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - groups[-1][-1] > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups if group]


def _sample_cubic(
    start: Point,
    control1: Point,
    control2: Point,
    end: Point,
    segments: int = 20,
) -> list[Point]:
    result: list[Point] = []
    for index in range(segments + 1):
        t = index / segments
        mt = 1.0 - t
        result.append(
            (
                mt**3 * start[0]
                + 3 * mt**2 * t * control1[0]
                + 3 * mt * t**2 * control2[0]
                + t**3 * end[0],
                mt**3 * start[1]
                + 3 * mt**2 * t * control1[1]
                + 3 * mt * t**2 * control2[1]
                + t**3 * end[1],
            )
        )
    return result


def _words(page, clip: BBox | None = None) -> list[PdfWord]:
    raw_words = page.get_text("words", clip=clip)
    result: list[PdfWord] = []
    for item in raw_words:
        text = str(item[4]).strip()
        if not text:
            continue
        bbox = (float(item[0]), float(item[1]), float(item[2]), float(item[3]))
        result.append(PdfWord(text=text, bbox=bbox, center=_bbox_center(bbox)))
    return result


def _extract_labels(page) -> list[BoardLabel]:
    words = _words(page)
    number_words = [
        word
        for word in words
        if word.text.isdigit()
        and 1 <= int(word.text) <= 999
        and 100.0 < word.center[0] < 450.0
        and 25.0 < word.center[1] < 760.0
    ]
    size_words = [
        word
        for word in words
        if SIZE_RE.search(word.text)
        and 100.0 < word.center[0] < 450.0
        and 25.0 < word.center[1] < 780.0
    ]

    labels: list[BoardLabel] = []
    used_numbers: set[int] = set()
    for size_word in size_words:
        match = SIZE_RE.search(size_word.text)
        if not match:
            continue
        candidates = [
            word
            for word in number_words
            if int(word.text) not in used_numbers
            and abs(word.center[0] - size_word.center[0]) <= 8.0
            and -5.0 <= size_word.bbox[1] - word.bbox[3] <= 18.0
        ]
        if not candidates:
            continue
        number_word = min(
            candidates,
            key=lambda word: (
                abs(word.center[0] - size_word.center[0]),
                abs(word.center[1] - size_word.center[1]),
            ),
        )
        used_numbers.add(int(number_word.text))
        width_mm = float(match.group(1).replace(",", "."))
        height_mm = float(match.group(2).replace(",", "."))
        labels.append(
            BoardLabel(
                number=int(number_word.text),
                width_mm=width_mm,
                height_mm=height_mm,
                number_bbox=number_word.bbox,
                size_bbox=size_word.bbox,
                center=(
                    size_word.center[0],
                    (number_word.bbox[1] + size_word.bbox[3]) / 2.0,
                ),
            )
        )
    return sorted(labels, key=lambda label: label.number)


def _column_centers(labels: list[BoardLabel]) -> list[float]:
    x_values = sorted(label.center[0] for label in labels)
    if len(x_values) < 4:
        raise ValueError("PDF 页面未识别到四列板号")
    groups: list[list[float]] = []
    for value in x_values:
        if not groups or value - groups[-1][-1] > 10.0:
            groups.append([value])
        else:
            groups[-1].append(value)
    centers = [median(group) for group in groups if len(group) >= 2]
    if len(centers) != 4:
        centers = [median(group) for group in groups]
    if len(centers) != 4:
        # Fallback for a page with many extra numeric words.
        min_x = min(x_values)
        max_x = max(x_values)
        pitch = (max_x - min_x) / 3.0
        centers = [min_x + pitch * index for index in range(4)]
    return centers


def _plan_lines(
    drawings: list[dict],
    column_centers: list[float],
) -> tuple[BBox, list[float]]:
    pitch = median(
        [
            column_centers[index + 1] - column_centers[index]
            for index in range(len(column_centers) - 1)
        ]
    )
    nominal_x0 = column_centers[0] - 0.55 * pitch
    nominal_x1 = column_centers[-1] + 0.55 * pitch
    horizontal: list[tuple[float, float, float]] = []
    vertical: list[tuple[float, float, float]] = []

    for drawing in drawings:
        width = float(drawing.get("width") or 0.0)
        if abs(width - 0.72) > 0.04:
            continue
        for item in drawing["items"]:
            if item[0] != "l":
                continue
            start = (float(item[1].x), float(item[1].y))
            end = (float(item[2].x), float(item[2].y))
            if abs(start[1] - end[1]) < POINT_EPS:
                x0, x1 = sorted((start[0], end[0]))
                overlap = min(x1, nominal_x1) - max(x0, nominal_x0)
                if overlap > 0.7 * (nominal_x1 - nominal_x0):
                    horizontal.append(((start[1] + end[1]) / 2.0, x0, x1))
            elif abs(start[0] - end[0]) < POINT_EPS:
                y0, y1 = sorted((start[1], end[1]))
                if nominal_x0 - 5.0 <= start[0] <= nominal_x1 + 5.0 and y1 - y0 > 15:
                    vertical.append(((start[0] + end[0]) / 2.0, y0, y1))

    if not vertical:
        raise ValueError("PDF 页面未识别到平面图竖向边界")
    y_min = median([line[1] for line in vertical if line[2] - line[1] > 0.4 * 600])
    y_max = median([line[2] for line in vertical if line[2] - line[1] > 0.4 * 600])
    y_values = [line[0] for line in horizontal]
    y_values.extend((y_min, y_max))
    y_values = [value for value in y_values if y_min - 1.0 <= value <= y_max + 1.0]
    boundaries = _cluster(y_values, 0.6)
    return (nominal_x0, y_min, nominal_x1, y_max), boundaries


def _row_bands(
    labels: list[BoardLabel],
    y_boundaries: list[float],
    mm_per_pt: float,
) -> list[tuple[float, float, list[BoardLabel]]]:
    ordered_labels = sorted(labels, key=lambda label: label.center[1])
    clusters: list[list[BoardLabel]] = []
    for label in ordered_labels:
        if (
            not clusters
            or abs(label.center[1] - clusters[-1][-1].center[1]) > 8.0
        ):
            clusters.append([label])
        else:
            clusters[-1].append(label)

    bands: list[tuple[float, float, list[BoardLabel]]] = []
    for cluster in clusters:
        label = cluster[0]
        y = label.center[1]
        lower = max(
            (boundary for boundary in y_boundaries if boundary <= y + 4.0),
            default=None,
        )
        upper = min(
            (boundary for boundary in y_boundaries if boundary >= y - 4.0),
            default=None,
        )
        if lower is None or upper is None or upper - lower < 3.0:
            previous_bottom = max(
                (band[1] for band in bands), default=y_boundaries[-1]
            )
            top = previous_bottom - 1.0
            bottom = top + label.height_mm / mm_per_pt
        else:
            top, bottom = lower, upper
        bands.append((top, bottom, cluster))
    return bands


def _curve_candidates(drawings: list[dict], cell_rect: BBox) -> list[ArcCurve]:
    candidates: list[ArcCurve] = []

    for drawing in drawings:
        width = float(drawing.get("width") or 0.0)
        if abs(width - 0.72) > 0.04:
            continue
        cubic_points: list[Point] = []
        for item in drawing["items"]:
            if item[0] != "c":
                continue
            points = _sample_cubic(
                (float(item[1].x), float(item[1].y)),
                (float(item[2].x), float(item[2].y)),
                (float(item[3].x), float(item[3].y)),
                (float(item[4].x), float(item[4].y)),
            )
            if cubic_points and _point_distance(cubic_points[-1], points[0]) < 0.5:
                cubic_points.extend(points[1:])
            else:
                cubic_points.extend(points)
        if len(cubic_points) < 4:
            continue
        curve_bbox = _bbox(cubic_points)
        if not (
            14.0 < curve_bbox[2] - curve_bbox[0] < 27.0
            and 14.0 < curve_bbox[3] - curve_bbox[1] < 27.0
        ):
            continue
        center = _bbox_center(curve_bbox)
        if not _contains(cell_rect, center, tolerance=1.0):
            continue
        side = ""
        if abs(curve_bbox[0] - cell_rect[0]) < 2.0:
            side = "left"
        elif abs(curve_bbox[2] - cell_rect[2]) < 2.0:
            side = "right"
        if side:
            candidates.append(ArcCurve(tuple(cubic_points), curve_bbox, side))

    if candidates:
        # CAD PDFs often repeat one path three times for fill/stroke grouping.
        unique: dict[tuple[int, int, int, int], ArcCurve] = {}
        for candidate in candidates:
            key = tuple(round(value, 1) for value in candidate.bbox)
            unique.setdefault(key, candidate)
        return list(unique.values())

    # Some exporters flatten the arc to a short ten-segment polyline.
    flattened: list[ArcCurve] = []
    for drawing in drawings:
        width = float(drawing.get("width") or 0.0)
        if abs(width - 0.72) > 0.04:
            continue
        items = drawing["items"]
        if not 8 <= len(items) <= 14 or any(item[0] != "l" for item in items):
            continue
        rect = drawing["rect"]
        bbox = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
        if not (
            14.0 < bbox[2] - bbox[0] < 27.0
            and 14.0 < bbox[3] - bbox[1] < 27.0
            and _contains(cell_rect, _bbox_center(bbox), tolerance=1.0)
        ):
            continue
        chains: list[list[Point]] = []
        for item in items:
            a = (float(item[1].x), float(item[1].y))
            b = (float(item[2].x), float(item[2].y))
            if not chains:
                chains.append([a, b])
                continue
            if _point_distance(chains[-1][-1], a) < 0.3:
                chains[-1].append(b)
            elif _point_distance(chains[-1][-1], b) < 0.3:
                chains[-1].append(a)
            else:
                chains.append([a, b])
        for points in chains:
            if len(points) < 8:
                continue
            side = ""
            if abs(bbox[0] - cell_rect[0]) < 2.0:
                side = "left"
            elif abs(bbox[2] - cell_rect[2]) < 2.0:
                side = "right"
            if side:
                flattened.append(ArcCurve(tuple(points), bbox, side))
    return flattened


def _orient_curve_to_cell(curve: ArcCurve, cell_rect: BBox) -> ArcCurve:
    boundary_x = cell_rect[0] if curve.side == "left" else cell_rect[2]
    if abs(curve.points[-1][0] - boundary_x) < abs(curve.points[0][0] - boundary_x):
        points = tuple(reversed(curve.points))
    else:
        points = curve.points
    return ArcCurve(points=points, bbox=curve.bbox, side=curve.side)


def _classify_material(drawings: list[dict], rect: BBox) -> str:
    diagonal_long = 0
    vertical_short = 0
    dots = 0
    short_segments = 0

    for drawing in drawings:
        for item in drawing["items"]:
            if item[0] != "l":
                continue
            start = (float(item[1].x), float(item[1].y))
            end = (float(item[2].x), float(item[2].y))
            length = _point_distance(start, end)
            midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
            if not _contains(rect, midpoint):
                continue
            if length > 25.0:
                continue
            if (
                abs(start[0] - rect[0]) < 0.15
                and abs(end[0] - rect[0]) < 0.15
            ) or (
                abs(start[0] - rect[2]) < 0.15
                and abs(end[0] - rect[2]) < 0.15
            ):
                continue
            if (
                abs(start[1] - rect[1]) < 0.15
                and abs(end[1] - rect[1]) < 0.15
            ) or (
                abs(start[1] - rect[3]) < 0.15
                and abs(end[1] - rect[3]) < 0.15
            ):
                continue
            if length < 0.06:
                dots += 1
                short_segments += 1
                continue
            if length <= 3.0:
                short_segments += 1
            angle = math.degrees(
                math.atan2(end[1] - start[1], end[0] - start[0])
            ) % 180.0
            if 4.0 <= length <= 8.0 and 115.0 <= angle <= 145.0:
                diagonal_long += 1
            elif 1.8 <= length <= 2.6 and 80.0 <= angle <= 100.0:
                vertical_short += 1

    if diagonal_long >= 20:
        return "ST-102"
    if dots >= 20:
        return "ST-104"
    if vertical_short >= 8:
        return "ST-103"
    if short_segments <= 8:
        return "ST-101"
    return "UNKNOWN"


def _analyze_page(page, page_map: PageMap) -> PageModel:
    drawings = page.get_drawings()
    labels = _extract_labels(page)
    if not labels:
        raise ValueError(f"第 {page_map.page_number} 页未识别到板号和规格")

    column_centers = _column_centers(labels)
    gaps = [
        column_centers[index + 1] - column_centers[index]
        for index in range(3)
    ]
    column_width = median(gaps)
    mm_per_pt = 1197.0 / column_width
    plan_rect, y_boundaries = _plan_lines(drawings, column_centers)
    row_bands = _row_bands(labels, y_boundaries, mm_per_pt)

    x_boundaries = [
        column_centers[0] - column_width / 2.0,
        *[
            (column_centers[index] + column_centers[index + 1]) / 2.0
            for index in range(3)
        ],
        column_centers[-1] + column_width / 2.0,
    ]

    cells: list[BoardCell] = []
    for row_index, (top, bottom, row_labels) in enumerate(row_bands, start=1):
        for label in row_labels:
            column_index = min(
                range(4),
                key=lambda index: abs(label.center[0] - column_centers[index]),
            )
            rect = (
                x_boundaries[column_index],
                top,
                x_boundaries[column_index + 1],
                bottom,
            )
            curves = _curve_candidates(drawings, rect)
            arc = _orient_curve_to_cell(curves[0], rect) if curves else None
            issues = []
            if len(curves) > 1:
                issues.append("multiple_r450_curves_in_cell")
            material = _classify_material(drawings, rect)
            if material == "UNKNOWN":
                issues.append("material_pattern_ambiguous")
            cells.append(
                BoardCell(
                    label=label,
                    row=row_index,
                    column=column_index + 1,
                    rect_pt=rect,
                    material=material,
                    arc=arc,
                    issues=issues,
                )
            )

    drawing_count = 0
    vector_item_count = 0
    for drawing in drawings:
        drawing_count += 1
        vector_item_count += len(drawing["items"])

    return PageModel(
        page_map=page_map,
        width_pt=float(page.rect.width),
        height_pt=float(page.rect.height),
        mm_per_pt=mm_per_pt,
        plan_rect_pt=plan_rect,
        cells=cells,
        words=_words(page),
        drawing_count=drawing_count,
        vector_item_count=vector_item_count,
    )


def analyze_pdf(
    path: str | Path,
    page_maps: list[PageMap] | tuple[PageMap, ...],
) -> PdfIntakeModel:
    """Recover board semantics from every mapped page of a vector PDF."""

    import pymupdf

    preflight = inspect_pdf(path)
    if preflight.encrypted:
        raise ValueError("PDF 已加密，生产转换前必须解除加密")
    if not preflight.is_vector:
        raise ValueError("PDF 不是全矢量图纸，不能生成原位 DXF")
    page_numbers = [item.page_number for item in page_maps]
    if len(page_numbers) != len(set(page_numbers)):
        raise ValueError("页面映射存在重复页码")
    if max(page_numbers, default=0) > preflight.page_count:
        raise ValueError("页面映射超出 PDF 页数")
    if len(page_maps) != preflight.page_count:
        raise ValueError(
            f"PDF 共 {preflight.page_count} 页，但只配置了 {len(page_maps)} 页"
        )

    with pymupdf.open(preflight.source_path) as doc:
        pages = [
            _analyze_page(doc[page_map.page_number - 1], page_map)
            for page_map in sorted(page_maps, key=lambda item: item.page_number)
        ]
    template_materials: dict[tuple[int, int, float, float], list[str]] = defaultdict(
        list
    )
    for page in pages:
        for cell in page.cells:
            if cell.material != "UNKNOWN":
                template_materials[
                    (
                        cell.row,
                        cell.column,
                        cell.label.width_mm,
                        cell.label.height_mm,
                    )
                ].append(cell.material)
    for page in pages:
        for cell in page.cells:
            if cell.material != "UNKNOWN":
                continue
            candidates = set(
                template_materials.get(
                    (
                        cell.row,
                        cell.column,
                        cell.label.width_mm,
                        cell.label.height_mm,
                    ),
                    [],
                )
            )
            if len(candidates) == 1:
                cell.material = next(iter(candidates))
                cell.issues = [
                    issue
                    for issue in cell.issues
                    if issue != "material_pattern_ambiguous"
                ]
                cell.issues.append("material_inferred_from_floor_template")
    return PdfIntakeModel(
        source_path=preflight.source_path,
        source_sha256=preflight.sha256,
        pages=pages,
    )
