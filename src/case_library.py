"""Validated drawing case library.

Each validated case records a drawing fingerprint, the profile it used, the
expected read results, and any audit decisions that can be safely reused for
similar drawings. New drawings are matched by fingerprint similarity before
falling back to the generic drawing-profile ranker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .drawing_profile import DrawingProfile, load_profile


@dataclass
class CaseRecord:
    case_id: str
    profile_name: str
    fingerprint: dict[str, Any]
    expected_counts: dict[str, int] = field(default_factory=dict)
    audit_ignore_rules: list[dict[str, Any]] = field(default_factory=list)
    exclude_entity_handles: list[str] = field(default_factory=list)
    source_sha256: str | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseRecord":
        return cls(
            case_id=data["case_id"],
            profile_name=data["profile_name"],
            fingerprint=data.get("fingerprint", {}),
            expected_counts=data.get("expected_counts", {}),
            audit_ignore_rules=data.get("audit_ignore_rules", []),
            exclude_entity_handles=data.get("exclude_entity_handles", []),
            source_sha256=data.get("source_sha256"),
            notes=data.get("notes", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "profile_name": self.profile_name,
            "fingerprint": self.fingerprint,
            "expected_counts": self.expected_counts,
            "audit_ignore_rules": self.audit_ignore_rules,
            "exclude_entity_handles": self.exclude_entity_handles,
            "source_sha256": self.source_sha256,
            "notes": self.notes,
        }


def load_case(path: str | Path) -> CaseRecord:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return CaseRecord.from_dict(data)


def load_cases(directory: str | Path) -> list[CaseRecord]:
    directory = Path(directory)
    if not directory.exists():
        return []
    return [load_case(path) for path in sorted(directory.glob("*.json"))]


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _set_from_mapping(mapping: dict[str, Any] | None) -> set[str]:
    return {str(key) for key in (mapping or {})}


def case_similarity(fingerprint: dict[str, Any], case: CaseRecord) -> float:
    """Return a 0-100 similarity score between two drawing fingerprints."""
    case_fingerprint = case.fingerprint

    layer_score = _jaccard(
        _set_from_mapping(fingerprint.get("layer_counts")),
        _set_from_mapping(case_fingerprint.get("layer_counts")),
    )
    entity_score = _jaccard(
        _set_from_mapping(fingerprint.get("entity_counts")),
        _set_from_mapping(case_fingerprint.get("entity_counts")),
    )
    linetype_score = _jaccard(
        _set_from_mapping(fingerprint.get("linetype_counts")),
        _set_from_mapping(case_fingerprint.get("linetype_counts")),
    )

    score = layer_score * 50.0
    score += entity_score * 20.0
    score += linetype_score * 15.0

    for key in ("has_circle", "has_lwpolyline", "has_line"):
        if fingerprint.get(key) == case_fingerprint.get(key):
            score += 5.0

    if fingerprint.get("dxf_version") == case_fingerprint.get("dxf_version"):
        score += 5.0
    if fingerprint.get("insunits") == case_fingerprint.get("insunits"):
        score += 5.0

    return min(100.0, score)


def match_case(
    fingerprint: dict[str, Any],
    cases: list[CaseRecord],
    min_score: float = 75.0,
) -> tuple[CaseRecord, float] | None:
    ranked = sorted(
        ((case, case_similarity(fingerprint, case)) for case in cases),
        key=lambda item: item[1],
        reverse=True,
    )
    if not ranked or ranked[0][1] < min_score:
        return None
    return ranked[0]


def overlay_case_onto_profile(
    profile: DrawingProfile,
    case: CaseRecord,
) -> DrawingProfile:
    """Return a profile enriched with validated case decisions."""
    from dataclasses import replace

    merged_rules = list(profile.audit_ignore_rules)
    for rule in case.audit_ignore_rules:
        if rule not in merged_rules:
            merged_rules.append(rule)

    merged_handles = list(profile.exclude_entity_handles)
    for handle in case.exclude_entity_handles:
        if handle not in merged_handles:
            merged_handles.append(handle)

    return replace(
        profile,
        expected_counts=case.expected_counts or profile.expected_counts,
        audit_ignore_rules=merged_rules,
        exclude_entity_handles=merged_handles,
    )


def load_case_profile(
    case: CaseRecord,
    profiles: list[DrawingProfile],
) -> DrawingProfile | None:
    for profile in profiles:
        if profile.name == case.profile_name:
            return overlay_case_onto_profile(profile, case)
    return None
