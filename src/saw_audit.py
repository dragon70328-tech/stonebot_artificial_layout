"""Audit an existing nested DXF for circular-saw neighbour collisions."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import ezdxf

from src.layout_adjustment import _read_nested_layout
from src.saw_process import calculate_saw_clearance, diagnose_saw_access


def audit_nested_saw_clearance(
    nested_dxf: str | Path,
    *,
    diameter_mm: float,
    thickness_mm: float,
    plunge_extra_mm: float = 2.0,
    safety_margin_mm: float = 0.0,
) -> dict[str, Any]:
    """Return a structured saw-collision audit for a nested DXF."""
    path = Path(nested_dxf).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    clearance = calculate_saw_clearance(
        diameter_mm,
        thickness_mm,
        plunge_extra_mm=plunge_extra_mm,
        safety_margin_mm=safety_margin_mm,
    )
    sheets, _groups = _read_nested_layout(ezdxf.readfile(str(path)))

    warnings = []
    per_sheet = []
    total_checked = 0
    total_insufficient = 0
    total_deficit = 0.0
    all_minima = []
    for sheet in sheets:
        summary = diagnose_saw_access(
            [sheet],
            clearance.clearance_mm,
            max_warnings=10000,
        )
        total_checked += summary.points_checked
        total_insufficient += summary.points_insufficient
        total_deficit += summary.total_deficit_mm
        if summary.min_clearance_mm is not None:
            all_minima.append(summary.min_clearance_mm)
        warnings.extend(summary.warnings)
        per_sheet.append({
            "sheet": sheet.index,
            "part_count": len(sheet.parts),
            "points_checked": summary.points_checked,
            "points_insufficient": summary.points_insufficient,
            "min_clearance_mm": (
                round(summary.min_clearance_mm, 3)
                if summary.min_clearance_mm is not None
                else None
            ),
            "deficit_mm": round(summary.total_deficit_mm, 3),
        })

    pair_counts = Counter(
        (
            warning["sheet"],
            warning["part"],
            warning["neighbor_part"],
        )
        for warning in warnings
    )
    issue_pairs = [
        {
            "sheet": sheet,
            "part": part,
            "neighbor_part": neighbor,
            "occurrences": count,
        }
        for (sheet, part, neighbor), count in sorted(pair_counts.items())
    ]
    return {
        "ok": total_insufficient == 0,
        "safe": total_insufficient == 0,
        "nested_dxf": str(path),
        "sheet_count": len(sheets),
        "part_count": sum(len(sheet.parts) for sheet in sheets),
        "circular_saw": clearance.as_dict(),
        "points_checked": total_checked,
        "points_insufficient": total_insufficient,
        "total_deficit_mm": round(total_deficit, 3),
        "min_clearance_mm": (
            round(min(all_minima), 3) if all_minima else None
        ),
        "issue_pairs": issue_pairs,
        "warnings": warnings,
        "per_sheet": per_sheet,
        "policy": (
            "conservative local check at pronounced outer reflex cut points; "
            "sheet-edge entry/exit is exempt; this is not a full CAM simulation"
        ),
    }
