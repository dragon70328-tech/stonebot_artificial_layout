"""Build ``RecognizedDrawing`` contracts from read results.

The read functions currently return mutable ``parts_data`` dictionaries so the
existing CLI can continue to consume them without a large refactor. This module
provides the bridge that turns those dictionaries into the versioned contract
described in ``docs/drawing-intake-subsystem.md``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .contracts import RecognizedDrawing, RecognizedHole, RecognizedPanel


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parts_data_to_recognized_drawing(
    parts_data: list[dict[str, Any]],
    filepath: str | Path,
    *,
    profile_name: str | None = None,
    profile_match_score: float | None = None,
    unit: str = "mm",
    tolerance: float = 0.01,
    closed_tolerance: float = 0.01,
) -> RecognizedDrawing:
    """Convert ``parts_data`` into the standard read-drawing contract.

    This intentionally does not replace the existing ``Part`` objects used by
    the nesting pipeline; it makes the deterministic read result visible and
    auditable as a first step toward the SaaS workflow.
    """
    filepath = Path(filepath)
    source_digest = _sha256(filepath)
    drawing_id = f"drawing_{source_digest[:24]}"

    panels: list[RecognizedPanel] = []
    for part in parts_data:
        part_index = int(part["index"])
        holes: list[RecognizedHole] = []
        for hole_index, hole in enumerate(part.get("holes", [])):
            hole_handles = part.get("hole_handles") or []
            entity_handle = (
                hole_handles[hole_index]
                if hole_index < len(hole_handles)
                else None
            )
            holes.append(
                RecognizedHole(
                    hole_id=f"{drawing_id}:h{part_index}:{hole_index}",
                    geometry_wkt=hole.wkt,
                    layer=part.get("layer"),
                    entity_handle=entity_handle,
                )
            )

        outer = part.get("outer_polygon")
        panels.append(
            RecognizedPanel(
                panel_id=f"{drawing_id}:p{part_index}",
                geometry_wkt=part["polygon"].wkt,
                outer_geometry_wkt=outer.wkt if outer is not None else None,
                layer=part.get("layer"),
                entity_handle=part.get("outer_handle"),
                number=part.get("original_number"),
                holes=holes,
                confidence=float(part.get("confidence", 1.0)),
                properties={
                    "area": float(part.get("area", part["polygon"].area)),
                    "source": part.get("source", "closed_polygon"),
                },
            )
        )

    drawing = RecognizedDrawing(
        drawing_id=drawing_id,
        source_id=f"source_{source_digest[:24]}",
        revision_id=f"revision_{source_digest[:24]}",
        unit=unit,
        tolerance=tolerance,
        closed_tolerance=closed_tolerance,
        profile_name=profile_name,
        profile_match_score=profile_match_score,
        panels=panels,
        input_digest=source_digest,
    )
    drawing.output_digest = drawing.digest()
    return drawing
