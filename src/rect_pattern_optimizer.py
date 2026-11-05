"""Exact pattern-count rebalancing for fixed-orientation rectangle layouts."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass

import numpy as np

from src.rect_nesting import RectPlacement, _placed_polygon

try:
    import highspy
except ImportError:  # pragma: no cover - optional runtime acceleration
    highspy = None


@dataclass(frozen=True)
class PatternOptimization:
    placements: list[list[RectPlacement]]
    objective_sheets: int
    candidate_patterns: int
    selected_patterns: int


def _dimensions(part) -> tuple[float, float]:
    min_x, min_y, max_x, max_y = part.outer_polygon.bounds
    return round(max_x - min_x, 3), round(max_y - min_y, 3)


def _pattern(sheet) -> tuple[tuple[float, float], ...]:
    return tuple(sorted(_dimensions(placement.part) for placement in sheet))


def _representative(sheet) -> tuple[tuple[float, float, float, float, float], ...]:
    return tuple(
        sorted(
            (
                *_dimensions(placement.part),
                float(placement.x),
                float(placement.y),
                float(placement.rot),
            )
            for placement in sheet
        )
    )


def optimize_pattern_counts(
    parts: list,
    layouts: list,
    *,
    time_limit_s: float | None = 5.0,
) -> PatternOptimization | None:
    """Minimize the number of known sheet patterns needed for exact demand.

    ``layouts`` must contain valid fixed-orientation packings.  Every unique
    dimension multiset becomes one integer column.  The solver only decides
    how many times each existing valid sheet pattern should be used.
    """
    if highspy is None or not parts or not layouts:
        return None

    demand = Counter(_dimensions(part) for part in parts)
    dimensions = sorted(demand)
    dimension_index = {
        dimension: index for index, dimension in enumerate(dimensions)
    }
    representatives: dict[
        tuple[tuple[float, float], ...],
        tuple[tuple[float, float, float, float, float], ...],
    ] = {}
    for layout in layouts:
        for sheet in layout:
            pattern = _pattern(sheet)
            if pattern and pattern not in representatives:
                representatives[pattern] = _representative(sheet)
    patterns = sorted(representatives)
    if not patterns:
        return None

    column_count = len(patterns)
    row_count = len(dimensions)
    matrix = highspy.HighsSparseMatrix()
    matrix.format_ = highspy.MatrixFormat.kColwise
    matrix.num_col_ = column_count
    matrix.num_row_ = row_count
    starts = [0]
    indices: list[int] = []
    values: list[float] = []
    for pattern in patterns:
        counts = Counter(pattern)
        for dimension in dimensions:
            count = counts.get(dimension, 0)
            if count:
                indices.append(dimension_index[dimension])
                values.append(float(count))
        starts.append(len(indices))
    matrix.start_ = np.asarray(starts, dtype=np.int32)
    matrix.index_ = np.asarray(indices, dtype=np.int32)
    matrix.value_ = np.asarray(values, dtype=float)

    problem = highspy.HighsLp()
    problem.num_col_ = column_count
    problem.num_row_ = row_count
    problem.col_cost_ = np.ones(column_count, dtype=float)
    problem.col_lower_ = np.zeros(column_count, dtype=float)
    problem.col_upper_ = np.full(
        column_count,
        highspy.kHighsInf,
        dtype=float,
    )
    demand_values = np.asarray(
        [demand[dimension] for dimension in dimensions],
        dtype=float,
    )
    problem.row_lower_ = demand_values
    problem.row_upper_ = demand_values
    problem.a_matrix_ = matrix
    problem.integrality_ = [
        highspy.HighsVarType.kInteger for _ in range(column_count)
    ]

    solver = highspy.Highs()
    solver.setOptionValue("output_flag", False)
    solver.setOptionValue("threads", 1)
    solver.setOptionValue("random_seed", 0)
    solver.setOptionValue("mip_rel_gap", 0.0)
    if time_limit_s is not None:
        solver.setOptionValue("time_limit", float(time_limit_s))
    status = solver.passModel(problem)
    if status != highspy.HighsStatus.kOk:
        return None
    solver.run()
    if solver.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return None

    solution = solver.getSolution()
    selected = [
        (patterns[index], int(round(value)))
        for index, value in enumerate(solution.col_value)
        if value >= 0.5
    ]
    pools = defaultdict(deque)
    for part in sorted(parts, key=lambda item: (item.number, item.id)):
        pools[_dimensions(part)].append(part)

    placements: list[list[RectPlacement]] = []
    for pattern, count in selected:
        representative = representatives[pattern]
        for _ in range(count):
            sheet: list[RectPlacement] = []
            for width, height, x, y, rotation in representative:
                key = (width, height)
                if not pools[key]:
                    return None
                part = pools[key].popleft()
                sheet.append(
                    RectPlacement(
                        part=part,
                        rot=rotation,
                        x=x,
                        y=y,
                        poly=_placed_polygon(part, rotation, x, y),
                    )
                )
            placements.append(sheet)

    if any(pool for pool in pools.values()):
        return None
    return PatternOptimization(
        placements=placements,
        objective_sheets=int(round(solver.getObjectiveValue())),
        candidate_patterns=column_count,
        selected_patterns=len(selected),
    )
