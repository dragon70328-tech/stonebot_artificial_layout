"""审图 CLI：DXF 读图审图、问题状态管理、复检与证据输出。

从 ``main.py`` 抽出的审图适配层（对应 ``docs/drawing-intake-subsystem.md``
的步骤 1-2）。它只做读图和审图，不进入排板、后处理或生产写文件流程，
因此可以独立演进和测试，避免继续把功能堆进 ``main.py``。
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

from src.audit_cache import (
    DEFAULT_CACHE_DIR,
    audit_cache_key,
    load_cached_issues,
    save_cached_issues,
)
from src.cli_paths import make_output_dir
from src.contracts import (
    DrawingIssue as ContractDrawingIssue,
    IssueSeverity,
    IssueStatus,
    RecheckSummary,
    ReviewState,
)
from src.drawing_profile import audit_drawing, write_audit_dxf
from src.visual_evidence import write_issue_evidence_svg
from src.visual_renderer import write_dxf_overview_svg
from src.workflow import resolve_drawing_profile

MAX_EVIDENCE_ISSUES = 100


def severity_to_contract(value: str) -> IssueSeverity:
    return {
        "error": IssueSeverity.ERROR,
        "warning": IssueSeverity.WARNING,
        "info": IssueSeverity.INFO,
    }.get(value, IssueSeverity.WARNING)


def to_contract_issue(issue) -> ContractDrawingIssue:
    """把 drawing_profile.DrawingIssue 转成读图数据契约。"""
    return ContractDrawingIssue(
        issue_id=str(issue.issue_id),
        severity=severity_to_contract(issue.severity),
        issue_type=issue.type,
        entity_handle=issue.entity_handle,
        layer=issue.layer,
        coordinates=issue.coordinates,
        message=issue.message,
        suggestion=issue.suggestion,
        status=IssueStatus.NEW,
    )


def file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def issues_match(
    previous: ContractDrawingIssue,
    current: ContractDrawingIssue,
    coordinate_tolerance: float = 10.0,
) -> bool:
    """按实体句柄、坐标或图层/消息判断是否为同一问题。"""
    if previous.issue_type != current.issue_type:
        return False
    if previous.entity_handle and current.entity_handle:
        return previous.entity_handle == current.entity_handle
    if previous.coordinates and current.coordinates:
        return (
            math.hypot(
                previous.coordinates[0] - current.coordinates[0],
                previous.coordinates[1] - current.coordinates[1],
            )
            <= coordinate_tolerance
        )
    return previous.layer == current.layer and previous.message == current.message


def split_issue_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def apply_issue_statuses(
    issues: list[ContractDrawingIssue],
    accept_issue_ids: list[str],
    ignore_issue_ids: list[str],
    fixed_issue_ids: list[str],
) -> list[ContractDrawingIssue]:
    """把用户指定的状态写入当前问题列表。"""
    status_by_id: dict[str, IssueStatus] = {}
    for issue_id in accept_issue_ids:
        status_by_id[issue_id] = IssueStatus.ACCEPTED
    for issue_id in ignore_issue_ids:
        status_by_id[issue_id] = IssueStatus.IGNORED
    for issue_id in fixed_issue_ids:
        status_by_id[issue_id] = IssueStatus.FIXED
    if not status_by_id:
        return issues

    updated: list[ContractDrawingIssue] = []
    for issue in issues:
        status = status_by_id.get(issue.issue_id)
        if status is None:
            updated.append(issue)
        else:
            updated.append(issue.model_copy(update={"status": status}))
    return updated


def run_audit(
    dxf_path: str,
    previous_state_path: str | None = None,
    accept_issue_ids: list[str] | None = None,
    ignore_issue_ids: list[str] | None = None,
    fixed_issue_ids: list[str] | None = None,
    use_cache: bool = True,
) -> None:
    """只做读图审图，输出问题 JSON 和高亮 DXF，不进入排板。

    ``use_cache=False``（冷启动）跳过审图缓存读写，强制重新计算。
    """
    if Path(dxf_path).suffix.lower() != ".dxf":
        print(f"错误：仅支持 DXF 输入，收到 - {dxf_path}")
        sys.exit(1)
    stem = Path(dxf_path).stem
    out_dir = make_output_dir(dxf_path)

    drawing_profile = resolve_drawing_profile(dxf_path)
    if drawing_profile is None:
        print("错误：审图模式需要匹配到 drawing_profiles/*.json 图纸画像。")
        sys.exit(1)

    print(f"matched drawing profile: {drawing_profile.name}")
    current_hash = file_sha256(dxf_path)
    cache_key = audit_cache_key(current_hash, drawing_profile)
    issues = (
        load_cached_issues(DEFAULT_CACHE_DIR, cache_key)
        if use_cache else None
    )
    if issues is None:
        print("开始审图..." if use_cache else "开始审图（冷启动：忽略审计缓存）...")
        issues = audit_drawing(dxf_path, drawing_profile)
        if use_cache:
            save_cached_issues(DEFAULT_CACHE_DIR, cache_key, issues)
    else:
        print(f"使用审计缓存: {cache_key[:24]}...")
    contract_issues = [to_contract_issue(issue) for issue in issues]

    previous_state = None
    recheck = None
    if previous_state_path:
        previous_path = Path(previous_state_path)
        if not previous_path.exists():
            print(f"错误：找不到上次审图状态文件 - {previous_state_path}")
            sys.exit(1)
        previous_state = ReviewState.from_json(
            previous_path.read_text(encoding="utf-8")
        )
        if previous_state.file_sha256 == current_hash:
            print("提示：当前文件与上次审图文件哈希相同，复检差异可能为空。")

    audit_json = out_dir / f"{stem}_audit.json"
    audit_dxf = out_dir / f"{stem}_audit.dxf"
    state_json = out_dir / f"{stem}_review_state.json"
    recheck_json = out_dir / f"{stem}_recheck.json"

    if previous_state is not None:
        matched_new_indexes: set[int] = set()
        recheck_fixed_issue_ids: list[str] = []
        still_open_issue_ids: list[str] = []
        for old_issue in previous_state.issues:
            matched = False
            for index, current_issue in enumerate(contract_issues):
                if index in matched_new_indexes:
                    continue
                if issues_match(old_issue, current_issue):
                    contract_issues[index] = current_issue.model_copy(
                        update={"status": old_issue.status}
                    )
                    still_open_issue_ids.append(old_issue.issue_id)
                    matched_new_indexes.add(index)
                    matched = True
                    break
            if not matched:
                recheck_fixed_issue_ids.append(old_issue.issue_id)
        new_issue_ids = [
            issue.issue_id
            for index, issue in enumerate(contract_issues)
            if index not in matched_new_indexes
        ]
        recheck = RecheckSummary(
            parent_review_id=previous_state.review_id,
            child_review_id=f"review_{current_hash[:16]}",
            fixed_issue_ids=recheck_fixed_issue_ids,
            still_open_issue_ids=still_open_issue_ids,
            new_issue_ids=new_issue_ids,
        )

    contract_issues = apply_issue_statuses(
        contract_issues,
        accept_issue_ids=accept_issue_ids or [],
        ignore_issue_ids=ignore_issue_ids or [],
        fixed_issue_ids=fixed_issue_ids or [],
    )

    if len(contract_issues) <= MAX_EVIDENCE_ISSUES:
        evidence_results = write_issue_evidence_svg(
            dxf_path, contract_issues, out_dir
        )
    else:
        print(
            f"警告：问题数量 {len(contract_issues)} 超过 "
            f"{MAX_EVIDENCE_ISSUES}，跳过逐问题 SVG，仅生成整图检查图。"
        )
        evidence_results = []
    if evidence_results:
        evidence_by_issue_id = {
            item["issue_id"]: item for item in evidence_results
        }
        contract_issues = [
            issue.model_copy(
                update={
                    "evidence_artifact_id": evidence_by_issue_id.get(
                        issue.issue_id, {}
                    ).get("artifact_id"),
                    "evidence_digest": evidence_by_issue_id.get(
                        issue.issue_id, {}
                    ).get("digest"),
                }
            )
            if issue.issue_id in evidence_by_issue_id
            else issue
            for issue in contract_issues
        ]

    summary = Counter(issue.issue_type for issue in contract_issues)
    payload = {
        "schema_version": contract_issues[0].schema_version if contract_issues else "0.1.0",
        "source": str(Path(dxf_path).resolve()),
        "file_sha256": current_hash,
        "issue_count": len(contract_issues),
        "summary": dict(summary),
        "issues": [issue.model_dump(mode="json") for issue in contract_issues],
    }
    if recheck is not None:
        payload["recheck"] = recheck.model_dump(mode="json")
    audit_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_audit_dxf(dxf_path, issues, audit_dxf)
    overview_svg = out_dir / f"{stem}_overview.svg"
    try:
        write_dxf_overview_svg(dxf_path, overview_svg, issues=issues)
    except Exception as exc:
        print(f"警告：生成整图检查 SVG 失败 - {exc}")
        overview_svg = None

    review_state = ReviewState(
        review_id=recheck.child_review_id if recheck else f"review_{current_hash[:16]}",
        drawing_path=str(Path(dxf_path).resolve()),
        file_sha256=current_hash,
        profile_name=drawing_profile.name,
        parent_review_id=previous_state.review_id if previous_state else None,
        issues=contract_issues,
    )
    state_json.write_text(review_state.to_json(), encoding="utf-8")
    if recheck is not None:
        recheck_json.write_text(recheck.to_json(), encoding="utf-8")

    print(f"审图完成：发现 {len(contract_issues)} 个问题")
    for issue_type, count in sorted(summary.items()):
        print(f"  {issue_type}: {count}")
    for issue in issues[:10]:
        print(
            f"  ! [{issue.severity}] {issue.type} "
            f"({issue.coordinates[0]:.1f}, {issue.coordinates[1]:.1f}) "
            f"{issue.message}"
        )
    print(f"JSON: {audit_json}")
    print(f"高亮 DXF: {audit_dxf}")
    if overview_svg is not None:
        print(f"检查图: {overview_svg}")
    print(f"状态: {state_json}")
    if recheck is not None:
        print(
            f"复检: 修复 {len(recheck.fixed_issue_ids)}，"
            f"仍存在 {len(recheck.still_open_issue_ids)}，"
            f"新增 {len(recheck.new_issue_ids)}"
        )
        print(f"复检 JSON: {recheck_json}")
