"""检查"消失的零件"里是否装着别的零件——判断它是不是房间/区域轮廓。

用法：python contains_check.py <a.json> <b.json>

房间轮廓的特征：它内部包着若干规格件；真零件内部通常只有孔洞。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EPS = 1.0  # mm


def main() -> int:
    first = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    second = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    keys_right = {(row["area"], tuple(row["bbox"])) for row in second}
    lost = [
        row
        for row in first
        if (row["area"], tuple(row["bbox"])) not in keys_right
    ]
    print(f"A={len(first)} B={len(second)} 消失={len(lost)}\n")
    for row in sorted(lost, key=lambda item: item["area"]):
        x0, y0, x1, y1 = row["bbox"]
        inside = [
            other
            for other in second
            if other["bbox"][0] >= x0 - EPS
            and other["bbox"][1] >= y0 - EPS
            and other["bbox"][2] <= x1 + EPS
            and other["bbox"][3] <= y1 + EPS
        ]
        width = (x1 - x0) / 1000
        height = (y1 - y0) / 1000
        inner_area = sum(item["area"] for item in inside) / 1e6
        print(
            f"  {row['number']!r:22s} 外包络 {width:.2f}x{height:.2f}m "
            f"面积 {row['area'] / 1e6:.3f}m² → 内含零件 {len(inside)} 件"
            f"（合计 {inner_area:.3f}m²，占其面积 "
            f"{inner_area / (row['area'] / 1e6):.0%}）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
