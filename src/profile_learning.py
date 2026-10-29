"""Conservative project-level profile learning for plugin corrections.

Corrections create candidate patches only. A patch becomes active only after
explicit review, deterministic replay against the source drawing and creation
of a project-scoped profile override. Base profiles and validated cases are
never modified by this module.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.drawing_profile import (
    DrawingProfile,
    analyze_drawing,
    fingerprint_similarity,
    read_dxf_with_profile,
)
from src.job_context import JobContext
from src.profile_proposal import induce_label_pattern


PATCH_SCHEMA_VERSION = "0.1.0"
OVERRIDE_MATCH_THRESHOLD = 60.0
ALLOWED_PATCH_FIELDS = {
    "panel_layers",
    "panel_layer",
    "hole_layers",
    "number_layers",
    "label_pattern",
    "small_area_threshold",
    "closed_tolerance",
    "assignment_mode",
    "exclude_layers",
    "exclude_linetypes",
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _patch_root(
    data_root: str | Path,
    tenant_id: str,
    project_id: str,
) -> Path:
    return (
        Path(data_root)
        / "profile_learning"
        / tenant_id
        / project_id
        / "patches"
    )


def _override_root(
    data_root: str | Path,
    tenant_id: str,
    project_id: str,
) -> Path:
    return (
        Path(data_root)
        / "tenants"
        / tenant_id
        / project_id
        / "profile_overrides"
    )


def _override_index_path(root: Path) -> Path:
    return root / "index.json"


def _load_override_index(root: Path) -> dict[str, Any]:
    path = _override_index_path(root)
    if not path.exists():
        return {
            "schema_version": PATCH_SCHEMA_VERSION,
            "active": {},
            "history": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "schema_version": PATCH_SCHEMA_VERSION,
            "active": {},
            "history": [],
        }
    payload.setdefault("active", {})
    payload.setdefault("history", [])
    return payload


def _save_override_index(root: Path, payload: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _override_index_path(root).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _part_layer_evidence(context: JobContext) -> tuple[list[str], int]:
    values: set[str] = set()
    count = 0
    for item in context.parts_data or []:
        layer = str(item.get("layer") or "").strip()
        if layer:
            values.add(layer)
            count += 1
    return sorted(values), count


def _number_layer_evidence(context: JobContext) -> tuple[list[str], int]:
    values: set[str] = set()
    count = 0
    for item in context.parts_data or []:
        if not str(item.get("original_number") or "").strip():
            continue
        layer = str(item.get("number_layer") or "").strip()
        if layer:
            values.add(layer)
            count += 1
    return sorted(values), count


def propose_profile_patches(
    context: JobContext,
    *,
    data_root: str | Path,
    tenant_id: str,
    project_id: str,
) -> list[dict[str, Any]]:
    profile = context.drawing_profile
    if profile is None or context.correction_revision <= 0:
        return []

    changes: list[dict[str, Any]] = []
    panel_layers, panel_evidence = _part_layer_evidence(context)
    current_panel_layers = set(profile.panel_layers or [profile.panel_layer])
    current_panel_layers.discard("")
    missing_panel_layers = sorted(
        set(panel_layers) - current_panel_layers
    )
    if missing_panel_layers and panel_evidence:
        after = sorted(current_panel_layers | set(missing_panel_layers))
        changes.append(
            {
                "target": "panel_layers",
                "before": sorted(current_panel_layers),
                "after": after,
                "evidence_count": panel_evidence,
            }
        )

    number_layers, number_evidence = _number_layer_evidence(context)
    current_number_layers = set(profile.number_layers)
    missing_number_layers = sorted(
        set(number_layers) - current_number_layers
    )
    if missing_number_layers and number_evidence:
        changes.append(
            {
                "target": "number_layers",
                "before": sorted(current_number_layers),
                "after": sorted(
                    current_number_layers | set(missing_number_layers)
                ),
                "evidence_count": number_evidence,
            }
        )

    corrected_numbers = [
        str(part.number)
        for part in (context.parts or [])
        if str(getattr(part, "number", "") or "").strip()
    ]
    if len(corrected_numbers) >= 2:
        induction = induce_label_pattern(corrected_numbers)
        pattern = str(induction.get("pattern") or "")
        if (
            pattern
            and pattern != profile.label_pattern
            and float(induction.get("coverage", 0.0)) >= 0.95
        ):
            changes.append(
                {
                    "target": "label_pattern",
                    "before": profile.label_pattern,
                    "after": pattern,
                    "evidence_count": len(corrected_numbers),
                }
            )

    if not changes:
        return []

    evidence = {
        "job_id": getattr(context, "job_id", None),
        "drawing_digest": hashlib.sha256(
            Path(context.dxf_path).read_bytes()
        ).hexdigest(),
        "correction_revision": context.correction_revision,
        "panel_count": len(context.parts or []),
        "hole_count": sum(
            len(part.holes) for part in (context.parts or [])
        ),
        "number_count": len(corrected_numbers),
        "target_counts": {
            "panels": len(context.parts or []),
            "holes": sum(
                len(part.holes) for part in (context.parts or [])
            ),
            "numbers": len(corrected_numbers),
        },
    }
    canonical = json.dumps(
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "base_profile": profile.name,
            "changes": changes,
            "drawing_digest": evidence["drawing_digest"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    patch_id = "profilepatch-" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:16]
    patch = {
        "schema_version": PATCH_SCHEMA_VERSION,
        "patch_id": patch_id,
        "status": "candidate",
        "tenant_id": tenant_id,
        "project_id": project_id,
        "base_profile_name": profile.name,
        "changes": changes,
        "evidence": evidence,
        "validation": None,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    root = _patch_root(data_root, tenant_id, project_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{patch_id}.json"
    if not path.exists():
        path.write_text(
            json.dumps(patch, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return [patch]


def list_profile_patches(
    data_root: str | Path,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    base = Path(data_root) / "profile_learning"
    patches = []
    for path in sorted(base.glob("*/*/patches/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if tenant_id and payload.get("tenant_id") != tenant_id:
            continue
        if project_id and payload.get("project_id") != project_id:
            continue
        if status and payload.get("status") != status:
            continue
        patches.append(payload)
    return patches


def load_profile_patch(
    data_root: str | Path,
    patch_id: str,
) -> dict[str, Any]:
    for patch in list_profile_patches(data_root):
        if patch.get("patch_id") == patch_id:
            return patch
    raise FileNotFoundError(f"profile patch not found: {patch_id}")


def _save_patch(data_root: str | Path, patch: dict[str, Any]) -> None:
    root = _patch_root(
        data_root,
        str(patch["tenant_id"]),
        str(patch["project_id"]),
    )
    path = root / f"{patch['patch_id']}.json"
    path.write_text(
        json.dumps(patch, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def reject_profile_patch(
    data_root: str | Path,
    patch_id: str,
) -> dict[str, Any]:
    patch = load_profile_patch(data_root, patch_id)
    patch["status"] = "rejected"
    patch["updated_at"] = _utc_now()
    _save_patch(data_root, patch)
    return patch


def _apply_changes(
    profile: DrawingProfile,
    changes: list[dict[str, Any]],
) -> DrawingProfile:
    data = asdict(profile)
    for change in changes:
        target = str(change.get("target") or "")
        if target not in ALLOWED_PATCH_FIELDS:
            raise ValueError(f"unsupported profile patch target: {target}")
        data[target] = change.get("after")
    panel_layers = list(data.get("panel_layers") or [])
    if panel_layers:
        data["panel_layer"] = panel_layers[0]
    patched = DrawingProfile.from_dict(data)
    patched.version = f"{profile.version}+plugin"
    return patched


def _count_score(
    actual: dict[str, int],
    target: dict[str, int],
) -> int:
    return sum(
        1
        for key in ("panels", "holes", "numbers")
        if int(actual.get(key, -1)) == int(target.get(key, -2))
    )


def _read_counts(path: Path, profile: DrawingProfile) -> dict[str, int]:
    parts, _doc = read_dxf_with_profile(path, profile)
    return {
        "panels": len(parts),
        "holes": sum(len(item.get("holes") or []) for item in parts),
        "numbers": sum(
            1
            for item in parts
            if item.get("original_number")
        ),
    }


def _historical_replay(
    *,
    base: DrawingProfile,
    candidate: DrawingProfile,
    current_digest: str,
    target_counts: dict[str, int],
) -> dict[str, Any]:
    cases_root = PROJECT_ROOT / "drawing_profiles" / "validated_cases"
    fixtures_root = PROJECT_ROOT / "tests" / "fixtures" / "dxf"
    fixture_by_digest = {}
    for path in sorted(fixtures_root.glob("*.dxf")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        fixture_by_digest[digest] = path

    checked = []
    skipped = []
    regressions = []
    errors = []
    for case_path in sorted(cases_root.glob("*.json")):
        try:
            case = json.loads(case_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if case.get("profile_name") != base.name:
            continue
        digest = str(case.get("source_sha256") or "")
        fixture = fixture_by_digest.get(digest)
        if fixture is None:
            skipped.append(
                {
                    "case_id": case.get("case_id") or case_path.stem,
                    "reason": "fixture_missing",
                }
            )
            continue
        try:
            base_counts = _read_counts(fixture, base)
            candidate_counts = _read_counts(fixture, candidate)
        except Exception as exc:  # noqa: BLE001 - validation gate
            errors.append(
                {
                    "case_id": case.get("case_id") or case_path.stem,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if digest == current_digest:
            acceptable = _count_score(candidate_counts, target_counts) >= (
                _count_score(base_counts, target_counts)
            )
            comparison = target_counts
        else:
            acceptable = candidate_counts == base_counts
            comparison = base_counts
        record = {
            "case_id": case.get("case_id") or case_path.stem,
            "base_counts": base_counts,
            "candidate_counts": candidate_counts,
            "expected": comparison,
            "passed": acceptable,
        }
        checked.append(record)
        if not acceptable:
            regressions.append(record)
    return {
        "passed": not regressions and not errors,
        "checked": checked,
        "skipped": skipped,
        "regressions": regressions,
        "errors": errors,
    }


def _evaluate_profile_patch(
    patch: dict[str, Any],
    *,
    source_path: str | Path,
    profiles_root: str | Path,
) -> tuple[DrawingProfile, DrawingProfile, dict[str, Any]]:
    profile_path = (
        Path(profiles_root)
        / f"{patch['base_profile_name']}.json"
    )
    if not profile_path.exists():
        raise FileNotFoundError(
            f"base profile not found: {profile_path}"
        )
    base = DrawingProfile.from_dict(
        json.loads(profile_path.read_text(encoding="utf-8"))
    )
    candidate = _apply_changes(base, patch["changes"])
    base_counts = _read_counts(Path(source_path), base)
    candidate_counts = _read_counts(Path(source_path), candidate)
    target = patch["evidence"]["target_counts"]
    base_score = _count_score(base_counts, target)
    candidate_score = _count_score(candidate_counts, target)
    current_ok = candidate_score >= base_score
    current_digest = str(
        patch["evidence"].get("drawing_digest")
        or hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
    )
    replay = _historical_replay(
        base=base,
        candidate=candidate,
        current_digest=current_digest,
        target_counts=target,
    )
    accepted = current_ok and replay["passed"]
    reasons = []
    if not current_ok:
        reasons.append("candidate profile regresses target counts")
    if not replay["passed"]:
        reasons.append("historical replay failed")
    validation = {
        "accepted": accepted,
        "base_counts": base_counts,
        "candidate_counts": candidate_counts,
        "target_counts": target,
        "historical_replay": replay,
        "reasons": reasons,
        "reviewed_at": _utc_now(),
    }
    return base, candidate, validation


def _write_preview(
    patch: dict[str, Any],
    data_root: str | Path,
) -> Path:
    root = _patch_root(
        data_root,
        str(patch["tenant_id"]),
        str(patch["project_id"]),
    )
    path = root / f"{patch['patch_id']}_review.html"
    changes = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(change.get('target')))}</td>"
            f"<td><code>{html.escape(json.dumps(change.get('before'), ensure_ascii=False))}</code></td>"
            f"<td><code>{html.escape(json.dumps(change.get('after'), ensure_ascii=False))}</code></td>"
            f"<td>{html.escape(str(change.get('evidence_count')))}</td>"
            "</tr>"
        )
        for change in patch.get("changes", [])
    )
    validation = patch.get("validation") or {}
    cases = (
        validation.get("historical_replay", {}).get("checked", [])
    )
    case_rows = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(case.get('case_id')))}</td>"
            f"<td><code>{html.escape(json.dumps(case.get('base_counts'), ensure_ascii=False))}</code></td>"
            f"<td><code>{html.escape(json.dumps(case.get('candidate_counts'), ensure_ascii=False))}</code></td>"
            f"<td>{'通过' if case.get('passed') else '退化'}</td>"
            "</tr>"
        )
        for case in cases
    )
    content = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>Stonebot 画像补丁审核</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 32px; color: #172033; }}
h1 {{ margin-bottom: 4px; }}
.badge {{ display:inline-block; padding:4px 10px; border-radius:99px; background:#eef2ff; }}
table {{ border-collapse: collapse; width:100%; margin:12px 0 28px; }}
th, td {{ border-bottom:1px solid #dfe4ea; padding:9px; text-align:left; }}
pre {{ background:#f6f8fa; padding:16px; overflow:auto; }}
.ok {{ color:#087f5b; font-weight:600; }}
.fail {{ color:#c92a2a; font-weight:600; }}
</style>
<h1>Stonebot 画像补丁审核</h1>
<p><span class="badge">{html.escape(str(patch.get('status')))}</span>
 {html.escape(str(patch.get('patch_id')))}</p>
<p>项目：{html.escape(str(patch.get('project_id')))} ·
 基础画像：{html.escape(str(patch.get('base_profile_name')))}</p>
<h2>规则差异</h2>
<table><thead><tr><th>字段</th><th>当前</th><th>候选</th><th>证据数</th></tr></thead>
<tbody>{changes}</tbody></table>
<h2>当前图纸验证</h2>
<p>基础：<code>{html.escape(json.dumps(validation.get('base_counts'), ensure_ascii=False))}</code></p>
<p>候选：<code>{html.escape(json.dumps(validation.get('candidate_counts'), ensure_ascii=False))}</code></p>
<p>修正目标：<code>{html.escape(json.dumps(validation.get('target_counts'), ensure_ascii=False))}</code></p>
<h2>历史案例回放</h2>
<table><thead><tr><th>案例</th><th>基础结果</th><th>候选结果</th><th>结论</th></tr></thead>
<tbody>{case_rows or '<tr><td colspan="4">没有可用历史夹具</td></tr>'}</tbody></table>
<h2>结论</h2>
<p class="{'ok' if validation.get('accepted') else 'fail'}">
{'建议激活' if validation.get('accepted') else '不建议激活'}</p>
<pre>{html.escape(json.dumps(validation.get('reasons', []), ensure_ascii=False, indent=2))}</pre>
</html>"""
    path.write_text(content, encoding="utf-8")
    return path


