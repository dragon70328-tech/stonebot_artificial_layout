"""机床就绪检查：DXF 直接下料前必须排除的图纸缺陷。

检查项（不依赖第三方库、纯 ezdxf）：
- 单位未声明（$INSUNITS 缺失/为 0）：机床可能按英寸解读，整图放大 25.4 倍；
- 零长度实体：刀具会走空刀或直接报错；
- 完全重复实体：同一轮廓画两遍 = 重复切割；
- 面板实体落在 0 层 / DEFPOINTS：无法按图层开关核对。
"""

from __future__ import annotations

import math
from typing import Any


_CUT_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "SPLINE", "ELLIPSE"}
_SIGNATURE_TYPES = {"LINE", "LWPOLYLINE", "ARC", "CIRCLE"}


def _round_point(point) -> tuple[float, float]:
    return (round(float(point[0]), 3), round(float(point[1]), 3))


def _entity_signature(entity) -> tuple | None:
    dtype = entity.dxftype()
    if dtype == "LINE":
        start = _round_point((entity.dxf.start.x, entity.dxf.start.y))
        end = _round_point((entity.dxf.end.x, entity.dxf.end.y))
        return ("LINE",) + tuple(sorted([start, end]))
    if dtype == "LWPOLYLINE":
        points = tuple(
            (
                round(float(x), 3),
                round(float(y), 3),
                round(float(bulge), 4),
            )
            for x, y, bulge in entity.get_points("xyb")
        )
        return ("LWPOLYLINE", points, bool(entity.closed))
    if dtype == "CIRCLE":
        center = _round_point((entity.dxf.center.x, entity.dxf.center.y))
        return ("CIRCLE", center, round(float(entity.dxf.radius), 3))
    if dtype == "ARC":
        center = _round_point((entity.dxf.center.x, entity.dxf.center.y))
        return (
            "ARC",
            center,
            round(float(entity.dxf.radius), 3),
            round(float(entity.dxf.start_angle), 3),
            round(float(entity.dxf.end_angle), 3),
        )
    return None


def _anchor(entity) -> tuple[float, float]:
    dtype = entity.dxftype()
    try:
        if dtype == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            return ((start.x + end.x) / 2, (start.y + end.y) / 2)
        if dtype in {"CIRCLE", "ARC"}:
            center = entity.dxf.center
            return (float(center.x), float(center.y))
        if dtype in {"LWPOLYLINE", "POLYLINE"}:
            points = list(entity.get_points("xy"))
            if points:
                return (float(points[0][0]), float(points[0][1]))
        insert = entity.dxf.insert
        return (float(insert.x), float(insert.y))
    except Exception:  # noqa: BLE001
        return (0.0, 0.0)


def _zero_length(entity) -> bool:
    dtype = entity.dxftype()
    try:
        if dtype == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            return math.hypot(end.x - start.x, end.y - start.y) <= 1e-3
        if dtype == "LWPOLYLINE":
            points = list(entity.get_points("xy"))
            if len(points) < 2:
                return True
            total = 0.0
            for index in range(1, len(points)):
                total += math.hypot(
                    points[index][0] - points[index - 1][0],
                    points[index][1] - points[index - 1][1],
                )
            return total <= 1e-3
        if dtype in {"CIRCLE", "ARC"}:
            return float(entity.dxf.radius) <= 1e-6
    except Exception:  # noqa: BLE001
        return False
    return False


def findings(
    doc,
    *,
    panel_layer: str | None = None,
    regions: list | None = None,
) -> list[dict[str, Any]]:
    """返回机床就绪问题清单（空 = 通过）。"""
    results: list[dict[str, Any]] = []
    if doc is None:
        return results

    insunits = doc.header.get("$INSUNITS")
    try:
        units_value = int(insunits or 0)
    except (TypeError, ValueError):
        units_value = 0
    if units_value == 0:
        results.append(
            {
                "type": "units_missing",
                "severity": "warning",
                "layer": "",
                "coordinates": (0.0, 0.0),
                "message": "图纸未声明单位（$INSUNITS=0），机床可能按英寸读取",
                "suggestion": "在 CAD 中设置图形单位（人造石图纸应为毫米）后重新导出",
            }
        )

    seen: dict[tuple, str] = {}
    duplicates: list[dict[str, Any]] = []
    zero_length: list[dict[str, Any]] = []
    bounds = []
    for region in regions or []:
        try:
            if region is not None and not region.is_empty:
                bounds.append(region.bounds)
        except Exception:  # noqa: BLE001
            continue

    def inside_region(point: tuple[float, float]) -> bool:
        if not bounds:
            return True
        x, y = point
        return any(
            box[0] <= x <= box[2] and box[1] <= y <= box[3]
            for box in bounds
        )

    for entity in doc.modelspace():
        dtype = entity.dxftype()
        layer = str(getattr(entity.dxf, "layer", "0") or "0")
        if layer.upper() in {"DEFPOINTS"}:
            continue
        if dtype not in _CUT_TYPES:
            continue
        anchor = _anchor(entity)
        # 有零件区域时只检查零件所在范围：图框、详图里的重复线
        # 不会进入切割文件，报出来只会造成噪音。
        if not inside_region(anchor):
            continue
        if _zero_length(entity):
            zero_length.append(
                {
                    "layer": layer,
                    "coordinates": anchor,
                    "entity_type": dtype,
                    "handle": getattr(entity.dxf, "handle", None),
                }
            )
        if dtype in _SIGNATURE_TYPES:
            signature = _entity_signature(entity)
            if signature is None:
                continue
            if signature in seen:
                duplicates.append(
                    {
                        "layer": layer,
                        "coordinates": anchor,
                        "entity_type": dtype,
                        "handle": getattr(entity.dxf, "handle", None),
                        "duplicate_of": seen[signature],
                    }
                )
            else:
                seen[signature] = str(
                    getattr(entity.dxf, "handle", "") or ""
                )

    if zero_length:
        sample = zero_length[0]
        results.append(
            {
                "type": "zero_length_entity",
                "severity": "warning",
                "layer": sample["layer"],
                "coordinates": sample["coordinates"],
                "message": (
                    f"检测到 {len(zero_length)} 个零长度实体"
                    f"（如 {sample['entity_type']} handle "
                    f"{sample['handle']}），机床会走空刀或报错"
                ),
                "suggestion": "删除这些实体后重新导出 DXF",
                "entity_type": sample["entity_type"],
                "metadata": {
                    "count": len(zero_length),
                    "handles": [
                        item["handle"] for item in zero_length[:50]
                    ],
                },
            }
        )
    if duplicates:
        sample = duplicates[0]
        results.append(
            {
                "type": "duplicate_entity",
                "severity": "warning",
                "layer": sample["layer"],
                "coordinates": sample["coordinates"],
                "message": (
                    f"检测到 {len(duplicates)} 个完全重复的实体"
                    f"（如 {sample['entity_type']} handle "
                    f"{sample['handle']} 与 {sample['duplicate_of']} 相同），"
                    "机床会切两刀"
                ),
                "suggestion": "删除重复实体后重新导出 DXF",
                "entity_type": sample["entity_type"],
                "metadata": {
                    "count": len(duplicates),
                    "handles": [
                        item["handle"] for item in duplicates[:50]
                    ],
                },
            }
        )
    return results
