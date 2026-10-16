"""画像库历史回填：把 output/ 里已核对过的读图结果补成完整画像资产。

入库标准是五件齐：画像 + fingerprint + expected_counts + notes + 已验证案例
（见 AGENTS.md「画像库积累」）。历史项目里有一部分只落了画像或只落了案例，
本模块按"只补缺口、不覆盖已确认数据"的原则把它们补齐：

1. 扫描 ``output/*/*_recognized.json``：读图契约自带源图 ``input_digest``
   （sha256）与所用画像名，是回填的线索来源；
2. 用 sha256 在给定源图目录里定位源 DXF——**按内容配对，不按文件名猜**；
3. 用生产读图链路复算件数与指纹（一次读图同时产出两者）；
4. 与画像/案例中已声明的值比对：**不一致只报告**（``force`` 才写并留痕），
   缺口（缺案例 / 缺 source_sha256 / 缺 fingerprint / 缺 expected_counts）
   默认补。

本模块只读写画像库：不排板、不改画像参数、不覆盖已有的 expected_counts。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from src.cli_audit import file_sha256
from src.cli_paths import PROJECT_ROOT
from src.drawing_profile import (
    DrawingProfile,
    fingerprint_from_doc,
    load_profile,
    read_dxf_with_profile,
)

COUNT_KEYS = ("panels", "holes", "numbers")


# ---------------------------------------------------------------------------
# 生产探针：一次读图同时产出件数与指纹
# ---------------------------------------------------------------------------


def probe_drawing(path: str | Path, profile: DrawingProfile) -> dict[str, Any]:
    """用生产链路读一张图，返回 ``{"counts": {...}, "fingerprint": {...}}``。

    指纹取自同一次读图的文档对象，避免为同一张大图再打开一遍。
    """
    parts_data, doc = read_dxf_with_profile(path, profile)
    counts = {
        "panels": len(parts_data),
        "holes": sum(len(part.get("holes") or []) for part in parts_data),
        "numbers": sum(
            1 for part in parts_data if part.get("original_number")
        ),
    }
    fingerprint = fingerprint_from_doc(doc, path)
    fingerprint.pop("path", None)
    return {"counts": counts, "fingerprint": fingerprint}


# ---------------------------------------------------------------------------
# 线索采集
# ---------------------------------------------------------------------------


def index_sources(directory: str | Path) -> dict[str, Path]:
    """把源图目录里的 ``*.dxf`` 按 sha256 建索引（内容配对，不吃文件名）。"""
    index: dict[str, Path] = {}
    directory = Path(directory)
    if not directory.exists():
        return index
    for path in sorted(directory.glob("*.dxf")):
        try:
            digest = file_sha256(path)
        except OSError:
            continue
        index.setdefault(digest, path)
    return index


def counts_from_panels(panels: Iterable[dict[str, Any]]) -> tuple[int, int, int]:
    """从读图契约的 panels 还原 expected_counts 三元组。"""
    panels = list(panels)
    return (
        len(panels),
        sum(len(panel.get("holes") or []) for panel in panels),
        sum(1 for panel in panels if panel.get("original_number")),
    )


@dataclass(frozen=True)
class RecognizedRun:
    """一次历史读图运行的线索（来自 ``*_recognized.json``）。"""

    contract_path: Path
    profile_name: str
    input_digest: str
    counts: tuple[int, int, int]

    @property
    def run_dir(self) -> Path:
        return self.contract_path.parent


def scan_recognized_runs(
    output_root: str | Path,
) -> dict[str, list[RecognizedRun]]:
    """按画像名归集历史读图结果（同一张图多次运行会得到多条）。"""
    runs: dict[str, list[RecognizedRun]] = {}
    for path in sorted(Path(output_root).glob("*/*recognized*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = data.get("profile_name")
        digest = data.get("input_digest")
        if not name or not digest:
            continue
        runs.setdefault(str(name), []).append(
            RecognizedRun(
                contract_path=path,
                profile_name=str(name),
                input_digest=str(digest),
                counts=counts_from_panels(data.get("panels") or []),
            )
        )
    return runs


@dataclass
class CaseInfo:
    """一个已验证案例的现状（保留原始 dict 以便原样回写）。"""

    path: Path
    data: dict[str, Any]

    @property
    def case_id(self) -> str:
        return str(self.data.get("case_id") or self.path.stem)

    @property
    def profile_name(self) -> str:
        return str(self.data.get("profile_name") or "")

    @property
    def source_sha256(self) -> str | None:
        value = self.data.get("source_sha256")
        return str(value) if value else None

    @property
    def declared(self) -> dict[str, Any]:
        return self.data.get("expected_counts") or {}


def load_cases(cases_dir: str | Path) -> dict[str, list[CaseInfo]]:
    """按 profile_name 归集已验证案例（一个画像可以对应多张源图）。"""
    cases: dict[str, list[CaseInfo]] = {}
    cases_dir = Path(cases_dir)
    if not cases_dir.exists():
        return cases
    for path in sorted(cases_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        info = CaseInfo(path=path, data=data)
        cases.setdefault(info.profile_name, []).append(info)
    return cases


# ---------------------------------------------------------------------------
# 源图定位
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """一条"这个画像对应这张源图"的证据。"""

    sha256: str
    path: Path
    origin: str  # "explicit" | "case" | "recognized" | "case-path"


def _find_recorded_file(
    recorded_path: str, sources_dir: str | Path | None
) -> Path | None:
    """按案例记录的原始路径找源图；图纸被挪过位置时按文件名在源图目录下回找。"""
    path = Path(recorded_path)
    if path.is_file():
        return path
    if sources_dir is None:
        return None
    matches = [
        candidate
        for candidate in Path(sources_dir).rglob(path.name)
        if candidate.is_file()
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_source(
    name: str,
    cases: list[CaseInfo],
    runs: list[RecognizedRun],
    sources: dict[str, Path],
    overrides: dict[str, str] | None = None,
    sources_dir: str | Path | None = None,
) -> tuple[SourceRef | None, list[str]]:
    """确定画像对应的源图。

    优先级：显式指定 > 案例记录的 sha256 > 历史运行摘要 > 案例指纹记录的
    原图路径（按名称回找，必须通过指纹结构复核才采信）。案例与历史运行
    指向不同源图时信任案例，并把冲突作为发现项报告（历史运行可能是误配
    画像跑出来的，不是"已核对"的结果）。
    """
    findings: list[str] = []
    overrides = overrides or {}

    if name in overrides:
        path = Path(overrides[name])
        if not path.exists():
            return None, [f"显式指定的源图不存在：{path}"]
        return SourceRef(file_sha256(path), path, "explicit"), findings

    case_digests = {c.source_sha256 for c in cases if c.source_sha256}
    run_digests = {r.input_digest for r in runs}

    if len(case_digests) > 1:
        findings.append(
            "该画像有多个案例指向不同源图，画像级回填需人工指定（一对多）"
        )
    if case_digests and run_digests and case_digests != run_digests:
        findings.append(
            "历史运行摘要与案例不一致（疑似误配画像的历史运行），"
            "已按案例为准"
        )
    if len(run_digests) > 1 and not case_digests:
        findings.append("历史运行指向多个不同源图，无法确定唯一源图")

    for digest, origin in (
        (next(iter(case_digests)) if len(case_digests) == 1 else None, "case"),
        (next(iter(run_digests)) if len(run_digests) == 1 else None, "recognized"),
    ):
        if digest is None:
            continue
        if digest in sources:
            return SourceRef(digest, sources[digest], origin), findings
        findings.append(
            f"源图摘要 {digest[:12]}…（来自{origin}）在源图目录中找不到"
        )

    for case in cases:
        recorded = (case.data.get("fingerprint") or {}).get("path")
        if not recorded:
            continue
        found = _find_recorded_file(str(recorded), sources_dir)
        if found is None:
            findings.append(
                f"案例 {case.case_id} 记录的原图路径也找不到：{recorded}"
            )
            continue
        findings.append(
            f"源图按案例 {case.case_id} 记录的原图路径回找（名称证据，"
            "需指纹结构复核）"
        )
        return SourceRef(file_sha256(found), found, "case-path"), findings
    return None, findings


# ---------------------------------------------------------------------------
# 回填计划
# ---------------------------------------------------------------------------


@dataclass
class Action:
    """一条待执行的回填动作。"""

    kind: str  # case:create / case:fill_sha256 / profile:fill_fingerprint
    #            / profile:fill_expected_counts
    label: str
    target: Path
    payload: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str | None = None


@dataclass
class ProfileReport:
    """一个画像的回填体检结果。"""

    name: str
    profile_path: Path
    source: SourceRef | None = None
    actual: dict[str, int] | None = None
    declared: dict[str, Any] | None = None
    actions: list[Action] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.actions and not self.blockers


def _declared_for_source(
    profile_dict: dict[str, Any],
    cases: list[CaseInfo],
    sha256: str,
    *,
    single_source: bool,
) -> list[tuple[str, dict[str, Any]]]:
    """只收集"确实属于这张源图"的已声明件数。

    一画像多源图时（如东莞奥特莱斯1/2 共用 ``dongguan_outlets``），拿另一张图
    的声明来比当前源图没有意义，因此只比案例里 sha256 与本源图一致的那条。
    """
    declared: list[tuple[str, dict[str, Any]]] = []
    if single_source and profile_dict.get("expected_counts"):
        declared.append(("画像", profile_dict["expected_counts"]))
    for case in cases:
        if case.source_sha256 == sha256 and case.declared:
            declared.append((f"案例 {case.case_id}", case.declared))
    return declared


def _mismatches(
    declared: list[tuple[str, dict[str, Any]]], actual: dict[str, int]
) -> dict[str, dict[str, int]]:
    """已声明件数与复算结果不一致的项（不含未声明的项）。"""
    result: dict[str, dict[str, int]] = {}
    for label, values in declared:
        for key in COUNT_KEYS:
            value = values.get(key)
            if value is not None and value != actual[key]:
                result.setdefault(key, {})[label] = value
    return result


def _format_mismatches(
    mismatches: dict[str, dict[str, int]], actual: dict[str, int]
) -> str:
    return "；".join(
        f"{key}: "
        + "、".join(
            f"{label}={value}" for label, value in sorted(entries.items())
        )
        + f" / 复算={actual[key]}"
        for key, entries in sorted(mismatches.items())
    )


# 指纹里的结构性字段（数量确定、与代码版本无关），用于复核"这张源图就是
# 当年登记的那张图"。
_STRUCT_KEYS = (
    "dxf_version",
    "insunits",
    "entity_counts",
    "layer_counts",
    "linetype_counts",
    "has_hatch",
    "has_lwpolyline",
    "has_line",
    "has_circle",
)


def _fingerprint_conflict(
    cases: list[CaseInfo], computed: dict[str, Any]
) -> str | None:
    """源图与案例记录的结构指纹是否对不上（对不上说明绑错了源图）。"""
    for case in cases:
        stored = case.data.get("fingerprint") or {}
        if not stored:
            continue
        diffs = [
            key for key in _STRUCT_KEYS if stored.get(key) != computed.get(key)
        ]
        if diffs:
            return (
                f"案例 {case.case_id} 记录的结构指纹与本次复算不一致"
                f"（{'、'.join(diffs)}），源图绑定可疑"
            )
        return None
    return None


def build_plan(
    *,
    profiles_dir: str | Path | None = None,
    output_root: str | Path | None = None,
    sources_dir: str | Path | None = None,
    overrides: dict[str, str] | None = None,
    only: Iterable[str] | None = None,
    verify_all: bool = False,
    probe: Callable[[str | Path, DrawingProfile], dict[str, Any]] | None = None,
) -> list[ProfileReport]:
    """体检画像库并给出回填计划（默认只读，不写任何文件）。"""
    root = (
        Path(PROJECT_ROOT) / "drawing_profiles"
        if profiles_dir is None
        else Path(profiles_dir)
    )
    output_root = (
        Path(PROJECT_ROOT) / "output" if output_root is None else Path(output_root)
    )
    cases_dir = root / "validated_cases"
    probe = probe or probe_drawing
    sources = index_sources(sources_dir) if sources_dir else {}
    runs = scan_recognized_runs(output_root)
    cases = load_cases(cases_dir)

    wanted = set(only) if only else None
    reports: list[ProfileReport] = []
    for profile_path in sorted(root.glob("*.json")):
        profile_dict = json.loads(profile_path.read_text(encoding="utf-8"))
        if not (
            profile_dict.get("panel_layer")
            or profile_dict.get("panel_layers")
        ):
            # 画像目录里可能有非画像的 JSON（报表、清单），一律不碰。
            continue
        name = str(profile_dict.get("name") or profile_path.stem)
        if wanted and name not in wanted:
            continue
        profile_cases = cases.get(name, [])
        report = ProfileReport(
            name=name,
            profile_path=profile_path,
            declared=profile_dict.get("expected_counts") or None,
        )
        reports.append(report)

        missing_case = not profile_cases
        needs_fingerprint = not profile_dict.get("fingerprint")
        needs_counts = not profile_dict.get("expected_counts")
        cases_missing_sha = [c for c in profile_cases if not c.source_sha256]
        if not (
            missing_case
            or needs_fingerprint
            or needs_counts
            or cases_missing_sha
            or verify_all
        ):
            report.findings.append("五件齐，无需回填")
            continue

        if not sources:
            report.blockers.append("未提供源图目录（--sources），无法核对件数")
            continue

        source, findings = resolve_source(
            name,
            profile_cases,
            runs.get(name, []),
            sources,
            overrides,
            sources_dir=sources_dir,
        )
        report.findings.extend(findings)
        report.source = source
        if source is None:
            report.blockers.append(
                "未能定位源图（源图目录里没有匹配的 sha256）"
            )
            continue

        profile = load_profile(profile_path)
        outcome = probe(source.path, profile)
        report.actual = outcome["counts"]
        # 只跟"确实属于这张源图"的案例比指纹：一画像多源图时（东莞奥特莱斯1/2）
        # 拿另一张图记录的结构去比会误报。
        conflict_cases = [
            case
            for case in profile_cases
            if case.source_sha256 == source.sha256
        ]
        if not conflict_cases:
            # 案例还没写 sha256（正是要回填的那条）或源图按名称回找：
            # 这些案例都可能就是本源图的，需要复核。
            conflict_cases = [
                case for case in profile_cases if not case.source_sha256
            ] or (
                profile_cases if source.origin == "case-path" else []
            )
        conflict = _fingerprint_conflict(
            conflict_cases, outcome["fingerprint"]
        )
        if conflict:
            report.findings.append(conflict)
            if source.origin == "case-path":
                report.source = None
                report.blockers.append(
                    f"{conflict}；按名称回找的源图绑定已放弃，"
                    "请用 --source 显式指定"
                )
                continue
        identity_block = conflict

        single_source = (
            len({c.source_sha256 for c in profile_cases if c.source_sha256}) <= 1
            and len({r.input_digest for r in runs.get(name, [])}) <= 1
        )
        declared = _declared_for_source(
            profile_dict,
            profile_cases,
            source.sha256,
            single_source=single_source,
        )
        report.declared = {label: dict(values) for label, values in declared}
        mismatches = _mismatches(declared, report.actual)
        count_block = None
        if mismatches:
            count_block = (
                "已声明件数与复算不一致，需人工确认："
                + _format_mismatches(mismatches, report.actual)
            )
            report.findings.append(count_block)

        if needs_counts:
            if count_block:
                reason = count_block
            elif len(profile_cases) > 1:
                reason = "一画像多源图，expected_counts 不做猜测（需人工确认）"
            else:
                reason = None
            report.actions.append(
                Action(
                    kind="profile:fill_expected_counts",
                    label=f"补画像 expected_counts {report.actual}",
                    target=profile_path,
                    payload=dict(report.actual),
                    blocked_reason=reason,
                )
            )
        if needs_fingerprint:
            report.actions.append(
                Action(
                    kind="profile:fill_fingerprint",
                    label="补画像 fingerprint（画像排序用）",
                    target=profile_path,
                    payload=outcome["fingerprint"],
                    blocked_reason=identity_block,
                )
            )
        if missing_case:
            report.actions.append(
                Action(
                    kind="case:create",
                    label=f"新建已验证案例 {name}.json（sha256 已核对）",
                    target=cases_dir / f"{name}.json",
                    payload={
                        "case_id": name,
                        "profile_name": name,
                        "source_sha256": source.sha256,
                        "fingerprint": outcome["fingerprint"],
                        "expected_counts": dict(report.actual),
                        "notes": (
                            f"由历史回填登记（源文件 {source.path.name}，"
                            f"线索来源 {source.origin}）。"
                        ),
                        "audit_ignore_rules": profile_dict.get(
                            "audit_ignore_rules", []
                        ),
                    },
                    blocked_reason=count_block or identity_block,
                )
            )
        for case in cases_missing_sha:
            report.actions.append(
                Action(
                    kind="case:fill_sha256",
                    label=f"补案例 {case.case_id} 的 source_sha256",
                    target=case.path,
                    payload={"source_sha256": source.sha256},
                    blocked_reason=identity_block,
                )
            )
    return reports


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def apply_plan(
    reports: Iterable[ProfileReport],
    *,
    write: bool = False,
    force: bool = False,
) -> list[str]:
    """执行回填计划；``write=False``（默认）时只返回将要执行的动作。"""
    applied: list[str] = []
    for report in reports:
        for action in report.actions:
            if action.blocked_reason and not force:
                applied.append(
                    f"[跳过] {report.name}：{action.label}"
                    f" —— {action.blocked_reason}"
                )
                continue
            warning = (
                f" [回填警告] {action.blocked_reason}"
                if action.blocked_reason
                else ""
            )
            if action.kind == "case:create":
                payload = dict(action.payload)
                if warning:
                    payload["notes"] = (
                        f"{payload.get('notes', '')}{warning}"
                    ).strip()
                if write:
                    action.target.parent.mkdir(parents=True, exist_ok=True)
                    _write_json(action.target, payload)
            elif action.kind == "case:fill_sha256":
                if action.target.exists():
                    data = json.loads(action.target.read_text(encoding="utf-8"))
                    data.update(action.payload)
                    if warning:
                        data["notes"] = (
                            f"{data.get('notes', '')}{warning}"
                        ).strip()
                    if write:
                        _write_json(action.target, data)
            elif action.kind in (
                "profile:fill_fingerprint",
                "profile:fill_expected_counts",
            ):
                data = json.loads(action.target.read_text(encoding="utf-8"))
                if action.kind == "profile:fill_fingerprint":
                    data["fingerprint"] = action.payload
                else:
                    data["expected_counts"] = action.payload
                if warning:
                    data["notes"] = f"{data.get('notes', '')}{warning}".strip()
                if write:
                    _write_json(action.target, data)
            else:  # pragma: no cover - 防御未知动作
                raise ValueError(f"未知回填动作：{action.kind}")
            prefix = "已回填" if write else "待回填"
            applied.append(f"[{prefix}] {report.name}：{action.label}")
    return applied


def has_blocked_actions(report: ProfileReport) -> bool:
    """该画像是否还有被拦下的回填动作（需人工确认）。"""
    return bool(report.blockers) or any(
        action.blocked_reason for action in report.actions
    )


def _status(report: ProfileReport) -> str:
    pending = [a for a in report.actions if not a.blocked_reason]
    if has_blocked_actions(report):
        return "受阻"
    if pending:
        return "待回填"
    findings = [f for f in report.findings if f != "五件齐，无需回填"]
    return "完整·有发现" if findings else "完整"


def render_report(reports: Iterable[ProfileReport]) -> str:
    """把体检结果渲染成终端输出。"""
    lines: list[str] = []
    for report in reports:
        lines.append(f"[{_status(report)}] {report.name}")
        if report.source is not None:
            lines.append(
                f"    源图：{report.source.path.name}"
                f"（{report.source.origin}, sha256 {report.source.sha256[:12]}…）"
            )
        if report.actual is not None:
            lines.append(f"    复算：{report.actual}")
        for blocker in report.blockers:
            lines.append(f"    ! {blocker}")
        for action in report.actions:
            mark = "!" if action.blocked_reason else "+"
            suffix = (
                f" —— {action.blocked_reason}" if action.blocked_reason else ""
            )
            lines.append(f"    {mark} {action.label}{suffix}")
        for finding in report.findings:
            lines.append(f"    · {finding}")
    return "\n".join(lines)
