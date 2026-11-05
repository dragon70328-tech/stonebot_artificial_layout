"""矩形快通道：尺寸档 + 行/列模式（guillotine）+ 束搜索。

适用：零件外轮廓近似矩形（bbox 填充率 ≥ :data:`RECT_FILL_RATIO`）。绝大多数
台面/地面下单图属于这一类——它们不需要多边形束搜索。每张板按"横带"或"竖列"
切分，带/列内按尺寸档排列，版式天然可桥切。

流程：

1. 按外包络分尺寸档；允许 90° 时生成行/列板型，禁止 90° 时使用固定朝向
   MaxRects 多策略候选；
2. 每张板形成可桥切的行列板型，或省料优先的固定朝向二维候选；
3. 在"各档剩余数量"上做束搜索，每步选一张板，按（板数, 剩余面积）择优；
4. 模式落成坐标（``RectPlacement``，字段与 ``nesting._Placement`` 相同）。

非矩形件或要求非零间距时返回 ``None``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from shapely import affinity
from shapely.geometry import Polygon

RECT_FILL_RATIO = 0.98


@dataclass
class RectPlacement:
    """与 ``nesting._Placement`` 字段一致的放置记录（鸭子类型兼容）。"""

    part: object
    rot: float
    x: float
    y: float
    poly: Polygon


def is_rect_like(part) -> bool:
    outer = part.outer_polygon
    if outer.is_empty:
        return False
    min_x, min_y, max_x, max_y = outer.bounds
    box_area = (max_x - min_x) * (max_y - min_y)
    if box_area <= 0:
        return False
    return outer.area / box_area >= RECT_FILL_RATIO


def _dims(part) -> tuple[float, float, bool]:
    min_x, min_y, max_x, max_y = part.outer_polygon.bounds
    width, height = max_x - min_x, max_y - min_y
    long_side, short_side = max(width, height), min(width, height)
    return long_side, short_side, width >= height


def rectangles_are_guillotine(
    rectangles: Iterable[tuple[float, float, float, float]],
) -> bool:
    """Return whether straight full cuts can separate every rectangle."""
    normalized = tuple(
        sorted(
            (
                round(float(x), 6),
                round(float(y), 6),
                round(float(width), 6),
                round(float(height), 6),
            )
            for x, y, width, height in rectangles
            if width > 0 and height > 0
        )
    )

    def slices(items: tuple[tuple[float, float, float, float], ...]) -> bool:
        if len(items) <= 1:
            return True
        min_x = min(item[0] for item in items)
        min_y = min(item[1] for item in items)
        max_x = max(item[0] + item[2] for item in items)
        max_y = max(item[1] + item[3] for item in items)
        for cut in sorted({item[0] for item in items} | {
            item[0] + item[2] for item in items
        }):
            if cut <= min_x + 1e-9 or cut >= max_x - 1e-9:
                continue
            if any(item[0] < cut < item[0] + item[2] for item in items):
                continue
            left = tuple(
                item for item in items
                if item[0] + item[2] <= cut + 1e-9
            )
            right = tuple(
                item for item in items
                if item[0] >= cut - 1e-9
            )
            if left and right and slices(left) and slices(right):
                return True
        for cut in sorted({item[1] for item in items} | {
            item[1] + item[3] for item in items
        }):
            if cut <= min_y + 1e-9 or cut >= max_y - 1e-9:
                continue
            if any(item[1] < cut < item[1] + item[3] for item in items):
                continue
            bottom = tuple(
                item for item in items
                if item[1] + item[3] <= cut + 1e-9
            )
            top = tuple(
                item for item in items
                if item[1] >= cut - 1e-9
            )
            if bottom and top and slices(bottom) and slices(top):
                return True
        return False

    return slices(normalized)


def _fixed_orientation_candidate(
    parts: list,
    sheet_w: float,
    sheet_h: float,
    *,
    strategy: str,
    heuristic: str,
) -> list[list[RectPlacement]] | None:
    dimensions = []
    for index, part in enumerate(parts):
        min_x, min_y, max_x, max_y = part.outer_polygon.bounds
        width = max_x - min_x
        height = max_y - min_y
        if width > sheet_w + 1e-9 or height > sheet_h + 1e-9:
            return None
        dimensions.append((index, width, height))

    if strategy == "area":
        ordered = sorted(
            dimensions,
            key=lambda item: (-item[1] * item[2], -max(item[1], item[2])),
        )
    elif strategy == "width":
        ordered = sorted(
            dimensions,
            key=lambda item: (-item[1], -item[2]),
        )
    elif strategy == "height":
        ordered = sorted(
            dimensions,
            key=lambda item: (-item[2], -item[1]),
        )
    else:
        ordered = sorted(
            dimensions,
            key=lambda item: (-max(item[1], item[2]), -item[1] * item[2]),
        )

    sheet_free: list[list[tuple[float, float, float, float]]] = []
    sheet_parts: list[list[RectPlacement]] = []
    for part_index, width, height in ordered:
        best = None
        for sheet_index, free_rectangles in enumerate(sheet_free):
            for rect_index, (x, y, free_w, free_h) in enumerate(free_rectangles):
                if width > free_w + 1e-9 or height > free_h + 1e-9:
                    continue
                leftover_w = free_w - width
                leftover_h = free_h - height
                if heuristic == "short":
                    position_score = (
                        min(leftover_w, leftover_h),
                        max(leftover_w, leftover_h),
                    )
                elif heuristic == "area":
                    position_score = (
                        leftover_w * leftover_h,
                        min(leftover_w, leftover_h),
                    )
                else:
                    position_score = (
                        y + height,
                        x,
                        leftover_w * leftover_h,
                    )
                key = (*position_score, sheet_index, rect_index)
                if best is None or key < best[0]:
                    best = (key, sheet_index, x, y, free_rectangles)
        if best is None:
            sheet_free.append([(0.0, 0.0, sheet_w, sheet_h)])
            sheet_parts.append([])
            sheet_index = len(sheet_free) - 1
            x, y = 0.0, 0.0
            free_rectangles = sheet_free[sheet_index]
        else:
            _, sheet_index, x, y, free_rectangles = best

        part = parts[part_index]
        sheet_parts[sheet_index].append(
            RectPlacement(
                part=part,
                rot=0.0,
                x=x,
                y=y,
                poly=_placed_polygon(part, 0.0, x, y),
            )
        )
        placed_right = x + width
        placed_top = y + height
        updated = []
        for fx, fy, free_w, free_h in free_rectangles:
            if (
                placed_right <= fx + 1e-9
                or x >= fx + free_w - 1e-9
                or placed_top <= fy + 1e-9
                or y >= fy + free_h - 1e-9
            ):
                updated.append((fx, fy, free_w, free_h))
                continue
            if x > fx + 1e-9:
                updated.append((fx, fy, x - fx, free_h))
            if placed_right < fx + free_w - 1e-9:
                updated.append(
                    (placed_right, fy, fx + free_w - placed_right, free_h)
                )
            if y > fy + 1e-9:
                updated.append((fx, fy, free_w, y - fy))
            if placed_top < fy + free_h - 1e-9:
                updated.append(
                    (fx, placed_top, free_w, fy + free_h - placed_top)
                )
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
    return sheet_parts


def rect_fast_layout_fixed_orientation(
    parts: list,
    sheet_w: float,
    sheet_h: float,
) -> list[list[RectPlacement]] | None:
    """Optimize material usage while preserving every source orientation."""
    candidates = []
    for strategy in ("area", "width", "height", "maxside"):
        for heuristic in ("short", "area", "bottom"):
            candidate = _fixed_orientation_candidate(
                parts,
                sheet_w,
                sheet_h,
                strategy=strategy,
                heuristic=heuristic,
            )
            if candidate is not None:
                candidates.append(candidate)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda layout: (
            len(layout),
            -sum(
                placement.poly.area
                for sheet in layout
                for placement in sheet
            ),
            sum(len(sheet) for sheet in layout),
        ),
    )


def _line_fills(lengths: list[float], limit: float) -> list[tuple[int, ...]]:
    results: list[tuple[int, ...]] = []

    def walk(index: int, used: float, counts: list[int]):
        if index == len(lengths):
            results.append(tuple(counts))
            return
        length = lengths[index]
        max_n = int((limit - used) // length + 1e-9) if length > 0 else 0
        for n in range(max_n + 1):
            counts.append(n)
            walk(index + 1, used + n * length, counts)
            counts.pop()

    walk(0, 0.0, [])
    return results


def _board_patterns(classes: list[tuple[float, float]],
                    sheet_w: float, sheet_h: float,
                    rotations: tuple) -> list[tuple[tuple[int, ...], tuple]]:
    """返回 [(counts, placements)]，placements 元素为 (class, mode, x, y)。"""
    patterns: dict[tuple[int, ...], tuple] = {}
    longs = [long_side for long_side, _short in classes]
    shorts = [short_side for _long, short_side in classes]
    can_rotate = 90 in rotations or 90.0 in rotations

    # 行模式：横带高度 = 该带内最大短边；带内按长边从左到右
    row_height = max(shorts) if shorts else 0.0
    if row_height > 0 and can_rotate:
        for row_counts in _line_fills(longs, sheet_w):
            height = max((shorts[i] for i, n in enumerate(row_counts) if n), default=0.0)
            if height <= 0:
                continue
            rows = int(sheet_h // height + 1e-9)
            for repeat in range(1, rows + 1):
                counts = tuple(n * repeat for n in row_counts)
                placements = []
                for row in range(repeat):
                    y = row * height
                    x = 0.0
                    for index, n in enumerate(row_counts):
                        for _ in range(n):
                            placements.append((index, "row", x, y))
                            x += longs[index]
                patterns.setdefault(counts, tuple(placements))

    # 列模式：列宽 = 短边；列内同类堆叠（长边竖放）
    if can_rotate:
        col_units = []
        for index, (long_side, short_side) in enumerate(classes):
            stack = int(sheet_h // long_side + 1e-9)
            if stack > 0:
                col_units.append((index, short_side, stack))

        def combine(index: int, used: float, counts: list[int], placements: list):
            if index == len(col_units):
                patterns.setdefault(tuple(counts), tuple(placements))
                return
            cls, width, stack = col_units[index]
            repeats = int((sheet_w - used) // width + 1e-9)
            for repeat in range(repeats + 1):
                added = []
                for column in range(repeat):
                    x = used + column * width
                    for step in range(stack):
                        added.append((cls, "col", x, step * classes[cls][0]))
                counts[cls] += stack * repeat
                placements.extend(added)
                combine(index + 1, used + repeat * width, counts, placements)
                if added:
                    del placements[-len(added):]
                counts[cls] -= stack * repeat

        if col_units:
            combine(0, 0.0, [0] * len(classes), [])

    return [(counts, placements) for counts, placements in patterns.items() if any(counts)]


def _placed_polygon(part, rot: float, x: float, y: float) -> Polygon:
    outer = part.outer_polygon
    origin = (outer.centroid.x, outer.centroid.y)
    rotated = affinity.rotate(outer, rot, origin=origin) if rot else outer
    return affinity.translate(
        rotated,
        xoff=x - rotated.bounds[0],
        yoff=y - rotated.bounds[1],
    )


def rect_fast_layout(parts: list, sheet_w: float, sheet_h: float, *,
                     rotations: tuple = (0, 90, 180, 270),
                     min_gap: float = 0.0,
                     beam_width: int = 120,
                     max_patterns: int = 400) -> list[list[RectPlacement]] | None:
    """返回 ``list[list[RectPlacement]]``；不适用时 ``None``。"""
    if not parts or min_gap > 0:
        return None
    if not all(is_rect_like(part) for part in parts):
        return None
    can_rotate = any(
        abs(float(angle) % 360.0 - 90.0) <= 1e-9
        or abs(float(angle) % 360.0 - 270.0) <= 1e-9
        for angle in rotations
    )
    if not can_rotate:
        return rect_fast_layout_fixed_orientation(
            parts,
            sheet_w,
            sheet_h,
        )

    groups: dict[tuple[float, float], list] = {}
    for part in parts:
        long_side, short_side, _wide = _dims(part)
        key = (round(long_side, 1), round(short_side, 1))
        groups.setdefault(key, []).append(part)
    keys = sorted(groups, key=lambda key: -key[0] * key[1])
    classes = list(keys)
    demand = tuple(len(groups[key]) for key in keys)
    areas = [key[0] * key[1] for key in keys]
    if max(key[1] for key in keys) > sheet_h + 1e-9:
        return None
    if max(key[0] for key in keys) > max(sheet_w, sheet_h) + 1e-9:
        return None

    patterns = _board_patterns(classes, sheet_w, sheet_h, rotations)
    if not patterns:
        return None
    patterns.sort(key=lambda item: -sum(n * a for n, a in zip(item[0], areas)))
    patterns = patterns[:max_patterns]

    board_area = sheet_w * sheet_h
    beam = [(0, demand, ())]
    final = None
    for _depth in range(2000):
        candidates = []
        for sheets_used, remaining, trail in beam:
            if not any(remaining):
                final = (sheets_used, remaining, trail)
                break
            for counts, placements in patterns:
                take = tuple(min(n, r) for n, r in zip(counts, remaining))
                if not any(take):
                    continue
                candidates.append(
                    (
                        sheets_used + 1,
                        tuple(r - t for r, t in zip(remaining, take)),
                        trail + ((counts, placements, take),),
                    )
                )
        if final is not None:
            break
        if not candidates:
            return None
        candidates.sort(key=lambda item: (
            item[0] + sum(r * a for r, a in zip(item[1], areas)) / board_area * 0.999,
            -sum((d - r) * a for d, r, a in zip(demand, item[1], areas)),
        ))
        if not any(candidates[0][1]):
            final = candidates[0]
            break
        seen = set()
        beam = []
        for item in candidates:
            if item[1] in seen:
                continue
            seen.add(item[1])
            beam.append(item)
            if len(beam) >= beam_width:
                break
    if final is None:
        return None

    queues = {index: list(groups[key]) for index, key in enumerate(keys)}
    sheets: list[list[RectPlacement]] = []
    for _counts, placements, take in final[2]:
        budget = list(take)
        sheet: list[RectPlacement] = []
        for cls, mode, x, y in placements:
            if budget[cls] <= 0:
                continue
            part = queues[cls].pop()
            budget[cls] -= 1
            _long, _short, wide = _dims(part)
            if mode == "row":
                rot = 0.0 if wide else 90.0
            else:
                rot = 90.0 if wide else 0.0
            sheet.append(
                RectPlacement(
                    part=part,
                    rot=rot,
                    x=x,
                    y=y,
                    poly=_placed_polygon(part, rot, x, y),
                )
            )
        if sheet:
            sheets.append(sheet)
    if any(queues.values()):
        return None
    return sheets