def review_profile_patch(
    data_root: str | Path,
    *,
    patch_id: str,
    source_path: str | Path,
    profiles_root: str | Path,
) -> dict[str, Any]:
    patch = load_profile_patch(data_root, patch_id)
    if patch.get("status") in {"active", "rejected", "superseded"}:
        raise ValueError(
            f"profile patch is already {patch.get('status')}"
        )
    _base, _candidate, validation = _evaluate_profile_patch(
        patch,
        source_path=source_path,
        profiles_root=profiles_root,
    )
    patch["status"] = (
        "validated" if validation["accepted"] else "candidate"
    )
    patch["validation"] = validation
    validation["preview_path"] = str(
        _write_preview(patch, data_root)
    )
    patch["updated_at"] = _utc_now()
    _save_patch(data_root, patch)
    return patch


def accept_profile_patch(
    data_root: str | Path,
    *,
    patch_id: str,
    source_path: str | Path,
    profiles_root: str | Path,
) -> dict[str, Any]:
    patch = load_profile_patch(data_root, patch_id)
    if patch.get("status") not in {"candidate", "validated"}:
        raise ValueError(
            f"profile patch is already {patch.get('status')}"
        )
    base, candidate, validation = _evaluate_profile_patch(
        patch,
        source_path=source_path,
        profiles_root=profiles_root,
    )
    if not validation["accepted"]:
        validation["preview_path"] = str(
            _write_preview(patch, data_root)
        )
        patch["status"] = "rejected"
        patch["validation"] = validation
        patch["updated_at"] = _utc_now()
        _save_patch(data_root, patch)
        raise ValueError(
            "candidate profile failed validation: "
            + ", ".join(validation["reasons"])
        )

    fingerprint = analyze_drawing(source_path)
    fingerprint.pop("path", None)
    override_root = _override_root(
        data_root,
        str(patch["tenant_id"]),
        str(patch["project_id"]),
    )
    index = _load_override_index(override_root)
    previous_patch_id = (
        index["active"].get(base.name)
    )
    override = {
        "schema_version": PATCH_SCHEMA_VERSION,
        "base_profile_name": base.name,
        "patch_id": patch["patch_id"],
        "status": "active",
        "previous_patch_id": previous_patch_id,
        "profile": asdict(candidate),
        "fingerprint": fingerprint,
        "activated_at": _utc_now(),
    }
    override_root.mkdir(parents=True, exist_ok=True)
    override_path = override_root / f"{patch['patch_id']}.json"
    override_path.write_text(
        json.dumps(override, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    index["active"][base.name] = patch["patch_id"]
    index["history"].append(
        {
            "action": "activate",
            "patch_id": patch["patch_id"],
            "base_profile_name": base.name,
            "previous_patch_id": previous_patch_id,
            "at": _utc_now(),
        }
    )
    _save_override_index(override_root, index)
    patch["status"] = "active"
    validation["preview_path"] = str(
        _write_preview(patch, data_root)
    )
    patch["validation"] = {
        **validation,
        "override_path": str(override_path),
    }
    patch["updated_at"] = _utc_now()
    _save_patch(data_root, patch)
    return patch


def matching_profile_override(
    dxf_path: str | Path,
    override_root: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    root = Path(override_root)
    if not root.exists():
        return None
    fingerprint = analyze_drawing(dxf_path)
    fingerprint.pop("path", None)
    matches = []
    index = _load_override_index(root)
    active_ids = {
        str(patch_id)
        for patch_id in index.get("active", {}).values()
        if patch_id
    }
    paths = [
        root / f"{patch_id}.json"
        for patch_id in sorted(active_ids)
    ]
    if not paths:
        paths = sorted(root.glob("*.json"))
    for path in paths:
        if path.name == "index.json" or not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status") not in {None, "active"}:
                continue
            score = fingerprint_similarity(
                fingerprint,
                payload.get("fingerprint") or {},
            )
        except (OSError, ValueError, TypeError):
            continue
        if score >= OVERRIDE_MATCH_THRESHOLD:
            matches.append((score, payload))
    if not matches:
        return None
    score, payload = max(matches, key=lambda item: item[0])
    meta = {
        "patch_id": payload.get("patch_id"),
        "base_profile_name": payload.get("base_profile_name"),
        "match_score": round(score, 2),
    }
    return dict(payload["profile"]), meta


def list_profile_overrides(
    data_root: str | Path,
    *,
    tenant_id: str,
    project_id: str,
    base_profile_name: str | None = None,
) -> dict[str, Any]:
    root = _override_root(data_root, tenant_id, project_id)
    index = _load_override_index(root)
    active = dict(index.get("active") or {})
    if base_profile_name is not None:
        active = {
            key: value
            for key, value in active.items()
            if key == base_profile_name
        }
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "active": active,
        "history": list(index.get("history") or []),
    }


def rollback_profile_override(
    data_root: str | Path,
    *,
    tenant_id: str,
    project_id: str,
    base_profile_name: str,
    target_patch_id: str | None = None,
) -> dict[str, Any]:
    root = _override_root(data_root, tenant_id, project_id)
    index = _load_override_index(root)
    current_patch_id = index["active"].get(base_profile_name)
    if not current_patch_id:
        raise ValueError(
            f"no active profile override for {base_profile_name}"
        )
    current_path = root / f"{current_patch_id}.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    target = (
        target_patch_id
        if target_patch_id is not None
        else current.get("previous_patch_id")
    )

    current["status"] = "superseded"
    current["superseded_at"] = _utc_now()
    current_path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if target:
        target_path = root / f"{target}.json"
        if not target_path.exists():
            raise FileNotFoundError(
                f"rollback target not found: {target}"
            )
        target_payload = json.loads(
            target_path.read_text(encoding="utf-8")
        )
        if target_payload.get("base_profile_name") != base_profile_name:
            raise ValueError(
                "rollback target belongs to a different base profile"
            )
        target_payload["status"] = "active"
        target_payload["reactivated_at"] = _utc_now()
        target_path.write_text(
            json.dumps(
                target_payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        index["active"][base_profile_name] = target
    else:
        index["active"].pop(base_profile_name, None)

    index["history"].append(
        {
            "action": "rollback",
            "base_profile_name": base_profile_name,
            "from_patch_id": current_patch_id,
            "to_patch_id": target,
            "at": _utc_now(),
        }
    )
    _save_override_index(root, index)
    return {
        "base_profile_name": base_profile_name,
        "active_patch_id": target,
        "rolled_back_from": current_patch_id,
    }


def override_root_for_project(
    data_root: str | Path,
    tenant_id: str,
    project_id: str,
) -> Path:
    return _override_root(data_root, tenant_id, project_id)
