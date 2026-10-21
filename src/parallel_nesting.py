# -*- coding: utf-8 -*-
"""并行排板：按材料分组并行 + 同材料分层切批 + 尾部合并。

设计（2026-10-21 实测依据，citizen ST-6 1543 件）：

- 按材料分组并行是**零损失**的：不同材料本来就不能共板；
- 同一材料内切批会多出尾板：2 批 +2 张 / 3 批 +3 张，
  所以切完必须做尾部合并，把各批最低填充的板重新拼回更少的张数；
- 排板是纯 Python 计算，多线程被 GIL 卡住，必须用多进程。
"""

from __future__ import annotations

import multiprocessing as mp
from collections import defaultdict

from src.models import NestingResult


def _class_key(part) -> tuple[float, float]:
    """尺寸档键：长边/短边（0.1mm 精度）。"""
    x0, y0, x1, y1 = part.outer_polygon.bounds
    w, h = x1 - x0, y1 - y0
    return (round(max(w, h), 1), round(min(w, h), 1))


def stratified_split(parts: list, n: int) -> list[list]:
    """按尺寸档轮转切批：每批的规格结构与整单一致（按图纸顺序切会失衡）。"""
    if n <= 1:
        return [list(parts)]
    buckets: dict[tuple, list] = defaultdict(list)
    for part in parts:
        buckets[_class_key(part)].append(part)
    batches: list[list] = [[] for _ in range(n)]
    for key in sorted(buckets):
        for i, part in enumerate(buckets[key]):
            batches[i % n].append(part)
    return [b for b in batches if b]


def probe_multiprocessing() -> tuple[bool, str]:
    """探测本机能否创建进程池（受限环境会禁止命名管道）。

    返回 (是否可用, 原因)。探测失败时**不要**分批：顺序跑多批会比单批更慢、
    张数还更多（citizen 实测 149 对 148）。
    """
    try:
        ctx = mp.get_context("spawn")
        with ctx.Pool(1) as pool:
            pool.map(len, [[0]])
        return True, ""
    except (OSError, PermissionError) as exc:
        return False, type(exc).__name__
    except Exception as exc:  # pragma: no cover - 防御性
        return False, type(exc).__name__


def _nest_worker(payload):
    """进程池任务：排一批零件（顶层函数，Windows spawn 需要）。"""
    parts, kwargs = payload
    from src.nesting import nest_parts

    return nest_parts(parts, **kwargs)


def run_batches(payloads: list, workers: int,
                stats: dict | None = None) -> list[NestingResult]:
    """并行执行各批排板。

    workers<=1 / 只有一批 → 顺序执行；
    多进程不可用（受限环境禁止创建管道、WinError 5）时自动退回顺序执行并
    在 stats 里记录 ``parallel_fallback="sequential"``。
    """
    if workers <= 1 or len(payloads) <= 1:
        if stats is not None:
            stats["parallel_effective_workers"] = 1
        return [_nest_worker(p) for p in payloads]
    workers = min(workers, len(payloads))
    try:
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers) as pool:
            results = pool.map(_nest_worker, payloads)
        if stats is not None:
            stats["parallel_effective_workers"] = workers
        return results
    except (OSError, PermissionError) as exc:
        if stats is not None:
            stats["parallel_effective_workers"] = 1
            stats["parallel_fallback"] = "sequential"
            stats["parallel_fallback_reason"] = type(exc).__name__
        print(f"  多进程不可用（{type(exc).__name__}），退回顺序执行各批",
              flush=True)
        return [_nest_worker(p) for p in payloads]


def build_result(sheets: list, unit: str) -> NestingResult:
    """按板列表重建结果（重排板号）。"""
    for i, sheet in enumerate(sheets, start=1):
        sheet.index = i
    return NestingResult(
        sheets=sheets,
        unit=unit,
        total_parts=sum(len(s.parts) for s in sheets),
        total_sheets=len(sheets),
        total_part_area=sum(s.used_area for s in sheets),
        total_sheet_area=sum(s.total_area for s in sheets),
    )


def merge_tail_sheets(results: list[NestingResult],
                      originals_by_number: dict,
                      unit: str,
                      nest_kwargs: dict) -> tuple[list[NestingResult], int]:
    """尾部合并：每批取最低填充的一张板，重新拼进更少的张数。

    返回 (新的结果列表, 省下的张数)。省不下就原样返回。
    """
    if len(results) < 2:
        return results, 0

    picked = []
    for res in results:
        if not res.sheets:
            return results, 0
        worst = min(res.sheets, key=lambda s: s.used_area / s.total_area)
        picked.append(worst)

    tail_numbers = [p.number for s in picked for p in s.parts]
    tail_parts = [originals_by_number[n] for n in tail_numbers
                  if n in originals_by_number]
    if len(tail_parts) != len(tail_numbers) or not tail_parts:
        return results, 0

    from src.nesting import nest_parts

    kwargs = dict(nest_kwargs)
    kwargs["improve_budget"] = 0.0        # 尾板重排只跑束搜索，秒级
    kwargs["compress"] = False
    kwargs["stats"] = None
    try:
        merged = nest_parts(tail_parts, **kwargs)
    except Exception:
        return results, 0

    saved = len(picked) - len(merged.sheets)
    if saved <= 0:
        return results, 0

    picked_ids = {id(s) for s in picked}
    new_results = []
    for res in results:
        keep = [s for s in res.sheets if id(s) not in picked_ids]
        new_results.append(build_result(keep, res.unit))
    new_results.append(merged)
    return new_results, saved