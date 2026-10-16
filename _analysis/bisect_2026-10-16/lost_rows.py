"""把 "A 有而 B 没有" 的零件行写成 JSON，供 gap_probe.py 反查实体。

用法：python lost_rows.py <a.json> <b.json> <out.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    first = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    second = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    keys = {(row["area"], tuple(row["bbox"])) for row in second}
    lost = [
        row for row in first if (row["area"], tuple(row["bbox"])) not in keys
    ]
    Path(sys.argv[3]).write_text(
        json.dumps(lost, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"消失零件 {len(lost)} 件 -> {sys.argv[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
