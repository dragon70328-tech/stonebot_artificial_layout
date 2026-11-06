"""Circular-saw clearance calculations and local access diagnostics.

The saw body is a vertical disc.  At a plunging depth ``H`` below the
table plane, the half chord of the disc at that plane is the sideways
space needed by the blade body:

    clearance = sqrt(diameter * H - H * H)

This clearance is distinct from kerf and sheet oversize.  It is only a
secondary manufacturability objective: sheet count remains the primary
nesting objective.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable

from shapely.geometry import Point, Polygon


EPSILON = 1e-7


@dataclass(frozen=True)
class SawClearance:
    diameter_mm: float
    thickness_mm: float
    plunge_extra_mm: float
    safety_margin_mm: float
    plunge_depth_mm: float
    theoretical_clearance_mm: float
    clearance_mm: float

    def as_dict(self) -> dict[str, float]:
        return {
            "diameter_mm": _round(self.diameter_mm),
            "thickness_mm": _round(self.thickness_mm),
            "plunge_extra_mm": _round(self.plunge_extra_mm),
            "safety_margin_mm": _round(self.safety_margin_mm),
            "plunge_depth_mm": _round(self.plunge_depth_mm),
            "theoretical_clearance_mm": _round(
                self.theoretical_clearance_mm
            ),
            "clearance_mm": _round(self.clearance_mm),
        }


@dataclass
class SawAccessSummary:
    required_clearance_mm: float
    min_reflex_turn_degrees: float = 60.0
    points_checked: int = 0
    points_compliant: int = 0
    points_insufficient: int = 0
    total_deficit_mm: float = 0.0
    min_clearance_mm: float | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def all_compliant(self) -> bool:
        return self.points_insufficient == 0

    @property
    def ranking_key(self) -> tuple[float, float, float, float]:
        """Lower is better; used only after total sheet count is tied."""
        minimum_score = (
            -float(round(self.min_clearance_mm, 6))
            if self.min_clearance_mm is not None
            else 0.0
        )
        return (
            float(self.points_insufficient),
            float(round(self.total_deficit_mm, 6)),
            float(self.points_checked),
            minimum_score,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "required_clearance_mm": _round(self.required_clearance_mm),
            "min_reflex_turn_degrees": _round(
                self.min_reflex_turn_degrees
            ),
            "points_checked": self.points_checked,
            "points_compliant": self.points_compliant,
            "points_insufficient": self.points_insufficient,
            "all_compliant": self.all_compliant,
            "total_deficit_mm": _round(self.total_deficit_mm),
            "min_clearance_mm": (
                _round(self.min_clearance_mm)
                if self.min_clearance_mm is not None
                else None
            ),
            "warnings": self.warnings,
            "policy": (
                "secondary_after_minimum_sheet_count; "
                "never_increases_sheet_count_automatically"
            ),
        }


def calculate_saw_clearance(
    diameter_mm: float,
    thickness_mm: float,
    *,
    plunge_extra_mm: float = 2.0,
    safety_margin_mm: float = 0.0,
) -> SawClearance:
    """Return the half-chord blade-body clearance at the required depth."""
    diameter = float(diameter_mm)
    thickness = float(thickness_mm)
    extra = float(plunge_extra_mm)
    safety = float(safety_margin_mm)
    if diameter <= 0:
        raise ValueError("circular saw diameter must be greater than 0")
    if thickness <= 0:
        raise ValueError("sheet thickness must be greater than 0")
    if extra < 0:
        raise ValueError("plunge extra must be non-negative")
    if safety < 0:
        raise ValueError("saw safety margin must be non-negative")

    plunge_depth = thickness + extra
    radicand = diameter * plunge_depth - plunge_depth * plunge_depth
    if radicand <= 0:
        raise ValueError(
            "circular saw diameter must exceed the plunge depth "
            f"({diameter:g} <= {plunge_depth:g} mm)"
        )
    theoretical = math.sqrt(radicand)
    return SawClearance(
        diameter_mm=diameter,
        thickness_mm=thickness,
        plunge_extra_mm=extra,
        safety_margin_mm=safety,
        plunge_depth_mm=plunge_depth,
        theoretical_clearance_mm=theoretical,
        clearance_mm=theoretical + safety,
    )


def diagnose_saw_access(
    sheets: Iterable[Any],
    required_clearance_mm: float,
    *,
    min_reflex_turn_degrees: float = 60.0,
    max_warnings: int = 50,
) -> SawAccessSummary:
    """Check local space at outer-contour reflex vertices.

    A pronounced reflex outer vertex is an internal turn/plunge point.  The
    checker measures from that point to every other panel on the same sheet
    and reports neighbours closer than the calculated half-chord.  This is a
    conservative local sweep test for the operational risk: cutting the current
    panel must not cut a neighbouring panel.  The panel's own non-adjacent
    edges are excluded because they are the shape being cut, not a neighbour.
    Sheet edges are allowed entrances/exits.

    Only pronounced reflex turns (default 60 degrees or more) are checked;
    small turns introduced by flattening an ARC are ignored.  This is a
    conservative local diagnostic, not a full blade-path or machine-clamp
    simulation.  Rectangular parts therefore have no checked points.
    """
    required = float(required_clearance_mm)
    if required < 0:
        raise ValueError("required saw clearance must be non-negative")
    if not 0.0 <= min_reflex_turn_degrees <= 180.0:
        raise ValueError(
            "minimum reflex turn must be between 0 and 180 degrees"
        )

    summary = SawAccessSummary(
        required_clearance_mm=required,
        min_reflex_turn_degrees=float(min_reflex_turn_degrees),
    )
    for fallback_index, sheet in enumerate(sheets, start=1):
        sheet_index = (
            int(getattr(sheet, "index", fallback_index) or fallback_index)
            if hasattr(sheet, "parts")
            else fallback_index
        )
        items = list(_sheet_items(sheet))
        polygons = [item[1] for item in items]
        for item_index, (label, poly) in enumerate(items):
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
            coords = _ring_coords(poly.exterior.coords)
            if len(coords) < 4:
                continue
            clockwise = _signed_area(coords) < 0

            for vertex_index in range(len(coords)):
                previous_index = (vertex_index - 1) % len(coords)
                next_index = (vertex_index + 1) % len(coords)
                previous = Point(coords[previous_index])
                current = Point(coords[vertex_index])
                following = Point(coords[next_index])
                if not _is_reflex(
                    previous,
                    current,
                    following,
                    clockwise,
                    min_reflex_turn_degrees,
                ):
                    continue

                nearest = min(
                    (
                        (float(current.distance(other)), other_index)
                        for other_index, other in enumerate(polygons)
                        if other_index != item_index and not other.is_empty
                    ),
                    default=(required, None),
                )
                actual, collision_index = nearest
                summary.points_checked += 1
                if summary.min_clearance_mm is None:
                    summary.min_clearance_mm = actual
                else:
                    summary.min_clearance_mm = min(
                        summary.min_clearance_mm,
                        actual,
                    )
                if actual + EPSILON >= required:
                    summary.points_compliant += 1
                    continue

                deficit = max(0.0, required - actual)
                summary.points_insufficient += 1
                summary.total_deficit_mm += deficit
                if len(summary.warnings) < max_warnings:
                    summary.warnings.append({
                        "type": "saw_clearance_insufficient",
                        "sheet": sheet_index,
                        "part": label,
                        "neighbor_part": _part_label(
                            items,
                            int(collision_index),
                        ),
                        "vertex_index": vertex_index,
                        "x_mm": _round(current.x),
                        "y_mm": _round(current.y),
                        "required_clearance_mm": _round(required),
                        "actual_clearance_mm": _round(actual),
                        "deficit_mm": _round(deficit),
                    })
    return summary


def saw_ranking_key(
    sheets: Iterable[Any],
    required_clearance_mm: float,
) -> tuple[float, float, float, float]:
    """Compact ranking key for nesting candidates."""
    return diagnose_saw_access(
        sheets,
        required_clearance_mm,
    ).ranking_key


def _sheet_items(sheet: Any) -> Iterable[tuple[str, Polygon]]:
    if hasattr(sheet, "parts"):
        for part in sheet.parts:
            poly = getattr(part, "outer_polygon", None)
            if poly is not None:
                yield str(getattr(part, "number", "?")), poly
        return

    # Nesting-internal placement lists use placement.poly.
    placements = getattr(sheet, "placements", None)
    if placements is None and not hasattr(sheet, "parts"):
        placements = sheet
    for index, placement in enumerate(placements or [], start=1):
        poly = getattr(placement, "poly", None)
        if poly is None:
            poly = getattr(placement, "outer_polygon", None)
        if poly is None:
            continue
        part = getattr(placement, "part", None)
        label = str(getattr(part, "number", f"placement-{index}"))
        yield label, poly


def _ring_coords(coords: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    points = [(float(x), float(y)) for x, y in coords]
    if len(points) > 1 and _same_point(points[0], points[-1]):
        points.pop()
    return points


def _is_reflex(
    previous: Point,
    current: Point,
    following: Point,
    clockwise: bool,
    min_turn_degrees: float,
) -> bool:
    ax = current.x - previous.x
    ay = current.y - previous.y
    bx = following.x - current.x
    by = following.y - current.y
    cross = ax * by - ay * bx
    length = math.hypot(ax, ay) * math.hypot(bx, by)
    if length <= EPSILON:
        return False
    normalized = cross / length
    turn_degrees = math.degrees(
        math.atan2(
            abs(cross),
            ax * bx + ay * by,
        )
    )
    if turn_degrees < min_turn_degrees:
        return False
    return normalized > EPSILON if clockwise else normalized < -EPSILON


def _signed_area(coords: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        coords[index][0] * coords[(index + 1) % len(coords)][1]
        - coords[(index + 1) % len(coords)][0] * coords[index][1]
        for index in range(len(coords))
    )


def _same_point(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    return (
        abs(first[0] - second[0]) <= EPSILON
        and abs(first[1] - second[1]) <= EPSILON
    )


def _part_label(items: list[tuple[str, Polygon]], index: int) -> str:
    if 0 <= index < len(items):
        return items[index][0]
    return f"part-{index + 1}"


def _round(value: float) -> float:
    return round(float(value), 3)
