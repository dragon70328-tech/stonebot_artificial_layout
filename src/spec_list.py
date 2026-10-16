"""规格板尺寸清单：读图分析后的审图参考产物。

按 材料 × 区域 分组列出规格板尺寸（长×宽、数量、单件净面积、面积小计、
孔洞数、编号），并附上图纸文字中提取的 表面处理 / 磨边 / 纹理走向 要求；
**图纸未注明的要求一律留空**，不做猜测（用户口径："图纸中没有要求，
则空出该项"）。

输出两份文件：

- ``{stem}_规格清单.json``：结构化数据（分组 + 逐件明细 + 提取证据），
  供流程与后续校验使用；
- ``{stem}_规格清单.csv``：utf-8-sig 编码，Excel/WPS 直接打开，供人工审图。

尺寸口径：外轮廓外包络跨度，长边在前（长 >= 宽），0.1mm 精度；
异形件（外轮廓面积 / 外包络面积 < 0.98）在备注列标注"尺寸为外包络"。
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import ezdxf

# ---------------------------------------------------------------------------
# 图纸文字要求提取（表面处理 / 磨边 / 纹理走向）
# ---------------------------------------------------------------------------

# 只扫描"说明类"短文本（长度上限），降低把零件编号/尺寸标注误当要求的概率。
_ANNOTATION_MAX_TEXT_LEN = 60
# 每个属性最多保留的不同取值条数（同一说明在图上重复出现时去重）。
_ANNOTATION_MAX_VALUES = 3
# 关键词上下文窗口（字符数）：保留"不磨边""无需抛光"这类否定语境。
_ANNOTATION_CONTEXT = 8

_SURFACE_KEYWORDS = (
    "抛光", "光面", "哑光", "亚光", "荔枝面", "火烧面", "仿古", "拉丝",
    "喷砂", "酸洗", "磨光", "自然面", "刀砍面", "皮革面",
)
_EDGE_KEYWORDS = (
    "磨边", "边型", "圆边", "斜边", "倒角", "鸭嘴边", "法国边", "直边",
    "加厚边", "吊边",
)
_GRAIN_KEYWORDS = ("纹理", "纹路", "顺纹", "对纹", "追纹")

_ANNOTATION_ATTRS = {
    "surface_finish": _SURFACE_KEYWORDS,
    "edge_finish": _EDGE_KEYWORDS,
    "grain_direction": _GRAIN_KEYWORDS,
}


def _annotation_value(text: str, keyword: str) -> str:
    """截取关键词上下文作为取值（保留否定词语境）。"""
    if len(text) <= 24:
        return text
    index = text.find(keyword)
    start = max(0, index - _ANNOTATION_CONTEXT)
    end = min(len(text), index + len(keyword) + _ANNOTATION_CONTEXT)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def extract_annotations(doc) -> dict[str, Any]:
    """从图纸 TEXT/MTEXT 中提取 表面处理 / 磨边 / 纹理走向 要求。

    返回 ``{"surface_finish": str, "edge_finish": str, "grain_direction": str,
    "evidence": {attr: [原文...]}}``；未提到的属性为空字符串。
    """
    values: dict[str, list[str]] = {attr: [] for attr in _ANNOTATION_ATTRS}
    evidence: dict[str, list[str]] = {attr: [] for attr in _ANNOTATION_ATTRS}
    for entity in doc.modelspace():
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            text = (
                entity.dxf.text
                if entity.dxftype() == "TEXT"
                else entity.plain_text()
            )
        except Exception:
            continue
        text = (text or "").strip()
        if not text or len(text) > _ANNOTATION_MAX_TEXT_LEN:
            continue
        for attr, keywords in _ANNOTATION_ATTRS.items():
            for keyword in keywords:
                if keyword not in text:
                    continue
                value = _annotation_value(text, keyword)
                if value not in values[attr]:
                    values[attr].append(value)
                    evidence[attr].append(text)
                break  # 一条文本对一个属性只记一次

    result: dict[str, Any] = {
        attr: "；".join(items[:_ANNOTATION_MAX_VALUES])
        for attr, items in values.items()
    }
    result["evidence"] = {
        attr: items[:_ANNOTATION_MAX_VALUES]
        for attr, items in evidence.items()
        if items
    }
    return result


def extract_annotations_from_file(dxf_path: str | Path) -> dict[str, Any]:
    """读 DXF 文件并提取图纸文字要求（主流程没有 doc 句柄时用）。"""
    return extract_annotations(ezdxf.readfile(str(dxf_path)))


# ---------------------------------------------------------------------------
# 清单构建
# ---------------------------------------------------------------------------

# 外轮廓面积 / 外包络面积 低于该比例视为异形件（尺寸只是外包络参考）。
_IRREGULAR_FILL_RATIO = 0.98


def _natural_key(value: str):
    return [
        int(chunk) if chunk.isdigit() else chunk
        for chunk in re.split(r"(\d+)", value)
    ]


def _part_row(part) -> dict[str, Any]:
    outer = part.outer_polygon
    minx, miny, maxx, maxy = outer.bounds
    span_x = maxx - minx
    span_y = maxy - miny
    bbox_area = span_x * span_y
    irregular = bool(
        bbox_area > 0 and outer.area / bbox_area < _IRREGULAR_FILL_RATIO
    )
    return {
        "number": part.number,
        "material": part.material_group or "",
        "zone": part.material_column or "",
        "length_mm": round(max(span_x, span_y), 1),
        "width_mm": round(min(span_x, span_y), 1),
        "area_m2": round(part.polygon.area / 1e6, 4),
        "holes": len(part.holes or []),
        "irregular": irregular,
    }


def build_spec_list(
    parts,
    *,
    annotations: dict[str, Any] | None = None,
    source_name: str = "",
    unit: str = "mm",
) -> dict[str, Any]:
    """把 Part 列表汇总成 材料 × 区域 × 尺寸 分组清单。"""
    rows = [_part_row(part) for part in parts]

    grouped: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = (row["material"], row["zone"], row["length_mm"], row["width_mm"])
        group = grouped.setdefault(
            key,
            {
                "material": row["material"],
                "zone": row["zone"],
                "length_mm": row["length_mm"],
                "width_mm": row["width_mm"],
                "quantity": 0,
                "numbers": [],
                "area_sum_m2": 0.0,
                "holes_total": 0,
                "irregular": False,
            },
        )
        group["quantity"] += 1
        group["numbers"].append(row["number"])
        group["area_sum_m2"] += row["area_m2"]
        group["holes_total"] += row["holes"]
        group["irregular"] = group["irregular"] or row["irregular"]

    groups = sorted(
        grouped.values(),
        key=lambda g: (
            g["material"],
            _natural_key(g["zone"]),
            -g["area_sum_m2"],
        ),
    )
    for group in groups:
        group["numbers"].sort(key=_natural_key)
        group["unit_net_area_m2"] = round(
            group["area_sum_m2"] / group["quantity"], 4
        )
        group["area_sum_m2"] = round(group["area_sum_m2"], 4)

    per_material: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"parts": 0, "area_m2": 0.0}
    )
    for row in rows:
        entry = per_material[row["material"]]
        entry["parts"] += 1
        entry["area_m2"] += row["area_m2"]
    material_totals = {
        material: {"parts": v["parts"], "area_m2": round(v["area_m2"], 4)}
        for material, v in sorted(per_material.items())
    }

    annotation_values = {
        "surface_finish": "",
        "edge_finish": "",
        "grain_direction": "",
    }
    evidence: dict[str, Any] = {}
    if annotations:
        annotation_values.update(
            {
                attr: annotations.get(attr, "") or ""
                for attr in annotation_values
            }
        )
        evidence = annotations.get("evidence", {})

    return {
        "source": source_name,
        "unit": unit,
        "size_basis": "外轮廓外包络（长>=宽），0.1mm 精度；异形件尺寸为外包络参考",
        "annotations": annotation_values,
        "annotations_evidence": evidence,
        "groups": groups,
        "details": rows,
        "totals": {
            "parts": len(rows),
            "groups": len(groups),
            "total_net_area_m2": round(sum(r["area_m2"] for r in rows), 4),
            "per_material": material_totals,
        },
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

_CSV_HEADER = [
    "材料", "区域", "长(mm)", "宽(mm)", "数量",
    "单件净面积(m²)", "面积小计(m²)", "孔洞(个)",
    "编号", "表面处理", "磨边", "纹理走向", "备注",
]
_CSV_MAX_NUMBERS = 20


def _fmt_mm(value: float) -> str:
    rounded = round(value, 1)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.1f}"


def _fmt_numbers(numbers: list[str]) -> str:
    if len(numbers) <= _CSV_MAX_NUMBERS:
        return "、".join(numbers)
    return "、".join(numbers[:_CSV_MAX_NUMBERS]) + f" 等{len(numbers)}个"


def write_spec_list_files(
    spec: dict[str, Any], out_dir: str | Path, stem: str
) -> tuple[Path, Path]:
    """写出 规格清单 JSON + CSV，返回两个路径。"""
    out_dir = Path(out_dir)
    json_path = out_dir / f"{stem}_规格清单.json"
    csv_path = out_dir / f"{stem}_规格清单.csv"

    json_path.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    annotations = spec.get("annotations", {})
    surface = annotations.get("surface_finish", "")
    edge = annotations.get("edge_finish", "")
    grain = annotations.get("grain_direction", "")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_CSV_HEADER)
        for group in spec["groups"]:
            writer.writerow(
                [
                    group["material"],
                    group["zone"],
                    _fmt_mm(group["length_mm"]),
                    _fmt_mm(group["width_mm"]),
                    group["quantity"],
                    f"{group['unit_net_area_m2']:.4f}".rstrip("0").rstrip("."),
                    f"{group['area_sum_m2']:.4f}".rstrip("0").rstrip("."),
                    group["holes_total"],
                    _fmt_numbers(group["numbers"]),
                    surface,
                    edge,
                    grain,
                    "异形件，尺寸为外包络" if group["irregular"] else "",
                ]
            )
        totals = spec["totals"]
        writer.writerow(
            [
                "合计", "", "", "", totals["parts"], "",
                f"{totals['total_net_area_m2']:.4f}".rstrip("0").rstrip("."),
                "", "", "", "", "", "",
            ]
        )
    return json_path, csv_path
