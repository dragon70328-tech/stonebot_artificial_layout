# -*- coding: utf-8 -*-
"""检查本机排板多进程是否可用（并行分批的前提）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parallel_nesting import probe_multiprocessing


def main() -> int:
    ok, reason = probe_multiprocessing()
    cpus = os.cpu_count() or 1
    print(f"CPU 逻辑核: {cpus}")
    if ok:
        print("多进程: 可用 —— 可以用 --parallel-workers 3 做分批并行")
        return 0
    print(f"多进程: 不可用（{reason}）—— 请按单批排板")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())