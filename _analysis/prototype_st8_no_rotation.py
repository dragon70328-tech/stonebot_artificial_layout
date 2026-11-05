"""Prototype fixed-orientation guillotine packing for the Citizen ST-8 case."""

from __future__ import annotations

import math
import random
from pathlib import Path

import ezdxf
from shapely.geometry import Polygon


SOURCE = Path(
    r"C:\Users\drago\Desktop\临时文件\AI识图文件"
    r"\citizen_ST-08_全部2018_numbered_原位.dxf"
)
SHEET_W = 3200.0
SHEET_H = 1600.0


def load_rectangles() -> list[tuple[float, float]]:
    doc = ezdxf.readfile(SOURCE)
    dimensions = []
    for entity in doc.modelspace():
        if (
            entity.dxftype() == "LWPOLYLINE"
            and str(entity.dxf.layer) == "OUTER"
        ):
            polygon = Polygon([
                (float(x), float(y))
                for x, y in entity.get_points("xy")
            ])
            min_x, min_y, max_x, max_y = polygon.bounds
            dimensions.append((max_x - min_x, max_y - min_y))
    return dimensions


def pack(
    dimensions: list[tuple[float, float]],
    *,
    strategy: str,
) -> tuple[int, float]:
    if strategy == "area":
        ordered = sorted(dimensions, key=lambda item: (-item[0] * item[1], -max(item)))
    elif strategy == "width":
        ordered = sorted(dimensions, key=lambda item: (-item[0], -item[1]))
    elif strategy == "height":
        ordered = sorted(dimensions, key=lambda item: (-item[1], -item[0]))
    elif strategy == "maxside":
        ordered = sorted(dimensions, key=lambda item: (-max(item), -item[0] * item[1]))
    else:
        raise ValueError(strategy)

    sheets: list[list[tuple[float, float, float, float]]] = []
    for width, height in ordered:
        best = None
        for sheet_index, free in enumerate(sheets):
            for rect_index, (x, y, free_w, free_h) in enumerate(free):
                if width <= free_w + 1e-9 and height <= free_h + 1e-9:
                    waste = free_w * free_h - width * height
                    key = (waste, y, x, sheet_index, rect_index)
                    if best is None or key < best[0]:
                        best = (key, sheet_index, rect_index, x, y, free_w, free_h)
        if best is None:
            sheets.append([(0.0, 0.0, SHEET_W, SHEET_H)])
            sheet_index = len(sheets) - 1
            rect_index = 0
            x, y, free_w, free_h = 0.0, 0.0, SHEET_W, SHEET_H
        else:
            _, sheet_index, rect_index, x, y, free_w, free_h = best

        free = sheets[sheet_index]
        free.pop(rect_index)
        if free_w - width > 1e-9:
            free.append((x + width, y, free_w - width, height))
        if free_h - height > 1e-9:
            free.append((x, y + height, free_w, free_h - height))
        # Remove rectangles fully covered by a larger free rectangle.
        kept = []
        for index, candidate in enumerate(free):
            if any(
                other != index
                and candidate[0] >= free[other][0] - 1e-9
                and candidate[1] >= free[other][1] - 1e-9
                and candidate[0] + candidate[2]
                <= free[other][0] + free[other][2] + 1e-9
                and candidate[1] + candidate[3]
                <= free[other][1] + free[other][3] + 1e-9
                for other in range(len(free))
            ):
                continue
            kept.append(candidate)
        sheets[sheet_index] = kept
    area = sum(width * height for width, height in dimensions)
    return len(sheets), area / (len(sheets) * SHEET_W * SHEET_H) * 100.0


