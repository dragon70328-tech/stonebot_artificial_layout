# -*- coding: utf-8 -*-
"""统计加拿大IFC.dxf 的封闭图形，并生成原位编号 DXF。

输出四部分：
  1) 原始封闭实体普查（全部图层，仅做线型/闭合过滤）
  2) 层级分类（外轮廓 / 孔洞）
  3) canada_ifc 画像管线结果（与画像 expected_counts 对比）
  4) 原位编号 DXF -> {stem}_numbered_原位.dxf

用法：
  python -X utf8 _analysis/analyze_ifc_drawing.py <dxf路径> <输出目录>
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ezdxf  # noqa: E402

from src.dxf_reader import (  # noqa: E402
    extract_closed_entity_candidates,
    _build_part_hierarchy,
)
from src.drawing_profile import load_profiles  # noqa: E402
from src.workflow import prepare_drawing  # noqa: E402
from src.dxf_writer import write_numbered_parts_dxf  # noqa: E402

PROFILE_NAME = "canada_ifc"


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python -X utf8 _analysis/analyze_ifc_drawing.py <dxf> <out_dir>")
        return 2
    dxf_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    if not dxf_path.exists():
        print(f"错误：找不到文件 {dxf_path}")
        return 1

    report: dict = {"dxf": str(dxf_path)}
    doc = ezdxf.readfile(str(dxf_path))

    # ── 1) 原始封闭实体普查 ──────────────────────────────────────
    raw = extract_closed_entity_candidates(doc, layer_filter_mode="off")
    by_type = Counter(c["entity_type"] for c in raw)
    by_layer = Counter(c["layer"] for c in raw)
    forced = [c for c in raw if float(c.get("closure_gap") or 0.0) > 0.0]

    print("=" * 62)
    print("1) 原始封闭实体普查（全部图层）")
    print("=" * 62)
    print(f"modelspace 实体总数        : {len(doc.modelspace())}")
    print(f"封闭图形实体总数           : {len(raw)}")
    print(f"  按类型: {dict(by_type)}")
    print(f"  按图层: {dict(by_layer)}")
    print(f"  非精确闭合(强制闭合)件数 : {len(forced)}")

    # ── 2) 层级分类：外轮廓 vs 孔洞 ──────────────────────────────
    items = [(c["polygon"], c.get("handle")) for c in raw]
    hierarchy = _build_part_hierarchy(items)
    outers = [h for h in hierarchy if h["parent"] == -1]
    holes = [h for h in hierarchy if h["parent"] != -1]

    print()
    print("=" * 62)
    print("2) 层级分类（质心包容 + 交集面积比 > 90%）")
    print("=" * 62)
    print(f"最外层轮廓数               : {len(outers)}")
    print(f"被包含(孔洞)数             : {len(holes)}")

    report["raw_closed_total"] = len(raw)
    report["raw_by_type"] = dict(by_type)
    report["raw_by_layer"] = dict(by_layer)
    report["raw_forced_closed"] = len(forced)
    report["hierarchy_outers"] = len(outers)
    report["hierarchy_holes"] = len(holes)

    # ── 3) canada_ifc 画像管线 ───────────────────────────────────
    print()
    print("=" * 62)
    print(f"3) 画像管线（{PROFILE_NAME}）")
    print("=" * 62)
    profiles = load_profiles(PROJECT_ROOT / "drawing_profiles")
    target = next((p for p in profiles if p.name == PROFILE_NAME), None)
    if target is None:
        print(f"错误：找不到画像 {PROFILE_NAME}")
        return 1
    print(f"显式使用画像               : {target.name}")
    print(f"画像声明 expected_counts   : {target.expected_counts}")

    prepared = prepare_drawing(
        str(dxf_path),
        drawing_profile=target,
        skip_unnumbered=False,
        use_case_library=False,
    )
    if prepared.error:
        print(f"错误：{prepared.error}")
        return 1

    parts = prepared.parts
    total_holes = sum(len(p.holes) for p in parts)
    hole_area = sum(h.area for p in parts for h in p.holes)
    numbered = [p for p in parts if p.original_number]
    auto = [p for p in parts if not p.original_number]
    dup = [n for n, c in Counter(p.original_number for p in numbered).items() if c > 1]
    forced_parts = [p for p in parts if p.closure_gap > 0.0]

    print(f"识别面板(parts)            : {len(parts)}")
    print(f"  其中原编号非空           : {len(numbered)}")
    print(f"  其中自动编号 P-xxxx      : {len(auto)}")
    print(f"  重复原编号               : {dup}")
    print(f"孔洞总数(跟随面板)         : {total_holes}")
    print(f"总面积(m^2)                : {prepared.total_area / 1e6:.3f}")
    print(f"孔洞总面积(m^2)            : {hole_area / 1e6:.3f}")
    print(f"强制闭合件数(缺口>0)       : {len(forced_parts)}")

    if target.expected_counts:
        exp = target.expected_counts
        print()
        print("画像期望值对比：")
        print(f"  panels : 期望 {exp.get('panels')} / 实际 {len(parts)}"
              f" -> {'一致' if exp.get('panels') == len(parts) else '不一致'}")
        print(f"  holes  : 期望 {exp.get('holes')} / 实际 {total_holes}"
              f" -> {'一致' if exp.get('holes') == total_holes else '不一致'}")
        print(f"  numbers: 期望 {exp.get('numbers')} / 实际 {len(numbered)}"
              f" -> {'一致' if exp.get('numbers') == len(numbered) else '不一致'}")

    report.update({
        "profile": target.name,
        "profile_expected_counts": target.expected_counts,
        "parts": len(parts),
        "numbered": len(numbered),
        "auto_numbered": len(auto),
        "duplicate_original_numbers": dup,
        "holes": total_holes,
        "total_area_m2": round(prepared.total_area / 1e6, 3),
        "hole_area_m2": round(hole_area / 1e6, 3),
        "forced_closure_parts": len(forced_parts),
    })

    # ── 4) 输出原位编号 DXF ──────────────────────────────────────
    print()
    print("=" * 62)
    print("4) 生成原位编号 DXF")
    print("=" * 62)
    numbered_dxf = out_dir / f"{dxf_path.stem}_numbered_原位.dxf"
    write_numbered_parts_dxf(parts, str(numbered_dxf), unit_system="metric")
    print(f"OUTER={len(parts)} 个外轮廓, HOLES={total_holes} 个孔洞, "
          f"NUMBERS={len(parts)} 个编号")
    print(f"已写入: {numbered_dxf}")
    report["numbered_dxf"] = str(numbered_dxf)

    # 识别明细（供需要时核对）
    recognized = out_dir / f"{dxf_path.stem}_recognized.json"
    recognized.write_text(prepared.recognized_drawing.to_json(), encoding="utf-8")
    print(f"已写入: {recognized}")

    summary = out_dir / f"{dxf_path.stem}_analysis_summary.json"
    summary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已写入: {summary}")

    print()
    print(f"结论：封闭图形 {len(raw)} 个"
          f"（最外层轮廓 {len(outers)}，被包含 {len(holes)}）；"
          f"画像管线识别面板 {len(parts)} 件、孔洞 {total_holes} 个。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
