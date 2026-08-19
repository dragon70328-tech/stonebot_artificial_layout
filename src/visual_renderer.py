"""DXF 检查图渲染器。

当前只输出 SVG，不依赖 matplotlib/Pillow。渲染结果只用于人工检查或作为
视觉模型的输入，不参与排板几何计算。
"""

from __future__ import annotations

import html
import math
from pathlib import Path

import ezdxf
from ezdxf import bbox


def _escape_text(value: str | None) -> str:
    return html.escape(value or "", quote=True)


def _world_to_svg(
    x: float,
    y: float,
    min_x: float,
    max_y: float,
    scale: float,
    offset_x: float,
    offset_y: float,
) -> tuple[float, float]:
    return ((x - min_x) * scale + offset_x, (max_y - y) * scale + offset_y)


def _stroke_color(entity) -> str:
    dtype = entity.dxftype()
    if dtype == "HATCH":
        return "#059669"
    if dtype in ("LWPOLYLINE", "POLYLINE"):
        return "#111827"
    if dtype in ("CIRCLE", "ARC", "ELLIPSE", "SPLINE"):
        return "#2563eb"
    if dtype == "LINE":
        return "#6b7280"
    return "#9ca3af"


def _entity_is_closed(entity) -> bool:
    dtype = entity.dxftype()
    try:
        if dtype == "LWPOLYLINE":
            if entity.closed:
                return True
            points = list(entity.get_points("xy"))
            if len(points) >= 3:
                first = points[0]
                last = points[-1]
                if math.hypot(first[0] - last[0], first[1] - last[1]) < 1e-6:
                    return True
            return False
        if dtype == "POLYLINE":
            return bool(getattr(entity, "is_closed", False))
        if dtype == "CIRCLE":
            return True
    except Exception:
        return False
    return False


def write_dxf_overview_svg(
    source_filepath: str | Path,
    output_path: str | Path,
    svg_size: int = 1600,
    padding: float = 40.0,
    include_text: bool = True,
    issues: list | None = None,
) -> Path:
    """把 DXF 渲染为 SVG 检查图，并返回输出路径。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = ezdxf.readfile(str(source_filepath))
    msp = doc.modelspace()
    entities = list(msp)

    if not entities:
        min_x = min_y = 0.0
        max_x = max_y = 1.0
    else:
        extents = bbox.extents(entities)
        min_point, max_point = extents
        min_x, min_y = float(min_point.x), float(min_point.y)
        max_x, max_y = float(max_point.x), float(max_point.y)

    span_x = max_x - min_x
    span_y = max_y - min_y
    if span_x <= 0:
        span_x = 1.0
    if span_y <= 0:
        span_y = 1.0

    usable = max(float(svg_size) - 2.0 * padding, 1.0)
    scale = min(usable / span_x, usable / span_y)
    offset_x = (svg_size - span_x * scale) / 2.0
    offset_y = (svg_size - span_y * scale) / 2.0

    svg_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{svg_size}" height="{svg_size}" '
            f'viewBox="0 0 {svg_size} {svg_size}">'
        ),
        '<rect width="100%" height="100%" fill="white"/>',
    ]

    for entity in entities:
        dtype = entity.dxftype()
        try:
            if include_text and dtype in ("TEXT", "MTEXT"):
                insert = entity.dxf.insert
                sx, sy = _world_to_svg(
                    float(insert.x),
                    float(insert.y),
                    min_x,
                    max_y,
                    scale,
                    offset_x,
                    offset_y,
                )
                raw_text = (
                    entity.dxf.text
                    if dtype == "TEXT"
                    else entity.plain_text()
                )
                svg_lines.append(
                    '<text x="%.1f" y="%.1f" font-size="20" '
                    'fill="#dc2626">%s</text>'
                    % (sx, sy, _escape_text(raw_text))
                )
                continue

            path = ezdxf.path.make_path(entity)
            points = list(path.flattening(max(0.5, span_x / 500, span_y / 500)))
            if len(points) < 2:
                continue
            svg_points = []
            for point in points:
                sx, sy = _world_to_svg(
                    float(point.x),
                    float(point.y),
                    min_x,
                    max_y,
                    scale,
                    offset_x,
                    offset_y,
                )
                svg_points.append(f"{sx:.1f},{sy:.1f}")

            color = _stroke_color(entity)
            if _entity_is_closed(entity):
                svg_lines.append(
                    '<polygon points="%s" fill="%s" fill-opacity="0.08" '
                    'stroke="%s" stroke-width="2"/>'
                    % (" ".join(svg_points), color, color)
                )
            else:
                svg_lines.append(
                    '<polyline points="%s" fill="none" '
                    'stroke="%s" stroke-width="2"/>'
                    % (" ".join(svg_points), color)
                )
        except Exception:
            continue

    for issue in issues or []:
        coordinates = getattr(issue, "coordinates", None)
        if coordinates is None:
            continue
        x, y = float(coordinates[0]), float(coordinates[1])
        sx, sy = _world_to_svg(
            x, y, min_x, max_y, scale, offset_x, offset_y
        )
        issue_id = getattr(issue, "issue_id", "")
        issue_type = getattr(issue, "issue_type", "")
        svg_lines.append(
            '<circle cx="%.1f" cy="%.1f" r="12" fill="none" '
            'stroke="#dc2626" stroke-width="4"/>' % (sx, sy)
        )
        svg_lines.append(
            '<text x="%.1f" y="%.1f" font-size="18" fill="#dc2626">#%s %s</text>'
            % (sx + 16, sy - 12, _escape_text(issue_id), _escape_text(issue_type))
        )

    svg_lines.append("</svg>")
    output_path.write_text("\n".join(svg_lines), encoding="utf-8")
    return output_path
