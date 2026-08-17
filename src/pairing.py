"""Optional common-edge pairing pre-pass for repeated identical panels.

This module keeps the original nesting engines unchanged.  When enabled, it
builds 180-degree shared-edge pair modules for congruent outer polygons, then
nests those modules together with leftover singles using the DeepNest-style
bottom-left fill helpers.  First-part-left-edge is preserved by always starting
a sheet with a single panel (splitting a pair when no single is available).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import shapely
from shapely import affinity
from shapely.geometry import Point
from shapely.strtree import STRtree

from src.models import Part, Sheet, NestingResult
from src.deepnest_engine import (
    _first_part_left_placement,
    _find_placement,
    _make_sort_key,
    _compactness,
    _ARBITRARY_ROTATION_STEP,
)
from src.nesting import validate_nesting

EPS = 1e-6


@dataclass
class PairingUnit:
    outer_polygon: object
    area: float
    local_parts: list[Part]
    number: str


def _shape_signature(poly, decimals: int = 3):
    coords = list(poly.exterior.coords)[:-1]
    n = len(coords)
    if n < 3:
        return None

    signed_area = sum(
        coords[i][0] * coords[(i + 1) % n][1]
        - coords[(i + 1) % n][0] * coords[i][1]
        for i in range(n)
    )
    if signed_area < 0:
        coords = coords[::-1]

    pairs = []
    for i in range(n):
        x0, y0 = coords[i]
        x1, y1 = coords[(i + 1) % n]
        x2, y2 = coords[(i + 2) % n]
        edge_len = math.hypot(x1 - x0, y1 - y0)
        a1 = math.atan2(y1 - y0, x1 - x0)
        a2 = math.atan2(y2 - y1, x2 - x1)
        turn = math.degrees((a2 - a1) % (2 * math.pi))
        pairs.append((round(edge_len, decimals), round(turn, decimals)))

    def rotations(seq):
        return [seq[i:] + seq[:i] for i in range(len(seq))]

    candidates = rotations(pairs) + rotations(list(reversed(pairs)))
    return tuple(min(candidates))


def _edges(poly):
    coords = list(poly.exterior.coords)
    return [(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]


def _edge_len(edge):
    (x0, y0), (x1, y1) = edge
    return math.hypot(x1 - x0, y1 - y0)


def _longest_edge(poly):
    return max(_edges(poly), key=_edge_len)


def _edge_midpoint(edge):
    (x0, y0), (x1, y1) = edge
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _single_unit(part: Part) -> PairingUnit:
    return PairingUnit(
        outer_polygon=part.outer_polygon,
        area=part.area,
        local_parts=[part],
        number=part.number,
    )


def _build_pair_unit(a: Part, b: Part) -> PairingUnit | None:
    """Return a 180-degree shared-edge module, or None if the pair is unsafe."""
    edge_a = _longest_edge(a.outer_polygon)
    mid_a = _edge_midpoint(edge_a)
    q_outer = affinity.rotate(a.outer_polygon, 180.0, origin=mid_a)

    # Only pair when the two outer polygons are translations of the same shape.
    delta = (
        b.outer_polygon.centroid.x - a.outer_polygon.centroid.x,
        b.outer_polygon.centroid.y - a.outer_polygon.centroid.y,
    )
    edge_b = _longest_edge(b.outer_polygon)
    mid_b = _edge_midpoint(edge_b)
    test_outer = affinity.translate(
        affinity.rotate(b.outer_polygon, 180.0, origin=mid_b),
        xoff=-delta[0],
        yoff=-delta[1],
    )
    if test_outer.distance(q_outer) > 2.0:
        return None

    module_outer = a.outer_polygon.union(q_outer)

    def local_transform(geom):
        geom = affinity.rotate(geom, 180.0, origin=mid_b)
        return affinity.translate(geom, xoff=-delta[0], yoff=-delta[1])

    label_b = local_transform(Point(b.label_position))
    local_b = Part(
        id=b.id,
        number=b.number,
        polygon=local_transform(b.polygon),
        outer_polygon=q_outer,
        holes=[local_transform(h) for h in b.holes],
        original_number=b.original_number,
        material_group=b.material_group,
        area=b.area,
        label_position=(label_b.x, label_b.y),
    )

    return PairingUnit(
        outer_polygon=module_outer,
        area=a.area + b.area,
        local_parts=[a, local_b],
        number=f"PAIR:{a.number}|{b.number}",
    )


def build_pairing_units(parts: list[Part]) -> list[PairingUnit]:
    clusters = defaultdict(list)
    for part in parts:
        clusters[_shape_signature(part.outer_polygon)].append(part)

    units: list[PairingUnit] = []
    for items in clusters.values():
        pair_count = len(items) // 2
        for i in range(pair_count):
            a = items[2 * i]
            b = items[2 * i + 1]
            unit = _build_pair_unit(a, b)
            if unit is None:
                units.append(_single_unit(a))
                units.append(_single_unit(b))
            else:
                units.append(unit)
        if len(items) % 2:
            units.append(_single_unit(items[-1]))
    return units


def _greedy_pairing_units(
    units: list[PairingUnit],
    sheet_w: float,
    sheet_h: float,
    cache: dict,
    rotations: tuple,
    arbitrary_rotation: bool,
    rotation_step: float,
    first_part_left_edge: bool,
    sort_key,
) -> list:
    remaining = sorted(units, key=sort_key)
    sheets: list[list] = []

    while remaining:
        cur = []
        geoms = []
        tree = None
        nxt = []

        if first_part_left_edge:
            single_idx = next(
                (i for i, u in enumerate(remaining) if len(u.local_parts) == 1),
                None,
            )
            if single_idx is None:
                pair_idx = next(
                    (i for i, u in enumerate(remaining) if len(u.local_parts) > 1),
                    None,
                )
                if pair_idx is None:
                    raise RuntimeError("pairing units exhausted unexpectedly")
                pair = remaining.pop(pair_idx)
                first = _single_unit(pair.local_parts[0])
                remaining.append(_single_unit(pair.local_parts[1]))
            else:
                first = remaining.pop(single_idx)

            placement = _first_part_left_placement(
                first, sheet_w, sheet_h, rotations, arbitrary_rotation
            )
            if placement is None:
                placement = _find_placement(
                    first, geoms, tree, sheet_w, sheet_h,
                    cache, rotations, arbitrary_rotation, rotation_step,
                )
            if placement is None:
                raise RuntimeError(f"first single cannot fit: {first.number}")
            cur.append(placement)
            geoms.append(placement.poly)
            tree = STRtree(geoms)

        for unit in remaining:
            placement = _find_placement(
                unit, geoms, tree, sheet_w, sheet_h,
                cache, rotations, arbitrary_rotation, rotation_step,
            )
            if placement is None:
                nxt.append(unit)
                continue
            cur.append(placement)
            geoms.append(placement.poly)
            tree = STRtree(geoms)

        if not cur:
            numbers = [u.number for u in remaining]
            raise ValueError(f"cannot place pairing units: {numbers}")
        sheets.append(cur)
        remaining = nxt

    return sheets


def _materialize_pairing(
    sheets_pl: list,
    sheet_w: float,
    sheet_h: float,
    thickness: float,
    unit: str,
    total_parts: int,
) -> NestingResult:
    sheets = []
    for index, placements in enumerate(sheets_pl, start=1):
        sheet = Sheet(index=index, width=sheet_w, height=sheet_h,
                      thickness=thickness)
        for pl in placements:
            pairing_unit = pl.part
            origin = pairing_unit.outer_polygon.centroid
            origin_pt = (origin.x, origin.y)
            if pl.rot:
                rotated_unit = affinity.rotate(
                    pairing_unit.outer_polygon, pl.rot, origin=origin_pt
                )
            else:
                rotated_unit = pairing_unit.outer_polygon
            ox = pl.poly.bounds[0] - rotated_unit.bounds[0]
            oy = pl.poly.bounds[1] - rotated_unit.bounds[1]

            def transform(geom):
                geom = (
                    affinity.rotate(geom, pl.rot, origin=origin_pt)
                    if pl.rot else geom
                )
                return affinity.translate(geom, xoff=ox, yoff=oy)

            for local in pairing_unit.local_parts:
                label = transform(Point(local.label_position))
                placed = Part(
                    id=local.id,
                    number=local.number,
                    polygon=transform(local.polygon),
                    outer_polygon=transform(local.outer_polygon),
                    holes=[transform(h) for h in local.holes],
                    original_number=local.original_number,
                    material_group=local.material_group,
                    area=local.area,
                    label_position=(label.x, label.y),
                )
                sheet.parts.append(placed)
        sheets.append(sheet)

    total_part_area = sum(p.area for sheet in sheets for p in sheet.parts)
    total_sheet_area = sum(s.total_area for s in sheets)
    return NestingResult(
        sheets=sheets,
        unit=unit,
        total_parts=total_parts,
        total_sheets=len(sheets),
        total_part_area=total_part_area,
        total_sheet_area=total_sheet_area,
    )


def nest_parts_deepnest_paired(
    parts: list[Part],
    sheet_width: float,
    sheet_height: float,
    sheet_thickness: float,
    unit: str = "metric",
    trials: int = 1,
    seed: int = 0,
    rotations: tuple = (0, 90, 180, 270),
    arbitrary_rotation: bool = False,
    first_part_left_edge: bool = False,
    rotation_step: float = _ARBITRARY_ROTATION_STEP,
    progress=None,
) -> NestingResult:
    if not parts:
        raise ValueError("paired DeepNest nesting requires at least one part")

    units = build_pairing_units(parts)
    cache: dict = {}
    order_names = ["area", "short", "long", "jitter"]
    best_pl = None
    best_key = None

    for trial in range(max(1, trials)):
        name = order_names[trial % len(order_names)]
        sort_key = _make_sort_key(name, seed + trial)
        sheets_pl = _greedy_pairing_units(
            units, sheet_width, sheet_height, cache,
            rotations, arbitrary_rotation, rotation_step,
            first_part_left_edge, sort_key,
        )
        key = (len(sheets_pl), _compactness(sheets_pl))
        if best_key is None or key < best_key:
            best_key = key
            best_pl = sheets_pl

    result = _materialize_pairing(
        best_pl, sheet_width, sheet_height, sheet_thickness, unit, len(parts)
    )
    errors = validate_nesting(result, sheet_width, sheet_height)
    if errors:
        raise RuntimeError("paired nesting validation failed: " + "; ".join(errors))
    return result
