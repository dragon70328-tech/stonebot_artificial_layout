"""审图问题的轻量视觉证据生成器。

当前实现不依赖 matplotlib/Pillow，直接输出 SVG，便于在网页端和本地浏览器查看。
"""

from __future__ import annotations

import hashlib
import html
import math
from pathlib import Path

import ezdxf
from ezdxf import bbox

from src.contracts import DrawingIssue


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _world_to_svg(
    x: float,
    y: float,
    min_x: float,
    min_y: float,
    scale: float,
    size: int,
) -> tuple[float, float]:
    return ((x - min_x) * scale, size - (y - min_y) * scale)


def _escape_text(value: str | None) -> str:
    return html.escape(value or "", quote=True)


def _entity_visible(entity, min_x, min_y, max_x, max_y) -> bool:
    try:
        extents = bbox.extents([entity])
    except Exception:
        return False
    if not extents:
        return False
    ex_min, ex_max = extents
    return not (
        ex_max[0] < min_x
        or ex_min[0] > max_x
        or ex_max[1] < min_y
        or ex_min[1] > max_y
    )


def write_issue_evidence_svg(
    source_filepath: str | Path,
    issues: list[DrawingIssue],
    output_dir: str | Path,
    viewport_radius: float = 500.0,
    svg_size: int = 640,
) -> list[dict[str, str]]:
    """为每个问题生成一张裁剪 SVG，并返回证据文件信息。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    issues_with_coordinates = [
        issue for issue in issues if issue.coordinates is not None
    ]
    if not issues_with_coordinates:
        return []

    doc = ezdxf.readfile(str(source_filepath))
    msp = doc.modelspace()
    entities = list(msp)
    results: list[dict[str, str]] = []

    for issue in issues_with_coordinates:
        cx, cy = issue.coordinates
        min_x = cx - viewport_radius
        max_x = cx + viewport_radius
        min_y = cy - viewport_radius
        max_y = cy + viewport_radius
        span_x = max_x - min_x
        span_y = max_y - min_y
        if span_x <= 0 or span_y <= 0:
            continue
        scale = min(svg_size / span_x, svg_size / span_y)

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
            if not _entity_visible(entity, min_x, min_y, max_x, max_y):
                continue
            try:
                if entity.dxftype() in ("TEXT", "MTEXT"):
                    insert = entity.dxf.insert
                    sx, sy = _world_to_svg(
                        float(insert.x),
                        float(insert.y),
                        min_x,
                        min_y,
                        scale,
                        svg_size,
                    )
                    raw_text = (
                        entity.dxf.text
                        if entity.dxftype() == "TEXT"
                        else entity.plain_text()
                    )
                    svg_lines.append(
                        '<text x="%.1f" y="%.1f" font-size="18" '
                        'fill="#334155">%s</text>'
                        % (sx, sy, _escape_text(raw_text))
                    )
                    continue

                path = ezdxf.path.make_path(entity)
                points = list(path.flattening(max(0.5, viewport_radius / 100)))
                if len(points) < 2:
                    continue
                svg_points = []
                for point in points:
                    sx, sy = _world_to_svg(
                        float(point.x),
                        float(point.y),
                        min_x,
                        min_y,
                        scale,
                        svg_size,
                    )
                    svg_points.append(f"{sx:.1f},{sy:.1f}")
                svg_lines.append(
                    '<polyline points="%s" fill="none" '
                    'stroke="#334155" stroke-width="1.5"/>'
                    % " ".join(svg_points)
                )
            except Exception:
                continue

        marker_x, marker_y = _world_to_svg(
            cx, cy, min_x, min_y, scale, svg_size
        )
        svg_lines.append(
            '<circle cx="%.1f" cy="%.1f" r="18" fill="none" '
            'stroke="#dc2626" stroke-width="6"/>' % (marker_x, marker_y)
        )
        svg_lines.append(
            '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '
            'stroke="#dc2626" stroke-width="4"/>'
            % (marker_x - 26, marker_y, marker_x + 26, marker_y)
        )
        svg_lines.append(
            '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '
            'stroke="#dc2626" stroke-width="4"/>'
            % (marker_x, marker_y - 26, marker_x, marker_y + 26)
        )
        svg_lines.append(
            '<text x="20" y="36" font-size="24" fill="#111827">#%s %s</text>'
            % (_escape_text(issue.issue_id), _escape_text(issue.issue_type))
        )
        svg_lines.append("</svg>")

        evidence_name = f"evidence_{issue.issue_id}.svg"
        evidence_path = output_dir / evidence_name
        raw = "\n".join(svg_lines).encode("utf-8")
        evidence_path.write_bytes(raw)
        results.append(
            {
                "issue_id": issue.issue_id,
                "artifact_id": evidence_name,
                "digest": _sha256_bytes(raw),
            }
        )

    return results
