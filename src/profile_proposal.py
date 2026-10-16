"""图纸画像提议（propose）与登记（register）——画像库积累流水线。

把手写 ``drawing_profiles/*.json`` 变成三步：

1. :func:`propose_profile`：只依赖 DXF 结构统计（实体 / 图层 / 线型 / 文本
   形态）自动产出候选画像 + 证据 + 待确认问题，并用生产读图链路
   ``read_dxf_with_profile`` 实读一遍，把读出的件数作为"待确认
   expected_counts"；
2. 人工核对（对照 CAD 确认件数、编号格式、排除项），必要时编辑草稿；
3. :func:`register_profile`：登记到 ``drawing_profiles/`` 与
   ``drawing_profiles/validated_cases/``，写入图纸指纹与源文件 sha256。

登记前会用 ``read_dxf_with_profile`` 复算件数并与草稿的
``expected_counts`` 对比：不一致默认拒绝登记（``force=True`` 时登记并把
警告写进 notes）。提议阶段只读图，不执行排板。
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf

from src.cli_audit import file_sha256
from src.cli_paths import PROJECT_ROOT
from src.drawing_profile import (
    DrawingProfile,
    analyze_drawing,
    audit_drawing,
    read_dxf_with_profile,
)
from src.dxf_reader import _linetype_name

_CONTINUOUS = "CONTINUOUS"

# 参与"规格件图层"统计的实体类型（与 dxf_reader 的封闭图形候选范围对齐）。
_SHAPE_TYPES = ("LWPOLYLINE", "POLYLINE", "CIRCLE", "SPLINE", "ELLIPSE")

# 编号候选文本：短、无空白、至少一个数字、字符集受控（对齐常见编号形态：
# 101-A / 1001-01 / P-123 / 01B-7）。含中文（材料：ST-6）或空格
# （L-10 LEVEL）的文本自然被排除。
_NUMBER_CHARSET = re.compile(r"^[0-9A-Za-z\-./#]+$")
_MIN_NUMBER_LEN = 2
_MAX_NUMBER_LEN = 15
# 一个图层至少要有这么多候选编号、且不低于最多图层的该比例，才被提议为
# number_layer（避免把零星标注层当成编号层）。
_MIN_NUMBER_LAYER_COUNT = 5
_NUMBER_LAYER_RATIO = 0.25
# 一个图层至少贡献这么多封闭图形、且不低于最多图层的该比例，才进 panel_layers。
_MIN_PANEL_LAYER_COUNT = 5
_PANEL_LAYER_RATIO = 0.25
# 面积分布断层的最小倍数：相邻两面积相差超过该倍数才建议小面积阈值。
_AREA_GAP_RATIO = 8.0


@dataclass
class ProfileProposal:
    """一次画像提议的完整产出。"""

    name: str
    draft: dict[str, Any]
    evidence: dict[str, Any]
    questions: list[str]
    validation: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 结构统计
# ---------------------------------------------------------------------------


def _polyline_points(entity) -> list[tuple[float, float]]:
    if entity.dxftype() == "LWPOLYLINE":
        return [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
    return [
        (float(v.dxf.location.x), float(v.dxf.location.y))
        for v in entity.vertices
    ]


def _shoelace(points: list[tuple[float, float]]) -> float:
    area = 0.0
    for i, (x0, y0) in enumerate(points):
        x1, y1 = points[(i + 1) % len(points)]
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0


def _closed_shape_stats(doc) -> dict[str, Any]:
    """按图层统计封闭图形（提议 panel_layer / 层级 / 容差 / 面积阈值用）。

    只读原始几何做统计；真正识别仍由 read_dxf_with_profile 完成。
    """
    layer_closed: Counter[str] = Counter()
    layer_circles: Counter[str] = Counter()
    layer_areas: dict[str, list[float]] = {}
    unclosed_gaps: dict[str, list[float]] = {}
    linetype_counts: Counter[str] = Counter()

    for entity in doc.modelspace():
        dxftype = entity.dxftype()
        if dxftype not in _SHAPE_TYPES:
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        linetype = _linetype_name(entity, doc)
        linetype_counts[linetype] += 1
        if linetype != _CONTINUOUS:
            continue

        if dxftype in ("LWPOLYLINE", "POLYLINE"):
            points = _polyline_points(entity)
            if len(points) < 3:
                continue
            flag_closed = bool(
                getattr(entity, "closed", getattr(entity, "is_closed", False))
            )
            # 首点重复（gap=0）但未置 closed 标志的多段线按闭合统计，
            # 否则面积/图层统计会整体漏掉这类最常见的画法。
            if flag_closed:
                layer_closed[layer] += 1
                layer_areas.setdefault(layer, []).append(_shoelace(points))
                continue
            gap = math.hypot(
                points[0][0] - points[-1][0],
                points[0][1] - points[-1][1],
            )
            if gap <= 1e-6:
                layer_closed[layer] += 1
                layer_areas.setdefault(layer, []).append(_shoelace(points))
            else:
                unclosed_gaps.setdefault(layer, []).append(gap)
        elif dxftype == "CIRCLE":
            layer_circles[layer] += 1
        else:  # SPLINE / ELLIPSE：近似闭合即计
            closed = bool(getattr(entity, "closed", True))
            if closed:
                layer_closed[layer] += 1

    return {
        "layer_closed": dict(layer_closed),
        "layer_circles": dict(layer_circles),
        "layer_areas": layer_areas,
        "unclosed_gaps": unclosed_gaps,
        "linetype_counts": dict(linetype_counts),
    }


def _is_number_candidate(text: str) -> bool:
    if not (_MIN_NUMBER_LEN <= len(text) <= _MAX_NUMBER_LEN):
        return False
    if not _NUMBER_CHARSET.match(text):
        return False
    return any(ch.isdigit() for ch in text)


def _text_stats(doc) -> dict[str, Any]:
    """按图层统计 TEXT/MTEXT：总数、编号候选、字高。"""
    per_layer: dict[str, dict[str, Any]] = {}
    for entity in doc.modelspace():
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        try:
            text = (
                entity.dxf.text
                if entity.dxftype() == "TEXT"
                else entity.plain_text()
            )
        except Exception:
            continue
        text = (text or "").strip()
        if not text:
            continue
        height = float(
            getattr(entity.dxf, "height", 0.0)
            or getattr(entity.dxf, "char_height", 0.0)
            or 0.0
        )
        entry = per_layer.setdefault(
            layer, {"total": 0, "candidates": [], "heights": []}
        )
        entry["total"] += 1
        if _is_number_candidate(text):
            entry["candidates"].append(text)
            if height > 0:
                entry["heights"].append(height)
    return per_layer


# ---------------------------------------------------------------------------
# 编号正则归纳
# ---------------------------------------------------------------------------


def _class_runs(text: str) -> list[tuple[str, str]]:
    """把文本切成字符类游程：9=数字 A=大写 a=小写 其他=字面量。"""
    runs: list[list[Any]] = []
    for ch in text:
        if ch.isdigit():
            cls = "9"
        elif "A" <= ch <= "Z":
            cls = "A"
        elif "a" <= ch <= "z":
            cls = "a"
        else:
            cls = ch
        if runs and runs[-1][0] == cls and (cls in "9Aa" or runs[-1][1] == ch):
            runs[-1][1] += ch
        else:
            runs.append([cls, ch])
    return [(cls, value) for cls, value in runs]


def _run_atom(cls: str, values: list[str], named: str | None) -> str:
    lengths = sorted({len(v) for v in values})
    if lengths[0] == lengths[-1]:
        extent = f"{{{lengths[0]}}}"
    else:
        extent = f"{{{lengths[0]},{lengths[-1]}}}"
    if cls == "9":
        atom = rf"\d{extent}"
    elif cls == "A":
        atom = f"[A-Z]{extent}"
    elif cls == "a":
        atom = f"[a-z]{extent}"
    else:
        unique = sorted({ch for v in values for ch in v})
        if len(unique) == 1:
            return re.escape(unique[0])
        return "[" + re.escape("".join(unique)) + "]"
    if named:
        return f"(?P<{named}>{atom})"
    return atom


def induce_label_pattern(texts: list[str]) -> dict[str, Any]:
    """从编号文本归纳最紧正则。

    命名约定与 ``_normalize_label`` 对齐：同时含数字段与大写字母段时，
    第一个数字段命名 ``material``、第一个大写段命名 ``shape``。
    """
    if not texts:
        return {
            "pattern": r"^\d+$",
            "coverage": 0.0,
            "covered": 0,
            "total": 0,
            "uncovered_samples": [],
            "signatures": {},
        }

    groups: dict[tuple[str, ...], list[str]] = {}
    for text in texts:
        signature = tuple(cls for cls, _ in _class_runs(text))
        groups.setdefault(signature, []).append(text)

    signature, members = max(groups.items(), key=lambda item: len(item[1]))
    runs = _class_runs(members[0])
    positions: list[list[str]] = [[] for _ in runs]
    for text in members:
        for i, (_cls, value) in enumerate(_class_runs(text)):
            positions[i].append(value)

    has_digit = "9" in signature
    has_upper = "A" in signature
    named_used = {"material": False, "shape": False}
    parts: list[str] = []
    for i, (cls, _value) in enumerate(runs):
        named = None
        if cls == "9" and has_digit and has_upper and not named_used["material"]:
            named = "material"
            named_used["material"] = True
        elif cls == "A" and has_digit and has_upper and not named_used["shape"]:
            named = "shape"
            named_used["shape"] = True
        parts.append(_run_atom(cls, positions[i], named))

    pattern = "^" + "".join(parts) + "$"
    compiled = re.compile(pattern)
    covered = [text for text in texts if compiled.match(text)]
    uncovered = [text for text in texts if not compiled.match(text)]
    return {
        "pattern": pattern,
        "coverage": len(covered) / len(texts),
        "covered": len(covered),
        "total": len(texts),
        "uncovered_samples": sorted(set(uncovered))[:8],
        "signatures": {
            "/".join(sig): len(members_of)
            for sig, members_of in sorted(
                groups.items(), key=lambda item: -len(item[1])
            )
        },
    }


# ---------------------------------------------------------------------------
# 容差 / 面积阈值 / 图层提议
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], q: float) -> float:
    index = min(
        len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1)))
    )
    return sorted_values[index]


def _propose_closed_tolerance(gaps: list[float]) -> tuple[float, dict[str, Any]]:
    if not gaps:
        return 0.01, {}
    ordered = sorted(gaps)
    p50 = _percentile(ordered, 0.5)
    p95 = _percentile(ordered, 0.95)
    evidence = {
        "count": len(gaps),
        "min": round(ordered[0], 4),
        "p50": round(p50, 4),
        "p95": round(p95, 4),
        "max": round(ordered[-1], 4),
    }
    if p95 <= 5.0:
        suggestion = max(0.05, math.ceil(p95 * 10) / 10)
        evidence["recovered_at_suggestion"] = sum(
            1 for g in gaps if g <= suggestion
        )
        return suggestion, evidence
    # 大缝隙居多：多半不是"视觉闭合"，保持严容差，交人工判断。
    evidence["warning"] = "未闭合多段线缝隙普遍偏大，多半是辅助线而非视觉闭合件"
    return 0.01, evidence


def _round_significant(value: float, digits: int = 2) -> float:
    if value <= 0:
        return 0.0
    exponent = math.floor(math.log10(value))
    factor = 10 ** (exponent - digits + 1)
    return round(value / factor) * factor


def _propose_small_area_threshold(
    areas: list[float],
) -> tuple[float, dict[str, Any]]:
    positive = sorted(a for a in areas if a > 0)
    if len(positive) < 10:
        return 0.0, {"reason": "封闭图形数量不足，不提议面积阈值"}
    median = positive[len(positive) // 2]
    best_ratio = 1.0
    best_index: int | None = None
    for i in range(1, len(positive)):
        if positive[i - 1] > median:
            break
        ratio = positive[i] / positive[i - 1] if positive[i - 1] else 1.0
        if ratio > best_ratio:
            best_ratio, best_index = ratio, i
    evidence = {
        "count": len(positive),
        "min": round(positive[0], 1),
        "p10": round(_percentile(positive, 0.1), 1),
        "median": round(median, 1),
    }
    if best_index is not None and best_ratio >= _AREA_GAP_RATIO:
        below = positive[best_index - 1]
        above = positive[best_index]
        threshold = _round_significant(math.sqrt(below * above))
        evidence.update(
            {
                "gap_below": round(below, 1),
                "gap_above": round(above, 1),
                "gap_ratio": round(best_ratio, 1),
                "below_count": best_index,
            }
        )
        return threshold, evidence
    evidence["reason"] = "面积分布无明显断层"
    return 0.0, evidence


def _pick_layers(
    counts: dict[str, int], min_count: int, ratio: float
) -> list[str]:
    if not counts:
        return []
    top = max(counts.values())
    return [
        layer
        for layer, count in sorted(counts.items(), key=lambda item: -item[1])
        if count >= min_count and count >= top * ratio
    ]


# ---------------------------------------------------------------------------
# 提议主流程
# ---------------------------------------------------------------------------


def propose_profile(
    filepath: str | Path,
    *,
    name: str | None = None,
    run_validation: bool = True,
) -> ProfileProposal:
    """分析 DXF 结构，产出候选画像草稿 + 证据 + 待确认问题。"""
    filepath = Path(filepath)
    profile_name = name or filepath.stem
    doc = ezdxf.readfile(str(filepath))

    fingerprint = analyze_drawing(filepath)
    shape_stats = _closed_shape_stats(doc)
    text_stats = _text_stats(doc)

    # 规格件图层：封闭图形（不含圆，圆按孔洞处理）最多的图层。
    panel_counts = {
        layer: count
        for layer, count in shape_stats["layer_closed"].items()
        if count
    }
    panel_layers = _pick_layers(
        panel_counts, _MIN_PANEL_LAYER_COUNT, _PANEL_LAYER_RATIO
    )
    primary_panel_layer = panel_layers[0] if panel_layers else "0"

    # 编号图层与正则。
    number_counts = {
        layer: len(stats["candidates"])
        for layer, stats in text_stats.items()
        if stats["candidates"]
    }
    number_layers = _pick_layers(
        number_counts, _MIN_NUMBER_LAYER_COUNT, _NUMBER_LAYER_RATIO
    )
    number_texts = [
        text
        for layer in number_layers
        for text in text_stats[layer]["candidates"]
    ]
    label = induce_label_pattern(number_texts)

    all_gaps = [
        gap for gaps in shape_stats["unclosed_gaps"].values() for gap in gaps
    ]
    closed_tolerance, tolerance_evidence = _propose_closed_tolerance(all_gaps)

    all_areas = [
        area for areas in shape_stats["layer_areas"].values() for area in areas
    ]
    small_area, area_evidence = _propose_small_area_threshold(all_areas)

    circles_total = sum(shape_stats["layer_circles"].values())
    has_hatch = bool(fingerprint.get("has_hatch"))
    hatch_layers: Counter[str] = Counter()
    if has_hatch:
        for entity in doc.modelspace():
            if entity.dxftype() == "HATCH":
                hatch_layers[getattr(entity.dxf, "layer", "0") or "0"] += 1

    exclude_linetypes = sorted(
        lt for lt in shape_stats["linetype_counts"] if lt != _CONTINUOUS
    )

    heights = [
        height
        for layer in number_layers
        for height in text_stats[layer]["heights"]
    ]
    text_height = (
        _round_significant(_percentile(sorted(heights), 0.5))
        if heights
        else 40.0
    )

    draft: dict[str, Any] = {
        "name": profile_name,
        "version": "1.0.0",
        "description": (
            f"{filepath.name} 读图画像（propose 自动提议，待人工确认）。"
        ),
        "panel_layer": primary_panel_layer,
        "hatch_layer": (
            hatch_layers.most_common(1)[0][0] if hatch_layers else None
        ),
        "use_hatch": has_hatch,
        "number_layers": number_layers,
        "label_pattern": label["pattern"],
        "assignment_mode": "point_then_bbox",
        "build_hierarchy": circles_total > 0,
        "conflict_resolution": "random",
        "closed_tolerance": closed_tolerance,
        "exclude_entity_types": [],
        "exclude_linetypes": exclude_linetypes,
        "small_area_threshold": small_area,
        "text_height": text_height,
        "material_group_enabled": False,
        "first_part_left_edge": False,
        "notes": (
            "propose 自动提议草稿：expected_counts 为候选参数实读结果，"
            "未经人工确认。"
        ),
    }
    if len(panel_layers) > 1:
        draft["panel_layers"] = panel_layers

    evidence: dict[str, Any] = {
        "fingerprint": {k: v for k, v in fingerprint.items() if k != "path"},
        "panel_layer_counts": panel_counts,
        "circle_layer_counts": shape_stats["layer_circles"],
        "linetype_counts": shape_stats["linetype_counts"],
        "text_layer_totals": {
            layer: stats["total"] for layer, stats in text_stats.items()
        },
        "number_candidate_counts": number_counts,
        "number_samples": {
            layer: sorted(set(text_stats[layer]["candidates"]))[:8]
            for layer in number_layers
        },
        "label_induction": label,
        "closure_gaps": tolerance_evidence,
        "area_distribution": area_evidence,
        "hatch_layer_counts": dict(hatch_layers),
    }

    validation: dict[str, Any] = {}
    if run_validation:
        validation = validate_draft(filepath, draft)
        draft["expected_counts"] = {
            "panels": validation["panels"],
            "holes": validation["holes"],
            "numbers": validation["numbers"],
        }

    questions = _build_questions(
        draft,
        evidence,
        validation,
        number_layers=number_layers,
        panel_counts=panel_counts,
        circles_total=circles_total,
    )
    return ProfileProposal(
        name=profile_name,
        draft=draft,
        evidence=evidence,
        questions=questions,
        validation=validation,
    )


def validate_draft(
    filepath: str | Path, draft: dict[str, Any]
) -> dict[str, Any]:
    """用生产读图链路实读候选画像，给出件数与审图问题分布。"""
    profile = DrawingProfile.from_dict(draft)
    started = time.monotonic()
    parts_data, _doc = read_dxf_with_profile(filepath, profile)
    issues = audit_drawing(filepath, profile)
    seconds = time.monotonic() - started

    numbers = sum(1 for part in parts_data if part.get("original_number"))
    return {
        "panels": len(parts_data),
        "holes": sum(len(part.get("holes") or []) for part in parts_data),
        "numbers": numbers,
        "unnumbered": len(parts_data) - numbers,
        "issue_counts": dict(Counter(issue.type for issue in issues)),
        "seconds": round(seconds, 1),
    }


def _build_questions(
    draft: dict[str, Any],
    evidence: dict[str, Any],
    validation: dict[str, Any],
    *,
    number_layers: list[str],
    panel_counts: dict[str, int],
    circles_total: int,
) -> list[str]:
    questions: list[str] = []
    counts = draft.get("expected_counts") or {}
    questions.append(
        f"识别规格件 {counts.get('panels', '?')} 件、"
        f"孔洞 {counts.get('holes', '?')} 个"
        f"（图层 {draft['panel_layer']}，分布 {panel_counts}）"
        "——请对照 CAD 实际数量确认。"
    )
    label = evidence["label_induction"]
    if not number_layers:
        questions.append(
            "未找到编号文本图层（短文本候选不足）——图纸有没有编号？"
            "若有，请告知编号所在图层与格式。"
        )
    else:
        questions.append(
            f"编号图层 {number_layers}，提议正则 {label['pattern']}，"
            f"覆盖候选文本 {label['coverage']:.1%}"
            f"（{label['covered']}/{label['total']}）"
            f"；未覆盖样例：{label['uncovered_samples'] or '无'}"
            "——未覆盖的是不是编号？格式是否需要放宽？"
        )
    if validation.get("unnumbered"):
        questions.append(
            f"有 {validation['unnumbered']} 件没有匹配到编号"
            "——编号归属方式（point_then_bbox）是否适用，还是编号在图形外？"
        )
    if draft["exclude_linetypes"]:
        questions.append(
            f"提议排除线型 {draft['exclude_linetypes']}"
            f"（实体数 {evidence['linetype_counts']}）"
            "——这些线型的内容是什么（大样图/辅助线），确认不参与排板？"
        )
    area = evidence["area_distribution"]
    if draft["small_area_threshold"]:
        questions.append(
            f"面积分布存在断层（{area.get('gap_below')} ~ "
            f"{area.get('gap_above')}，断层下 {area.get('below_count')} 件），"
            f"提议小面积阈值 {draft['small_area_threshold']}"
            "——小于阈值的图形是不是规格件？"
        )
    else:
        questions.append(
            f"面积分布无明显断层（最小 {area.get('min', '?')}）"
            "——图面有没有应排除的零碎小件？有则人工设 small_area_threshold。"
        )
    questions.append(
        f"孔洞层级 build_hierarchy={draft['build_hierarchy']}"
        f"（CIRCLE 共 {circles_total} 个）"
        "——图中的圆/内部封闭图形是否都是孔洞？"
    )
    gaps = evidence["closure_gaps"]
    if gaps.get("warning"):
        questions.append(
            f"闭合容差保持 {draft['closed_tolerance']}：{gaps['warning']}。"
        )
    elif gaps.get("count"):
        questions.append(
            f"有 {gaps['count']} 条未闭合多段线（p95 缝隙 {gaps.get('p95')}），"
            f"提议 closed_tolerance={draft['closed_tolerance']}，"
            f"可恢复 {gaps.get('recovered_at_suggestion')} 条"
            "——这些是否都是视觉闭合的规格件？"
        )
    return questions


# ---------------------------------------------------------------------------
# 登记
# ---------------------------------------------------------------------------


def register_profile(
    filepath: str | Path,
    draft_path: str | Path,
    *,
    force: bool = False,
    profiles_root: str | Path | None = None,
) -> dict[str, Any]:
    """把确认后的画像草稿登记进画像库。

    复算 expected_counts 并与草稿对比：不一致默认抛 ``ValueError``；
    ``force=True`` 时仍登记并把警告写进 notes。同名画像/案例已存在时
    抛 ``FileExistsError``（``force=True`` 覆盖）。
    """
    filepath = Path(filepath)
    draft_path = Path(draft_path)
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    profile = DrawingProfile.from_dict(draft)

    actual = validate_draft(filepath, draft)
    expected = draft.get("expected_counts") or {}
    mismatches = {
        key: {"expected": expected.get(key), "actual": actual[key]}
        for key in ("panels", "holes", "numbers")
        if expected.get(key) is not None and expected.get(key) != actual[key]
    }
    if mismatches and not force:
        raise ValueError(
            "expected_counts 与复算结果不一致，拒绝登记："
            f"{mismatches}。请先修正画像参数；确认无误后用 --force 登记。"
        )

    root = (
        Path(profiles_root)
        if profiles_root
        else PROJECT_ROOT / "drawing_profiles"
    )
    profile_path = root / f"{profile.name}.json"
    case_path = root / "validated_cases" / f"{profile.name}.json"
    if (profile_path.exists() or case_path.exists()) and not force:
        raise FileExistsError(
            f"画像 {profile.name!r} 已存在（{profile_path}）；"
            "确需覆盖请用 --force。"
        )

    fingerprint = analyze_drawing(filepath)
    fingerprint.pop("path", None)
    profile_dict = dict(draft)
    profile_dict["expected_counts"] = {
        "panels": actual["panels"],
        "holes": actual["holes"],
        "numbers": actual["numbers"],
    }
    profile_dict["fingerprint"] = fingerprint
    if mismatches:
        profile_dict["notes"] = (
            f"{profile_dict.get('notes', '')} [登记警告] expected_counts "
            f"与复算不一致仍登记：{mismatches}。"
        ).strip()

    case = {
        "case_id": profile.name,
        "profile_name": profile.name,
        "source_sha256": file_sha256(str(filepath)),
        "fingerprint": fingerprint,
        "expected_counts": profile_dict["expected_counts"],
        "notes": f"由 --register-profile 登记（源文件 {filepath.name}）。",
        "audit_ignore_rules": draft.get("audit_ignore_rules", []),
    }

    case_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(profile_dict, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    case_path.write_text(
        json.dumps(case, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "profile_path": profile_path,
        "case_path": case_path,
        "actual": actual,
        "mismatches": mismatches,
    }
