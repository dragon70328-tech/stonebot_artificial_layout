"""从图纸文字里识别材料厚度；图纸未注明时按常用 20mm 记录。

只在明确出现"厚 / 厚度 / T="这类语境时识别，避免把 1200mm 这类
长度尺寸误判成板厚。识别结果写到零件上：
``thickness_mm`` + ``thickness_source``（drawing / default）。
"""

from __future__ import annotations

import re
from typing import Any


DEFAULT_THICKNESS_MM = 20.0

_THICKNESS_PATTERNS = (
    re.compile(r"厚\s*度?\s*[:：=]?\s*(\d{1,3}(?:\.\d+)?)"),
    re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:mm|MM|毫米)\s*厚"),
    re.compile(r"[Tt]\s*[:：=]\s*(\d{1,3}(?:\.\d+)?)"),
    re.compile(r"厚\s*(\d{1,3}(?:\.\d+)?)\s*(?:mm|MM|毫米)"),
)


def _texts(doc) -> list[str]:
    values: list[str] = []
    if doc is None:
        return values
    for entity in doc.modelspace():
        try:
            if entity.dxftype() == "TEXT":
                values.append(str(entity.dxf.text or ""))
            elif entity.dxftype() == "MTEXT":
                values.append(str(entity.text or ""))
        except Exception:  # noqa: BLE001 - 异常文字忽略
            continue
    return values


def extract_thickness_values(texts: list[str]) -> list[float]:
    """从文字里收集所有明确的厚度值（去重、升序）。"""
    found: set[float] = set()
    for text in texts:
        for pattern in _THICKNESS_PATTERNS:
            for match in pattern.finditer(text):
                try:
                    value = float(match.group(1))
                except (TypeError, ValueError):
                    continue
                if 3.0 <= value <= 60.0:
                    found.add(value)
    return sorted(found)


def attach_thickness(
    parts_data: list[dict[str, Any]],
    doc,
) -> dict[str, Any]:
    """给每个零件挂上厚度；返回本次识别摘要。

    规则（保守，宁可回退默认值也不猜错）：
    1. 全图只出现一个厚度值 → 全部零件用它；
    2. 全图没有厚度说明 → 常用 20mm（source=default）；
    3. 出现多个厚度值 → 不猜，全部按默认值并记录待确认。
    """
    values = extract_thickness_values(_texts(doc))
    if len(values) == 1:
        thickness = values[0]
        source = "drawing"
        note = None
    elif not values:
        thickness = DEFAULT_THICKNESS_MM
        source = "default"
        note = "图纸未注明厚度，按常用 20mm 记录"
    else:
        thickness = DEFAULT_THICKNESS_MM
        source = "default"
        note = (
            "图纸出现多个厚度值 "
            + "、".join(f"{value:g}" for value in values)
            + "，无法归属到具体零件，暂按 20mm 记录"
        )
    for part in parts_data:
        part["thickness_mm"] = float(thickness)
        part["thickness_source"] = source
        if note:
            part["thickness_note"] = note
    return {
        "thickness_mm": float(thickness),
        "source": source,
        "candidates": values,
        "note": note,
    }
