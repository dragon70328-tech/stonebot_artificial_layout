"""画像提议 / 登记的 CLI 入口（从 main.py 抽出，保持 main 精简）。

用法：

    # 第 1 步：提议候选画像（只读图，不排板）
    python main.py new_drawing.dxf --propose-profile [--profile-name xxx]

    # 第 2 步：人工核对问题清单，必要时编辑草稿 JSON

    # 第 3 步：登记入库（复算 expected_counts，写画像 + 已验证案例）
    python main.py new_drawing.dxf --register-profile output/.../profile_draft_xxx.json
"""

from __future__ import annotations

import json
import sys

from src.cli_paths import make_output_dir
from src.profile_proposal import propose_profile, register_profile


def run_propose_profile(dxf_path: str, *, name: str | None = None) -> None:
    proposal = propose_profile(dxf_path, name=name)
    out_dir = make_output_dir(dxf_path)
    draft_path = out_dir / f"profile_draft_{proposal.name}.json"
    report_path = out_dir / "profile_proposal_report.json"

    draft_path.write_text(
        json.dumps(proposal.draft, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(
            {
                "name": proposal.name,
                "draft_file": str(draft_path),
                "validation": proposal.validation,
                "questions": proposal.questions,
                "evidence": proposal.evidence,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    validation = proposal.validation
    print(f"\n画像提议完成：{proposal.name}")
    print(f"  草稿：{draft_path}")
    print(f"  报告：{report_path}")
    if validation:
        print(
            f"  实读结果（{validation['seconds']}s）："
            f"规格件 {validation['panels']} 件，"
            f"孔洞 {validation['holes']} 个，"
            f"编号匹配 {validation['numbers']} 件"
        )
        if validation["issue_counts"]:
            print(f"  审图问题分布：{validation['issue_counts']}")
    print("\n待确认问题：")
    for index, question in enumerate(proposal.questions, 1):
        print(f"  {index}. {question}")
    print(
        "\n下一步：对照 CAD 核对上述问题，必要时编辑草稿 JSON，然后执行：\n"
        f'  python main.py "{dxf_path}" --register-profile "{draft_path}"'
    )


def run_register_profile(
    dxf_path: str, draft_path: str, *, force: bool = False
) -> None:
    try:
        result = register_profile(dxf_path, draft_path, force=force)
    except (ValueError, FileExistsError) as exc:
        print(f"错误：{exc}")
        sys.exit(1)

    actual = result["actual"]
    print("\n画像登记完成：")
    print(f"  画像：{result['profile_path']}")
    print(f"  案例：{result['case_path']}")
    print(
        f"  expected_counts：规格件 {actual['panels']} 件，"
        f"孔洞 {actual['holes']} 个，编号 {actual['numbers']} 件"
    )
    if result["mismatches"]:
        print(f"  [警告] 草稿 expected_counts 与复算不一致：{result['mismatches']}")
    print("  之后该图纸可直接使用：--drawing-profile "
          + result["profile_path"].stem)
