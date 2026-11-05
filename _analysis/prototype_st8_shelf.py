"""Prototype guillotine shelf recipes for Citizen ST-8 fixed orientation."""

from __future__ import annotations

from collections import Counter, defaultdict

from prototype_st8_no_rotation import SHEET_H, SHEET_W, load_rectangles


def build_rows(
    remaining: list[int],
    classes: list[tuple[float, float]],
    by_height: dict[float, list[int]],
) -> list[tuple[int, ...]]:
    rows: set[tuple[int, ...]] = set()
    for indices in by_height.values():
        # Best-fit decreasing partitions for the whole height group.
        bins: list[tuple[float, list[int]]] = []
        expanded = [
            index
            for index in indices
            for _ in range(remaining[index])
        ]
        for index in sorted(
            expanded,
            key=lambda value: (-classes[value][0], value),
        ):
            width = classes[index][0]
            fits = [
                (SHEET_W - used - width, bin_index)
                for bin_index, (used, _counts) in enumerate(bins)
                if used + width <= SHEET_W + 1e-9
            ]
            if fits:
                _left, bin_index = min(fits)
                bins[bin_index][1][index] += 1
                bins[bin_index] = (
                    bins[bin_index][0] + width,
                    bins[bin_index][1],
                )
            else:
                counts = [0] * len(classes)
                counts[index] = 1
                bins.append((width, counts))
        for _used, counts in bins:
            rows.add(tuple(counts))

        for start in indices:
            width, height = classes[start]
            if remaining[start] <= 0 or width > SHEET_W:
                continue
            counts = [0] * len(classes)
            counts[start] = 1
            used = width
            while True:
                candidates = [
                    index
                    for index in indices
                    if remaining[index] > counts[index]
                    and used + classes[index][0] <= SHEET_W + 1e-9
                ]
                if not candidates:
                    break
                chosen = max(candidates, key=lambda index: (classes[index][0], -index))
                counts[chosen] += 1
                used += classes[chosen][0]
            rows.add(tuple(counts))
    return list(rows)


def best_board(
    remaining: list[int],
    classes: list[tuple[float, float]],
    by_height: dict[float, list[int]],
    *,
    beam_width: int = 120,
) -> tuple[int, ...]:
    rows = build_rows(remaining, classes, by_height)
    row_heights = [
        max(
            (classes[index][1] for index, count in enumerate(row) if count),
            default=0.0,
        )
        for row in rows
    ]
    beam: list[tuple[float, list[int], float]] = [(0.0, [0] * len(classes), 0.0)]
    best = (0.0, [0] * len(classes))
    for _depth in range(8):
        candidates = []
        for used_h, used_counts, area in beam:
            for row, row_h in zip(rows, row_heights):
                if used_h + row_h > SHEET_H + 1e-9:
                    continue
                if any(
                    used_counts[index] + count > remaining[index]
                    for index, count in enumerate(row)
                ):
                    continue
                new_counts = [
                    used_counts[index] + count
                    for index, count in enumerate(row)
                ]
                new_area = area + row_h * sum(
                    count * classes[index][0]
                    for index, count in enumerate(row)
                )
                candidates.append((used_h + row_h, new_counts, new_area))
                if new_area > best[0]:
                    best = (new_area, new_counts)
        if not candidates:
            break
        candidates.sort(key=lambda item: (-item[2], item[0]))
        beam = []
        seen = set()
        for used_h, counts, area in candidates:
            key = (round(used_h, 3), tuple(counts))
            if key in seen:
                continue
            seen.add(key)
            beam.append((used_h, counts, area))
            if len(beam) >= beam_width:
                break
    return tuple(best[1])


def main() -> None:
    dimensions = load_rectangles()
    class_counts = Counter(
        (round(width, 1), round(height, 1))
        for width, height in dimensions
    )
    classes = sorted(class_counts, key=lambda item: (-item[0] * item[1], -item[1]))
    remaining = [class_counts[item] for item in classes]
    by_height: dict[float, list[int]] = defaultdict(list)
    for index, (_width, height) in enumerate(classes):
        by_height[height].append(index)

    sheets = 0
    while any(remaining):
        board = best_board(remaining, classes, by_height)
        if not any(board):
            raise RuntimeError("no board pattern")
        for index, count in enumerate(board):
            remaining[index] -= count
        sheets += 1
        if sheets % 100 == 0:
            print(f"{sheets} sheets, {sum(remaining)} parts remaining")
    area = sum(width * height for width, height in dimensions)
    print(
        f"shelf: {sheets} sheets / "
        f"{area / (sheets * SHEET_W * SHEET_H) * 100:.3f}%"
    )


if __name__ == "__main__":
    main()
