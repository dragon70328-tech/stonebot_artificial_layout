"""探测：把回看的搜索预算放大，"清空一张板"能不能找到？

用于区分两种可能：
  A) 49 张几何上可行，只是当前预算（donor 数 / 节点数 / 每件候选数）搜不到；
  B) 49 张在几何上不存在，需要换更强的机制（跨板重组 / 级联挪件）。

用法：
    python -X utf8 _analysis/rea_youshi/probe_review_budget.py <nested.dxf> [min_gap]
"""

import os
import sys
import time

# 允许直接从 _analysis/ 下运行（脚本目录不等于仓库根）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from src.constraints import NestingProfile
from src.nesting_reviewer import find_reductions, load_nested_dxf


def main():
    dxf = sys.argv[1]
    min_gap = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    result = load_nested_dxf(dxf)
    print(f"输入：{dxf}")
    print(f"板数={result.total_sheets} 零件={result.total_parts} "
          f"出材率={result.yield_rate:.2f}%")
    profile = NestingProfile(rotation=[0, 90, 180, 270], min_gap=min_gap)

    # (donor 上限, 单 donor 节点上限, 每件候选上限, donor 最大零件数)
    plans = [
        (8, 3000, 40, 6),        # 当前默认
        (24, 20000, 40, 6),
        (24, 20000, 200, 6),
        (60, 60000, 200, 12),
        (120, 200000, 400, 20),  # 近似"尽力而为"
    ]
    for attempts, nodes, plimit, pmax in plans:
        t0 = time.time()
        cands, reduced = find_reductions(
            result,
            profile=profile,
            max_reductions=1,
            max_donor_attempts=attempts,
            max_nodes_per_donor=nodes,
            placement_limit=plimit,
            max_donor_parts=pmax,
            min_gap=min_gap,
        )
        dt = time.time() - t0
        found = "找到可并板" if cands else "未找到"
        detail = (f" donor=sheet{cands[0].removed_sheet_index}"
                  f" parts={cands[0].moved_parts}" if cands else "")
        print(f"  donors={attempts:<4} nodes={nodes:<7} plimit={plimit:<4} "
              f"pmax={pmax:<3} -> {found}{detail}  "
              f"{reduced.total_sheets} 张 / {dt:.1f}s")


if __name__ == "__main__":
    main()
