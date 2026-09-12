"""Profile the bridge nesting + postprocess pipeline on synthetic parts.

This is a diagnostic helper, not a correctness test. It uses rectangles so the
profile reflects the geometry/collision code paths without depending on a
local production DXF. Run from the repository root:

    python -X utf8 scripts/profile_nesting_hotspots.py
"""

from __future__ import annotations

import cProfile
import pstats
import random
import sys
import time
from pathlib import Path

from shapely.geometry import Polygon


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.constraints import PROFILE_MIN_SHEETS
from src.models import Part
from src.nesting import nest_parts
from src.workflow import QUICK_CONFIGS, postprocess_result


def make_parts(count: int = 220) -> list[Part]:
    rng = random.Random(7)
    parts: list[Part] = []
    for index in range(1, count + 1):
        if rng.random() < 0.3:
            width = rng.randint(1800, 3000)
            height = rng.randint(500, 900)
        else:
            width = rng.randint(300, 900)
            height = rng.randint(120, 500)
        polygon = Polygon([(0, 0), (width, 0), (width, height), (0, height)])
        parts.append(
            Part(
                id=index,
                number=f"P-{index:04d}",
                polygon=polygon,
                outer_polygon=polygon,
            )
        )
    return parts


def main() -> int:
    parts = make_parts()
    profile = PROFILE_MIN_SHEETS
    profiler = cProfile.Profile()

    profiler.enable()
    started = time.time()
    result = nest_parts(
        parts,
        3200.0,
        1600.0,
        20.0,
        configs=QUICK_CONFIGS,
        improve_budget=0.0,
        rotations=tuple(profile.rotation),
        min_gap=profile.min_gap,
    )
    postprocess_result(result, profile)
    profiler.disable()

    print(
        f"elapsed={time.time() - started:.3f}s "
        f"sheets={result.total_sheets} parts={result.total_parts}"
    )
    stats = pstats.Stats(profiler)
    stats.sort_stats("cumtime")
    stats.print_stats(25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
