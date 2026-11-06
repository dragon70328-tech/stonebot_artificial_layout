"""Audit a nested DXF for circular-saw collisions with neighbouring parts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.saw_audit import audit_nested_saw_clearance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查排板 DXF 的内部凹切点是否会让刀盘碰到相邻规格板"
    )
    parser.add_argument("nested_dxf", help="需要检查的排板 DXF")
    parser.add_argument(
        "--diameter",
        type=float,
        required=True,
        help="圆盘锯直径 mm",
    )
    parser.add_argument(
        "--thickness",
        type=float,
        required=True,
        help="板厚 mm",
    )
    parser.add_argument(
        "--plunge-extra",
        type=float,
        default=2.0,
        help="切透后额外下切量 mm，默认 2",
    )
    parser.add_argument(
        "--safety-margin",
        type=float,
        default=0.0,
        help="额外安全量 mm，默认 0",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        help="同时把完整审计结果写入 JSON",
    )
    parser.add_argument(
        "--fail-on-risk",
        action="store_true",
        help="发现邻板碰撞风险时返回退出码 2",
    )
    args = parser.parse_args(argv)

    report = audit_nested_saw_clearance(
        args.nested_dxf,
        diameter_mm=args.diameter,
        thickness_mm=args.thickness,
        plunge_extra_mm=args.plunge_extra,
        safety_margin_mm=args.safety_margin,
    )
    if args.json_path:
        json_path = Path(args.json_path).expanduser().resolve()
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    clearance = report["circular_saw"]["clearance_mm"]
    print(f"让刀宽度: {clearance:.3f}mm")
    print(
        f"检查切点: {report['points_checked']}，"
        f"不足: {report['points_insufficient']}"
    )
    if report["min_clearance_mm"] is not None:
        print(f"最小邻板间距: {report['min_clearance_mm']:.3f}mm")
    if report["safe"]:
        print("结论: 未发现刀盘切入相邻规格板的风险")
        return 0

    print("结论: 存在刀盘切入相邻规格板的风险")
    for issue in report["issue_pairs"][:20]:
        print(
            f"  Sheet_{issue['sheet']}: "
            f"{issue['part']} -> {issue['neighbor_part']} "
            f"x{issue['occurrences']}"
        )
    return 2 if args.fail_on_risk else 0


if __name__ == "__main__":
    raise SystemExit(main())
