"""画像库历史回填 CLI（默认只报告，不改文件）。

把 ``output/`` 里已人工核对过的读图结果补成完整画像资产：缺指纹的补指纹、
缺 expected_counts 的补件数、缺 source_sha256 的补摘要、缺已验证案例的新建
案例。源图**按 sha256 内容配对**，源图目录不在仓库里，运行时用 ``--sources``
指定（例如把历史图纸集中存放的目录）。

用法：

    # 只体检（默认），打印每个画像的缺口与待执行动作
    python scripts/backfill_validated_cases.py --sources "D:\\图纸"

    # 只回填指定画像
    python scripts/backfill_validated_cases.py --sources "D:\\图纸" --only canada3l,citizen

    # 直接绑定某画像的源图（源图目录里没有、或一张图有多个历史版本时）
    python scripts/backfill_validated_cases.py --source mercury="D:\\图纸\\水星项目户型及排板.dxf"

    # 落盘
    python scripts/backfill_validated_cases.py --sources "D:\\图纸" --write

``--verify-all`` 会连"已经五件齐"的画像也复算一遍件数（慢，用于抽查画像库
是否与当前代码一致）；不一致默认只报告，``--force`` 才写并把警告留痕进 notes。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.case_backfill import (  # noqa: E402
    apply_plan,
    build_plan,
    has_blocked_actions,
    render_report,
)


def _parse_overrides(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        name, sep, path = value.partition("=")
        if not sep or not name or not path:
            raise SystemExit(
                f"错误：--source 需要 NAME=PATH 形式，收到 {value!r}"
            )
        overrides[name.strip()] = path.strip()
    return overrides


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        default=None,
        help="存放历史源图 DXF 的目录（按 sha256 内容配对）",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="显式绑定某画像的源图，可重复",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="只处理这些画像（逗号分隔）",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="落盘（默认只报告）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="件数不一致时仍写入，并把警告留痕进 notes",
    )
    parser.add_argument(
        "--verify-all",
        action="store_true",
        help="对已完整的画像也复算件数（慢）",
    )
    args = parser.parse_args()

    reports = build_plan(
        sources_dir=args.sources,
        overrides=_parse_overrides(args.source),
        only=[name.strip() for name in args.only.split(",")]
        if args.only
        else None,
        verify_all=args.verify_all,
    )
    if not reports:
        print("没有可处理的画像。")
        return 0

    print(render_report(reports))
    applied = apply_plan(reports, write=args.write, force=args.force)
    if applied:
        print()
        for line in applied:
            print(line)

    blocked = [report.name for report in reports if has_blocked_actions(report)]
    if blocked:
        print(f"\n{len(blocked)} 个画像有待人工确认的动作：{', '.join(blocked)}")
        return 1 if not args.write else 0
    print("\n全部画像已完整或已按计划回填。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