def pack_maxrects(
    dimensions: list[tuple[float, float]],
    *,
    strategy: str,
    heuristic: str,
    seed: int | None = None,
) -> tuple[int, float]:
    if strategy == "area":
        ordered = sorted(dimensions, key=lambda item: (-item[0] * item[1], -max(item)))
    elif strategy == "width":
        ordered = sorted(dimensions, key=lambda item: (-item[0], -item[1]))
    elif strategy == "height":
        ordered = sorted(dimensions, key=lambda item: (-item[1], -item[0]))
    elif strategy == "random":
        rng = random.Random(seed)
        ordered = list(dimensions)
        rng.shuffle(ordered)
        ordered.sort(
            key=lambda item: -item[0] * item[1] * rng.uniform(0.85, 1.15)
        )
    else:
        ordered = sorted(dimensions, key=lambda item: (-max(item), -item[0] * item[1]))

    sheet_free: list[list[tuple[float, float, float, float]]] = []
    for width, height in ordered:
        best = None
        for sheet_index, free in enumerate(sheet_free):
            for rect_index, (x, y, free_w, free_h) in enumerate(free):
                if width > free_w + 1e-9 or height > free_h + 1e-9:
                    continue
                leftover_w = free_w - width
                leftover_h = free_h - height
                if heuristic == "short":
                    score = (min(leftover_w, leftover_h), max(leftover_w, leftover_h))
                elif heuristic == "area":
                    score = (leftover_w * leftover_h, min(leftover_w, leftover_h))
                else:
                    score = (y + height, x, leftover_w * leftover_h)
                key = (*score, sheet_index, rect_index)
                if best is None or key < best[0]:
                    best = (key, sheet_index, rect_index, x, y)
        if best is None:
            sheet_free.append([(0.0, 0.0, SHEET_W, SHEET_H)])
            sheet_index = len(sheet_free) - 1
            free = sheet_free[sheet_index]
            x, y = 0.0, 0.0
        else:
            _, sheet_index, _, x, y = best
            free = sheet_free[sheet_index]

        updated = []
        placed_right = x + width
        placed_top = y + height
        for fx, fy, fw, fh in free:
            if (
                placed_right <= fx + 1e-9
                or x >= fx + fw - 1e-9
                or placed_top <= fy + 1e-9
                or y >= fy + fh - 1e-9
            ):
                updated.append((fx, fy, fw, fh))
                continue
            if x > fx + 1e-9:
                updated.append((fx, fy, x - fx, fh))
            if placed_right < fx + fw - 1e-9:
                updated.append((placed_right, fy, fx + fw - placed_right, fh))
            if y > fy + 1e-9:
                updated.append((fx, fy, fw, y - fy))
            if placed_top < fy + fh - 1e-9:
                updated.append((fx, placed_top, fw, fy + fh - placed_top))
        kept = []
        for index, candidate in enumerate(updated):
            if any(
                other != index
                and candidate[0] >= updated[other][0] - 1e-9
                and candidate[1] >= updated[other][1] - 1e-9
                and candidate[0] + candidate[2]
                <= updated[other][0] + updated[other][2] + 1e-9
                and candidate[1] + candidate[3]
                <= updated[other][1] + updated[other][3] + 1e-9
                for other in range(len(updated))
            ):
                continue
            kept.append(candidate)
        sheet_free[sheet_index] = kept
    area = sum(width * height for width, height in dimensions)
    return len(sheet_free), area / (len(sheet_free) * SHEET_W * SHEET_H) * 100.0


def main() -> None:
    dimensions = load_rectangles()
    for strategy in ("area", "width", "height", "maxside"):
        sheets, utilization = pack(dimensions, strategy=strategy)
        print(
            f"{strategy}: {sheets} sheets / {utilization:.3f}% "
            f"({len(dimensions)} parts)"
        )
    for strategy in ("area", "width", "height", "maxside"):
        for heuristic in ("short", "area", "bottom"):
            sheets, utilization = pack_maxrects(
                dimensions,
                strategy=strategy,
                heuristic=heuristic,
            )
            print(
                f"maxrects-{strategy}-{heuristic}: {sheets} sheets / "
                f"{utilization:.3f}%"
            )
    best_random = None
    for seed in range(100):
        for heuristic in ("short", "area", "bottom"):
            sheets, utilization = pack_maxrects(
                dimensions,
                strategy="random",
                heuristic=heuristic,
                seed=seed,
            )
            candidate = (sheets, -utilization, seed, heuristic)
            if best_random is None or candidate < best_random:
                best_random = candidate
    print(
        f"maxrects-random-best: {best_random[0]} sheets / "
        f"{-best_random[1]:.3f}% seed={best_random[2]} "
        f"heuristic={best_random[3]}"
    )


if __name__ == "__main__":
    main()
