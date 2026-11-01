"""源曲线元数据的刚体变换（原位 / 排板 DXF 如实复写用）。

读图阶段把源图里的 CIRCLE / ARC / 带 bulge 的 LWPOLYLINE 记成谱
（见 ``dxf_reader.attach_source_curves``）。排板时零件被旋转 + 平移，
后处理还会再平移；这里用"源外轮廓 + 施加的旋转角 + 当前外包框"
反推出落位变换，把谱映射到最终坐标。

约定：排板与后处理对零件只做刚体变换（旋转 90° 的倍数或任意角、
平移），不做缩放与镜像。bulge 在这种变换下不变（镜像会变号，
当前引擎不使用镜像）。
"""

from __future__ import annotations

import math
from typing import Any

from shapely import affinity


def _rotate_point(
    x: float,
    y: float,
    origin: tuple[float, float],
    degrees: float,
) -> tuple[float, float]:
    if not degrees:
        return (x, y)
    ox, oy = origin
    rad = math.radians(degrees)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    dx, dy = x - ox, y - oy
    return (
        ox + dx * cos_a - dy * sin_a,
        oy + dx * sin_a + dy * cos_a,
    )


def transform_spec(
    spec: dict[str, Any],
    *,
    degrees: float,
    origin: tuple[float, float],
    dx: float,
    dy: float,
) -> dict[str, Any]:
    """把一份源曲线谱映射到落位坐标（旋转 → 平移）。"""
    kind = spec.get("type")
    if kind == "circle":
        cx, cy = spec["center"]
        x, y = _rotate_point(float(cx), float(cy), origin, degrees)
        return {
            **spec,
            "center": (x + dx, y + dy),
            "radius": float(spec["radius"]),
        }
    if kind == "arc":
        cx, cy = spec["center"]
        x, y = _rotate_point(float(cx), float(cy), origin, degrees)
        return {
            **spec,
            "center": (x + dx, y + dy),
            "radius": float(spec["radius"]),
            "start_angle": (float(spec["start_angle"]) + degrees) % 360.0,
            "end_angle": (float(spec["end_angle"]) + degrees) % 360.0,
        }
    if kind == "polyline":
        vertices = []
        for raw in spec.get("vertices") or []:
            px, py, bulge = float(raw[0]), float(raw[1]), float(raw[2])
            x, y = _rotate_point(px, py, origin, degrees)
            # bulge 是弧段张角/4，旋转与平移不改变它。
            vertices.append([x + dx, y + dy, bulge])
        return {**spec, "vertices": vertices}
    if kind == "ellipse":
        cx, cy = spec["center"]
        x, y = _rotate_point(float(cx), float(cy), origin, degrees)
        mx, my = spec["major_axis"]
        ox, oy = origin
        vx, vy = _rotate_point(
            ox + float(mx), oy + float(my), origin, degrees
        )
        return {
            **spec,
            "center": (x + dx, y + dy),
            # 长短轴向量只旋转、不平移
            "major_axis": (vx - ox, vy - oy),
        }
    if kind == "spline":
        points = []
        for raw in spec.get("control_points") or []:
            x, y = _rotate_point(
                float(raw[0]), float(raw[1]), origin, degrees
            )
            points.append(
                [x + dx, y + dy, float(raw[2]) if len(raw) > 2 else 0.0]
            )
        return {**spec, "control_points": points}
    return dict(spec)


def placement_transform(part) -> tuple[float, tuple[float, float], float, float] | None:
    """由"源外轮廓 + 当前外轮廓"反推落位变换。

    后处理只做平移，所以旋转角沿用排板时的记录，平移量取当前外包框
    与"源外轮廓旋转后"外包框之差。
    """
    source = getattr(part, "source_outer_polygon", None)
    current = getattr(part, "outer_polygon", None)
    if source is None or current is None or source.is_empty:
        return None
    degrees = float(getattr(part, "placement_rot", 0.0) or 0.0)
    origin = (source.centroid.x, source.centroid.y)
    rotated = affinity.rotate(source, degrees, origin=origin) if degrees else source
    dx = current.bounds[0] - rotated.bounds[0]
    dy = current.bounds[1] - rotated.bounds[1]
    return degrees, origin, dx, dy


def placed_specs(part) -> tuple[dict | None, list[dict | None]]:
    """返回落位坐标下的 (外轮廓谱, 孔洞谱列表)。

    没有源曲线元数据时返回 (None, [])，调用端保持原有折线输出。
    """
    outer_polyline = getattr(part, "outer_polyline", None)
    outer_curve = getattr(part, "outer_curve", None)
    shape = getattr(part, "outer_shape", None)
    arcs = list(getattr(part, "outer_arcs", None) or [])
    hole_shapes = list(getattr(part, "hole_shapes", None) or [])
    if not (
        outer_polyline or outer_curve or shape or arcs or any(hole_shapes)
    ):
        return None, []
    transform = placement_transform(part)
    if transform is None:
        return None, []
    degrees, origin, dx, dy = transform

    def apply(spec: dict | None) -> dict | None:
        if not spec:
            return None
        return transform_spec(
            spec, degrees=degrees, origin=origin, dx=dx, dy=dy
        )

    return (
        {
            "shape": apply(shape) if shape else None,
            "arcs": [item for item in (apply(arc) for arc in arcs) if item],
            "polyline": apply(outer_polyline) if outer_polyline else None,
            "curve": apply(outer_curve) if outer_curve else None,
        },
        [apply(spec) for spec in hole_shapes],
    )
