"""比较两份零件清单（dump_panels.py 的产物），列出各自独有的零件。

用法：python diff_panels.py <a.json> <b.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _key(row: dict) -> tuple:
    return (row["area"], tuple(row["bbox"]))


def main() -> int:
    first = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    second = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    left = {_key(row): row for row in first}
    right = {_key(row): row for row in second}

    only_left = [row for key, row in left.items() if key not in right]
    only_right = [row for key, row in right.items() if key not in left]
    print(f"A={len(first)}  B={len(second)}  仅 A 有={len(only_left)}  仅 B 有={len(only_right)}")
    for label, rows in (("仅 A 有", only_left), ("仅 B 有", only_right)):
        for row in sorted(rows, key=lambda item: item["area"]):
            print(
                f"  [{label}] 面积={row['area'] / 1e6:.3f}m² "
                f"编号={row['number']!r} 孔={row['holes']} "
                f"bbox={[round(value) for value in row['bbox']]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
