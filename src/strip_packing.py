"""精确窄条排板：统一短边窄条的竖列/横排精确装箱。

工作坐标系为"放大空间"：零件尺寸与大板尺寸均已按锯缝放大（由调用方在
边界处完成放大/缩小），本模块按零间隙相邻放置。数学上等价于"相邻件之间
保留固定锯缝"。

模式：
- 竖列（column）：列宽 = 窄条主导短边，列容量 = 板高；件在列内自下而上堆叠。
- 横排（shelf）：排高 = 主导短边，排容量 = 板宽；件在排内自左而右排列。

列装箱使用精确一维装箱（模式枚举 + DFS set-cover + 记忆化），
在尺寸种数不多（<=15）时给出最小列数；枚举规模超限自动回退 BFD。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache

from shapely.geometry import box

from src.models import Part, Sheet

_MAX_DFS_STATES = 300_000
_MAX_PATTERNS = 100_000


def _bfd_bins(
    size_counts: list[tuple[float, int]], capacity: float
) -> list[tuple[float, dict[float, int]]]:
    """Best-Fit-Decreasing 贪心装箱，返回 [(fill, {size: count})]。"""
    items: list[float] = []
    for size, count in size_counts:
        items.extend([size] * count)
    items.sort(reverse=True)
    bins: list[list] = []
    for size in items:
        best_index = -1
        best_free = None
        for index, entry in enumerate(bins):
            free = capacity - entry[0]
            if size <= free + 1e-9 and (best_free is None or free - size < best_free):
                best_free = free - size
                best_index = index
        if best_index < 0:
            bins.append([size, Counter({size: 1})])
        else:
            bins[best_index][0] += size
            bins[best_index][1][size] += 1
    return [(fill, dict(counts)) for fill, counts in bins]


def _enumerate_patterns(
    sizes: list[float], capacity: float
) -> list[tuple[float, tuple[int, ...]]]:
    """枚举所有可行的列填充模式（各尺寸件数向量），按填充量降序。"""
    patterns: list[tuple[float, tuple[int, ...]]] = []
    n = len(sizes)
    vec = [0] * n

    def rec(index: int, used: float) -> None:
        if len(patterns) > _MAX_PATTERNS:
            return
        if index == n:
            if used > 0:
                patterns.append((used, tuple(vec)))
            return
        max_k = int((capacity - used + 1e-9) // sizes[index])
        for k in range(max_k + 1):
            vec[index] = k
            rec(index + 1, used + k * sizes[index])

    rec(0, 0.0)
    patterns.sort(key=lambda pattern: -pattern[0])
    return patterns


@lru_cache(maxsize=4096)
def _exact_bin_pack_impl(
    counts_items: tuple[tuple[float, int], ...],
    capacity: float,
) -> tuple[list[tuple[float, dict[float, int]]], bool]:
    if not counts_items:
        return [], True
    ordered_items = sorted(counts_items, key=lambda item: item[0], reverse=True)
    sizes = [size for size, _ in ordered_items]
    counts_tuple = tuple(count for _, count in ordered_items)
    total = sum(size * count for size, count in ordered_items)
    lower_bound = -(-total // capacity)
    greedy = _bfd_bins(list(zip(sizes, counts_tuple)), capacity)
    if len(greedy) == lower_bound:
        return greedy, True

    patterns = _enumerate_patterns(sizes, capacity)
    if len(patterns) > _MAX_PATTERNS:
        return greedy, False
    patterns_by_first: dict[int, list[tuple[tuple[int, ...], float]]] = defaultdict(list)
    for fill, vec in patterns:
        first = next(i for i, k in enumerate(vec) if k)
        patterns_by_first[first].append((vec, fill))

    states = [0]
    size_count = len(sizes)

    def try_pack(limit: int):
        failed: set[tuple[int, ...]] = set()
        chosen: list[tuple[tuple[int, ...], float]] = []

        def dfs(rem: tuple[int, ...], rem_sum: float) -> bool:
            if states[0] > _MAX_DFS_STATES:
                return False
            if rem_sum <= 1e-9:
                return True
            if len(chosen) >= limit:
                return False
            if len(chosen) + -(-rem_sum // capacity) > limit:
                return False
            if rem in failed:
                return False
            states[0] += 1
            first = next(i for i, k in enumerate(rem) if k)
            for vec, fill in patterns_by_first[first]:
                next_rem: list[int] = []
                for index, used in enumerate(vec):
                    remaining_count = rem[index] - used
                    if remaining_count < 0:
                        break
                    next_rem.append(remaining_count)
                else:
                    chosen.append((vec, fill))
                    if dfs(tuple(next_rem), rem_sum - fill):
                        return True
                    chosen.pop()
            failed.add(rem)
            return False

        if dfs(counts_tuple, total):
            result = []
            for vec, fill in chosen:
                result.append(
                    (fill, {sizes[i]: vec[i] for i in range(size_count) if vec[i]})
                )
            return result
        return None

    for limit in range(int(lower_bound), len(greedy)):
        result = try_pack(limit)
        if result is not None:
            return result, True
    return greedy, False


def exact_bin_pack(
    counts: dict[float, int], capacity: float
) -> tuple[list[tuple[float, dict[float, int]]], bool]:
    """精确一维装箱：求最小 bin 数。

    counts: {尺寸: 件数}；capacity: 列/排容量（均已含锯缝放大）。
    返回 (bins, exact)。bins 为 [(fill, {size: count})]；exact=False 表示
    搜索超限回退到 BFD 结果（不保证最小）。
    """
    counts_items = tuple(
        sorted(
            (float(size), int(count))
            for size, count in counts.items()
            if count > 0
        )
    )
    return _exact_bin_pack_impl(counts_items, float(capacity))


def _item_width(item) -> float:
    return min(item.length_mm, item.width_mm)


def _item_length(item) -> float:
    return max(item.length_mm, item.width_mm)


def _extract_big_lengths(
    counts: dict[float, int], capacity: float, absorb_limit: float
) -> tuple[list[tuple[float, dict[float, int]]], dict[float, int]]:
    """把 >capacity/2 的长度剥离为种子列（每列至多一件），缩小精确搜索空间。

    种子列余隙按"最大可放件优先"吸收小件，但吸收后填充不超过 absorb_limit
    （通常为最小板容量），避免把灵活列变成只能上大关的列。
    返回 (seed_bins, rest_counts)。
    """
    bigs = sorted((s for s in counts if s > capacity / 2), reverse=True)
    rest = dict(counts)
    seeds: list[list] = []
    for size in bigs:
        for _ in range(rest.pop(size)):
            seeds.append([size, Counter({size: 1})])
    if seeds:
        smalls = sorted(rest, reverse=True)
        for seed in seeds:
            for size in smalls:
                while (
                    rest.get(size, 0) > 0
                    and seed[0] + size <= absorb_limit + 1e-9
                ):
                    seed[1][size] += 1
                    rest[size] -= 1
                    seed[0] += size
    rest = {size: count for size, count in rest.items() if count > 0}
    return [(fill, dict(cnt)) for fill, cnt in seeds], rest


def _build_shelf_layout(
    longs: list,
    wide: list,
    pool: list,
    sheet_size: tuple[float, float],
    col_width: float,
    fill_rows: bool,
):
    """在一张板上布置横排：宽条独占一排，长条一排并可带 riders，
    fill_rows=True 时剩余排用 BFD 填满短条。
    返回 (layout, remaining_pool)；layout = [(x, y, w, h, item)]。
    排数放不下时返回 None。"""
    sheet_w, sheet_h = sheet_size
    n_rows = int(sheet_h // col_width)
    rows: list[list] = []
    for item in wide:
        rows.append([item])
    pool_sorted = sorted(pool, key=lambda it: -_item_length(it))
    for item in longs:
        row = [item]
        fill = _item_length(item)
        while True:
            rider = next(
                (p for p in pool_sorted if _item_length(p) <= sheet_w - fill + 1e-9),
                None,
            )
            if rider is None:
                break
            row.append(rider)
            fill += _item_length(rider)
            pool_sorted.remove(rider)
        rows.append(row)
    if len(rows) > n_rows:
        return None
    remaining = pool_sorted
    if fill_rows:
        free_rows = n_rows - len(rows)
        if free_rows > 0 and remaining:
            counts = Counter(round(_item_length(it), 3) for it in remaining)
            size_counts = sorted(counts.items(), reverse=True)
            bins = _bfd_bins(size_counts, sheet_w)
            bins.sort(key=lambda entry: -entry[0])
            for fill, cnt in bins[:free_rows]:
                row_items = []
                for length, count in cnt.items():
                    for _ in range(count):
                        picked = next(
                            p
                            for p in remaining
                            if round(_item_length(p), 3) == length
                        )
                        row_items.append(picked)
                        remaining.remove(picked)
                rows.append(row_items)
    layout = []
    y = 0.0
    for row in rows:
        x = 0.0
        for it in row:
            length = _item_length(it)
            width = _item_width(it)
            layout.append((x, y, length, width, it))
            x += length
        y += col_width
    return layout, remaining


def _assign_columns(
    bins: list[tuple[float, dict[float, int]]],
    sheet_sizes: list[tuple[float, float]],
    col_width: float,
):
    """把列分配到两种（或一种）大板，最小化 (总面积, 板数, 大板数)。

    返回 (n_large, n_small) 或 None。sheet_sizes 按面积升序。"""
    cols_per_sheet = [int(w // col_width) for w, _ in sheet_sizes]
    capacities = [h for _, h in sheet_sizes]
    areas = [w * h for w, h in sheet_sizes]
    total_bins = len(bins)
    if len(sheet_sizes) == 1:
        if all(fill <= capacities[0] + 1e-6 for fill, _ in bins):
            return (0, -(-total_bins // cols_per_sheet[0]))
        return None
    large_only = sum(1 for fill, _ in bins if fill > capacities[0] + 1e-6)
    flexible = total_bins - large_only
    min_large = -(-large_only // cols_per_sheet[1])
    best = None
    for n_large in range(min_large, min_large + 6):
        slots_large = n_large * cols_per_sheet[1]
        flex_in_large = max(0, slots_large - large_only)
        flex_remaining = max(0, flexible - flex_in_large)
        n_small = -(-flex_remaining // cols_per_sheet[0])
        candidate = (
            n_large * areas[1] + n_small * areas[0],
            n_large + n_small,
            n_large,
            n_small,
        )
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return None
    return (best[2], best[3])


def _make_part(item, part_id: int, x: float, y: float, w: float, h: float) -> Part:
    polygon = box(x, y, x + w, y + h)
    return Part(
        id=part_id,
        number=f"{item.material or 'P'}-{part_id:04d}",
        polygon=polygon,
        outer_polygon=polygon,
        material_group=item.material or None,
        area=_item_width(item) * _item_length(item),
        label_position=(x + w / 2.0, y + h / 2.0),
    )


def pack_residual_strips(
    strips: list,
    sheet_sizes: list[tuple[float, float]],
    next_id: int,
    thickness_mm: float,
) -> tuple[list[Sheet], int]:
    """剩余窄条精确排板（竖列精确装箱 + 横排兜底）。

    strips: ListItem 列表（qty=1，尺寸已放大）；sheet_sizes: 放大后大板尺寸。
    返回 (sheets, next_id)，Sheet/Part 均处于放大坐标系。
    """
    if not strips:
        return [], next_id
    width_counts = Counter(round(_item_width(it), 3) for it in strips)
    col_width = max(width_counts, key=lambda w: (width_counts[w], -w))
    max_h = max(h for _, h in sheet_sizes)
    max_w = max(w for w, _ in sheet_sizes)

    pool: list = []
    longs: list = []
    wide: list = []
    for item in strips:
        width = _item_width(item)
        length = _item_length(item)
        if width > col_width + 1e-6:
            wide.append(item)
        elif length > max_h + 1e-6:
            longs.append(item)
        else:
            pool.append(item)
    for item in wide + longs:
        if _item_length(item) > max_w + 1e-6:
            raise ValueError(
                f"窄条 {_item_length(item):.0f}x{_item_width(item):.0f} 超出任何大板"
            )

    types = sorted(sheet_sizes, key=lambda size: size[0] * size[1])
    shelf_configs: list[tuple] = []
    if longs or wide:
        for size in types:
            for fill_rows in (False, True):
                shelf_configs.append((size, fill_rows))
    else:
        shelf_configs.append((None, False))

    best_solution = None
    for shelf_size, fill_rows in shelf_configs:
        shelf_layout: list = []
        pool_after = list(pool)
        if shelf_size is not None:
            built = _build_shelf_layout(
                longs, wide, pool_after, shelf_size, col_width, fill_rows
            )
            if built is None:
                continue
            shelf_layout, pool_after = built
        for cap_size in types:
            counts = Counter(round(_item_length(it), 3) for it in pool_after)
            best_for_cap = None
            seed_bins, rest_counts = _extract_big_lengths(
                counts, cap_size[1], types[0][1]
            )
            exact_bins, _ = exact_bin_pack(rest_counts, cap_size[1])
            candidate_bins = [seed_bins + exact_bins]
            if seed_bins:
                raw_bins, _ = exact_bin_pack(counts, cap_size[1])
                candidate_bins.append(raw_bins)
            for bins in candidate_bins:
                assignment = _assign_columns(bins, types, col_width)
                if assignment is None:
                    continue
                n_l, n_s = assignment
                assign_area = n_s * types[0][0] * types[0][1]
                if len(types) > 1:
                    assign_area += n_l * types[1][0] * types[1][1]
                assign_key = (assign_area, n_l + n_s, len(bins))
                if best_for_cap is None or assign_key < best_for_cap[0]:
                    best_for_cap = (assign_key, bins, assignment)
            if best_for_cap is None:
                continue
            _, bins, assignment = best_for_cap
            n_large, n_small = assignment
            area = n_small * types[0][0] * types[0][1]
            if len(types) > 1:
                area += n_large * types[1][0] * types[1][1]
            if shelf_size is not None:
                area += shelf_size[0] * shelf_size[1]
            sheets_count = n_large + n_small + (1 if shelf_size is not None else 0)
            key = (area, sheets_count, n_large)
            if best_solution is None or key < best_solution[0]:
                best_solution = (
                    key,
                    shelf_size,
                    shelf_layout,
                    pool_after,
                    bins,
                    (n_large, n_small),
                )

    if best_solution is None:
        raise ValueError("剩余窄条无法在给定大板中排布")

    _, shelf_size, shelf_layout, pool_after, bins, (n_large, n_small) = best_solution

    by_length: dict[float, list] = defaultdict(list)
    for item in pool_after:
        by_length[round(_item_length(item), 3)].append(item)

    sheets: list[Sheet] = []
    cols_per_type = {size: int(size[0] // col_width) for size in types}

    def emit_sheet(size, columns):
        nonlocal next_id
        parts: list[Part] = []
        for col_index, (_, counts) in enumerate(columns):
            x = col_index * col_width
            y = 0.0
            for length in sorted(counts, reverse=True):
                for _ in range(counts[length]):
                    item = by_length[round(length, 3)].pop()
                    width = _item_width(item)
                    parts.append(_make_part(item, next_id, x, y, width, length))
                    next_id += 1
                    y += length
        sheets.append(
            Sheet(
                index=0,
                width=size[0],
                height=size[1],
                thickness=thickness_mm,
                parts=parts,
            )
        )

    # 大板先放只能上大板的列（fill 超小板容量），再用灵活列填满大板余槽
    if n_large > 0:
        large_size = types[1]
        small_cap = types[0][1]
        large_only_bins = [b for b in bins if b[0] > small_cap + 1e-6]
        flexible_bins = sorted(
            (b for b in bins if b[0] <= small_cap + 1e-6), key=lambda b: -b[0]
        )
        slots = n_large * cols_per_type[large_size]
        large_columns = large_only_bins + flexible_bins[: slots - len(large_only_bins)]
        flexible_bins = flexible_bins[slots - len(large_only_bins) :]
        per_sheet = cols_per_type[large_size]
        for start in range(0, len(large_columns), per_sheet):
            emit_sheet(large_size, large_columns[start : start + per_sheet])
        remaining_bins = flexible_bins
    else:
        remaining_bins = sorted(bins, key=lambda b: -b[0])
    if n_small > 0:
        small_size = types[0]
        per_sheet = cols_per_type[small_size]
        for start in range(0, len(remaining_bins), per_sheet):
            emit_sheet(small_size, remaining_bins[start : start + per_sheet])

    if shelf_size is not None:
        parts = []
        for x, y, w, h, item in shelf_layout:
            parts.append(_make_part(item, next_id, x, y, w, h))
            next_id += 1
        sheets.append(
            Sheet(
                index=0,
                width=shelf_size[0],
                height=shelf_size[1],
                thickness=thickness_mm,
                parts=parts,
            )
        )
    return sheets, next_id
