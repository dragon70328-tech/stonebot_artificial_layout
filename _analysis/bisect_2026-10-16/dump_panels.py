"""导出某代码树下某画像的零件清单（面积/图层/编号），用于比较两版读图差异。

用法：

    python dump_panels.py <代码树根> <画像名> <源图> <输出 json>
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def _geometry_of(part):
    """兼容两代契约：新契约给 WKT，老契约直接给 shapely 多边形。"""
    if "geometry_wkt" in part:
        from shapely import wkt  # noqa: PLC0415

        return wkt.loads(part["geometry_wkt"])
    for key in ("outer_polygon", "polygon", "outer_geometry"):
        if key in part:
            return part[key]
    raise KeyError(f"未知的零件几何字段：{sorted(part)}")


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    name, source, out_path = sys.argv[2], sys.argv[3], Path(sys.argv[4])
    sys.path.insert(0, str(root))

    from src.drawing_profile import (  # noqa: PLC0415
        load_profile,
        read_dxf_with_profile,
    )

    profile = load_profile(root / "drawing_profiles" / f"{name}.json")
    parts, _doc = read_dxf_with_profile(source, profile)
    rows = []
    for part in parts:
        geometry = _geometry_of(part)
        rows.append(
            {
                "area": round(geometry.area, 1),
                "bbox": [round(value, 1) for value in geometry.bounds],
                "layer": part.get("layer"),
                "number": part.get("original_number"),
                "holes": len(part.get("holes") or []),
            }
        )
    rows.sort(key=lambda row: (row["area"], row["bbox"]))
    out_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    areas = sorted(row["area"] for row in rows)
    print(f"{name}: {len(rows)} panels -> {out_path.name}")
    print("  图层分布:", dict(Counter(row["layer"] for row in rows)))
    print(
        "  面积 m²: min={:.2f} p25={:.2f} 中位={:.2f} p75={:.2f} max={:.2f}".format(
            areas[0] / 1e6,
            areas[len(areas) // 4] / 1e6,
            areas[len(areas) // 2] / 1e6,
            areas[3 * len(areas) // 4] / 1e6,
            areas[-1] / 1e6,
        )
    )
    print(f"  有编号: {sum(1 for row in rows if row['number'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
