# -*- coding: utf-8 -*-
"""通用读图分析（只读图、不排板）。

输出四部分：
  1) 原始封闭实体普查（全部图层，仅做线型/闭合过滤）
  2) 层级分类（外轮廓 / 孔洞）
  3) 画像管线结果（自动匹配画像，含 expected_counts 对比）
  3.5) 列头材料（画像启用 material_column_enabled 时）
  4) 审图问题清单 + 原位编号 DXF（含材料号：零件级 MATERIALS +
     原图列头标签原位的 MATERIAL_HEADER）

用法：
  python -X utf8 _analysis/analyze_drawing.py <dxf路径> [输出目录] \
      [--no-audit] [--cold-start] [--profile <画像名>]

不指定输出目录时写入 output/{时间戳}_{文件名}/。
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ezdxf  # noqa: E402

from src.drawing_profile import audit_drawing  # noqa: E402
from src.drawing_profile import (  # noqa: E402
    collect_column_materials,
    column_material_for_x,
    load_profiles,
)
from src.dxf_reader import (  # noqa: E402
    _build_part_hierarchy,
    extract_closed_entity_candidates,
)
from src.dxf_writer import write_numbered_parts_dxf  # noqa: E402
from src.workflow import prepare_drawing  # noqa: E402

SEP = "=" * 62


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not positional:
        print(__doc__)
        return 2

    dxf_path = Path(positional[0])
    if not dxf_path.exists():
        print(f"错误：找不到文件 {dxf_path}")
        return 1
    out_dir = (
        Path(positional[1])
        if len(positional) > 1
        else PROJECT_ROOT / "output"
        / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{dxf_path.stem}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    report: dict = {"dxf": str(dxf_path), "output_dir": str(out_dir)}
    print(f"读取 DXF: {dxf_path}  ({dxf_path.stat().st_size / 1e6:.1f} MB)")
    doc = ezdxf.readfile(str(dxf_path))

    # ── 1) 原始封闭实体普查 ─────────────────────────────────────
    raw = extract_closed_entity_candidates(doc, layer_filter_mode="off")
    by_type = Counter(c["entity_type"] for c in raw)
    by_layer = Counter(c["layer"] for c in raw)
    forced = [c for c in raw if float(c.get("closure_gap") or 0.0) > 0.0]

    print()
    print(SEP)
    print("1) 原始封闭实体普查（全部图层）")
    print(SEP)
    print(f"modelspace 实体总数        : {len(doc.modelspace())}")
    print(f"封闭图形实体总数           : {len(raw)}")
    print(f"  按类型: {dict(by_type)}")
    print(f"  按图层: {dict(by_layer.most_common(12))}")
    print(f"  非精确闭合(强制闭合)件数 : {len(forced)}")

    # ── 2) 层级分类：外轮廓 vs 孔洞 ──────────────────────────────
    items = [(c["polygon"], c.get("handle")) for c in raw]
    hierarchy = _build_part_hierarchy(items)
    outers = [h for h in hierarchy if h["parent"] == -1]
    holes = [h for h in hierarchy if h["parent"] != -1]

    print()
    print(SEP)
    print("2) 层级分类（质心包容 + 交集面积比 > 90%）")
    print(SEP)
    print(f"最外层轮廓数               : {len(outers)}")
    print(f"被包含(孔洞)数             : {len(holes)}")

    report.update({
        "modelspace_entities": len(doc.modelspace()),
        "raw_closed_total": len(raw),
        "raw_by_type": dict(by_type),
        "raw_by_layer": dict(by_layer),
        "raw_forced_closed": len(forced),
        "hierarchy_outers": len(outers),
        "hierarchy_holes": len(holes),
    })

    # ── 3) 画像管线（自动匹配） ──────────────────────────────────
    print()
    print(SEP)
    print("3) 画像管线（自动匹配 drawing_profiles/）")
    print(SEP)
    prepared = prepare_drawing(
        str(dxf_path),
        drawing_profile=_explicit_profile(flags),
        skip_unnumbered=False,
        use_case_library="--cold-start" not in flags,
    )
    if prepared.error:
        print(f"错误：{prepared.error}")
        report["error"] = prepared.error
        _write_summary(out_dir, dxf_path, report)
        return 1

    profile = prepared.drawing_profile
    parts = prepared.parts
    total_holes = sum(len(p.holes) for p in parts)
    hole_area = sum(h.area for p in parts for h in p.holes)
    numbered = [p for p in parts if p.original_number]
    auto = [p for p in parts if not p.original_number]
    dup = [
        (n, c) for n, c in Counter(
            p.original_number for p in numbered
        ).items() if c > 1
    ]
    forced_parts = [p for p in parts if getattr(p, "closure_gap", 0.0) > 0.0]

    print(f"使用画像                   : "
          f"{profile.name if profile is not None else '（无画像，通用回退）'}")
    if profile is not None and profile.expected_counts:
        print(f"画像声明 expected_counts   : {profile.expected_counts}")
    print(f"识别面板(parts)            : {len(parts)}")
    print(f"  其中原编号非空           : {len(numbered)}")
    print(f"  其中自动编号 P-xxxx      : {len(auto)}")
    print(f"  重复原编号               : "
          f"{[f'{n}x{c}' for n, c in dup] or '无'}")
    print(f"孔洞总数(跟随面板)         : {total_holes}")
    print(f"总面积(m^2)                : {prepared.total_area / 1e6:.3f}")
    print(f"孔洞总面积(m^2)            : {hole_area / 1e6:.3f}")
    print(f"强制闭合件数(缺口>0)       : {len(forced_parts)}")
    if forced_parts:
        print("  强制闭合件: " + ", ".join(
            f"{p.original_number or p.number}"
            f"(缺口 {p.closure_gap:.0f}mm)" for p in forced_parts[:12]
        ))
    if profile is not None and profile.expected_counts and len(parts):
        expected = profile.expected_counts
        print("画像期望值对比：")
        for key, actual in (
            ("panels", len(parts)),
            ("holes", total_holes),
            ("numbers", len(numbered)),
        ):
            if expected.get(key) is None:
                continue
            mark = "一致" if expected.get(key) == actual else "**不一致**"
            print(f"  {key:<7}: 期望 {expected[key]} / 实际 {actual} -> {mark}")

    report.update({
        "profile": profile.name if profile is not None else None,
        "profile_expected_counts": (
            profile.expected_counts if profile is not None else None
        ),
        "parts": len(parts),
        "numbered": len(numbered),
        "auto_numbered": len(auto),
        "duplicate_original_numbers": [
            {"number": n, "count": c} for n, c in dup
        ],
        "holes": total_holes,
        "total_area_m2": round(prepared.total_area / 1e6, 3),
        "hole_area_m2": round(hole_area / 1e6, 3),
        "forced_closure_parts": [
            {
                "number": p.original_number or p.number,
                "gap_mm": round(float(p.closure_gap), 1),
            }
            for p in forced_parts
        ],
    })

    # ── 3.5) 列头材料（多层平面图：每列一个楼层） ─────────────────
    material_columns: list[dict] = []
    material_headers: list[dict] = []
    if profile is not None and profile.material_column_enabled:
        material_columns = collect_column_materials(doc, profile)
        material_headers = [
            {
                "text": column["label"],
                "point": column["point"],
                "height": column["height"],
            }
            for column in material_columns
            if column.get("label") and column.get("point")
        ]
        by_material = Counter(
            column["material_group"] for column in material_columns
        )
        unmatched = [p for p in parts if not p.material_group]

        print()
        print(SEP)
        print("3.5) 列头材料（材料号写在原位）")
        print(SEP)
        print(f"列数（楼层）               : {len(material_columns)}")
        print("各材料列数                 : " + ", ".join(
            f"{key}: {value}" for key, value in sorted(by_material.items())
        ))
        for column in material_columns:
            if column.get("warning"):
                print(f"  警告：{column['warning']}")
        print(f"已按列归属零件             : "
              f"{len(parts) - len(unmatched)} / {len(parts)}")
        print(f"未归入任何列的零件         : {len(unmatched)}")
        if unmatched:
            for part in unmatched[:10]:
                print(f"  {part.number} 质心="
                      f"({part.label_position[0]:.0f},"
                      f"{part.label_position[1]:.0f})")

        report.update({
            "material_column_count": len(material_columns),
            "material_columns": [
                {
                    "column": column["column"],
                    "level": column["level"],
                    "materials": column["materials"],
                    "material_group": column["material_group"],
                    "x_min": (
                        None if column["x_min"] == float("-inf")
                        else round(float(column["x_min"]), 1)
                    ),
                    "x_max": (
                        None if column["x_max"] == float("inf")
                        else round(float(column["x_max"]), 1)
                    ),
                    "warning": column.get("warning"),
                }
                for column in material_columns
            ],
            "material_columns_by_group": dict(sorted(by_material.items())),
            "parts_without_material_column": [
                {
                    "number": part.number,
                    "x": round(float(part.label_position[0]), 1),
                    "y": round(float(part.label_position[1]), 1),
                }
                for part in unmatched
            ],
        })
    elif profile is not None:
        print()
        print("（画像未启用 material_column_enabled，原位 DXF 只有编号、没有材料号）")

    # ── 4) 审图问题 ──────────────────────────────────────────────
    if "--no-audit" not in flags and profile is not None:
        print()
        print(SEP)
        print("4) 审图（按画像规则做一次体检）")
        print(SEP)
        audit_started = time.perf_counter()
        issues = audit_drawing(str(dxf_path), profile)
        by_issue_type = Counter(issue.type for issue in issues)
        by_severity = Counter(issue.severity for issue in issues)
        print(f"问题总数                   : {len(issues)}  "
              f"（{time.perf_counter() - audit_started:.1f}s）")
        print(f"  按严重度: {dict(by_severity)}")
        for issue_type, count in by_issue_type.most_common():
            example = next(
                issue for issue in issues if issue.type == issue_type
            )
            print(f"  {issue_type:<28} {count:>4} 例  例：{example.message}")
        audit_path = out_dir / f"{dxf_path.stem}_audit.json"
        audit_path.write_text(
            json.dumps(
                {
                    "dxf": str(dxf_path),
                    "profile": profile.name,
                    "issue_count": len(issues),
                    "by_type": dict(by_issue_type),
                    "by_severity": dict(by_severity),
                    "issues": [issue.to_dict() for issue in issues],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"已写入: {audit_path}")
        report["audit_issues"] = len(issues)
        report["audit_by_type"] = dict(by_issue_type)
        report["audit_by_severity"] = dict(by_severity)
        report["audit_json"] = str(audit_path)
    elif profile is None:
        print()
        print("（未匹配到画像，跳过审图：审图需要 drawing_profiles/*.json 规则）")

    # ── 5) 原位编号 DXF ──────────────────────────────────────────
    print()
    print(SEP)
    print("5) 生成原位编号 DXF（坐标不变，供人工审核）")
    print(SEP)
    suffix = "_房号材料" if material_headers else ""
    numbered_dxf = out_dir / f"{dxf_path.stem}_numbered_原位{suffix}.dxf"
    write_numbered_parts_dxf(
        parts,
        str(numbered_dxf),
        unit_system="metric",
        material_headers=material_headers,
    )
    print(f"OUTER={len(parts)} 个外轮廓, HOLES={total_holes} 个孔洞, "
          f"NUMBERS={len(parts)} 个编号")
    if material_headers:
        print(f"MATERIALS={len(parts)} 个零件材料号, "
              f"MATERIAL_HEADER={len(material_headers)} 个原位列头标签")
    print(f"已写入: {numbered_dxf}")
    report["numbered_dxf"] = str(numbered_dxf)
    report["numbered_dxf_material_tags"] = len(material_headers)

    recognized = out_dir / f"{dxf_path.stem}_recognized.json"
    recognized.write_text(
        prepared.recognized_drawing.to_json(), encoding="utf-8"
    )
    print(f"已写入: {recognized}")

    report["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    _write_summary(out_dir, dxf_path, report)
    print(f"已写入: {out_dir / f'{dxf_path.stem}_analysis_summary.json'}")

    print()
    print(f"结论：封闭图形 {len(raw)} 个"
          f"（最外层轮廓 {len(outers)}，被包含 {len(holes)}）；"
          f"画像管线识别面板 {len(parts)} 件、孔洞 {total_holes} 个；"
          f"耗时 {report['elapsed_seconds']}s（未排板）。")
    return 0


def _explicit_profile(flags: set[str]):
    """支持 --profile=<画像名> 显式指定画像（绕过自动匹配与并列告警）。"""
    names = [flag.split("=", 1)[1] for flag in flags if flag.startswith("--profile=")]
    if not names:
        return None
    wanted = names[0].casefold()
    for profile in load_profiles(PROJECT_ROOT / "drawing_profiles"):
        if profile.name.casefold() == wanted:
            return profile
    raise SystemExit(f"错误：找不到画像 {names[0]}")


def _write_summary(out_dir: Path, dxf_path: Path, report: dict) -> None:
    (out_dir / f"{dxf_path.stem}_analysis_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())
