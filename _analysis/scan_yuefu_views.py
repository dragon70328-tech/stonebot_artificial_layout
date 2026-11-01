"""逐视图扫描「悦府顶层43-44层」：封闭区域 + 尺寸 + 线型 + 文字。"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf import bbox
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union


SOURCE = Path(r"C:\Users\drago\Desktop\临时文件\AI识图文件\悦府顶层43-44层.dxf")
REPORT = Path(__file__).resolve().parent / "yuefu_views_report.txt"


def main() -> None:
    doc = ezdxf.readfile(str(SOURCE))
    msp = doc.modelspace()

    titles = []
    for entity in msp:
        if entity.dxftype() not in {"TEXT", "MTEXT"}:
            continue
        text = str(
            entity.dxf.text if entity.dxftype() == "TEXT" else entity.text or ""
        )
        if any(key in text for key in ("立面", "墙面", "背景墙")):
            titles.append((entity.dxf.insert.x, entity.dxf.insert.y, text))

    upper = sorted([t for t in titles if t[1] > 0], key=lambda t: t[0])
    lower = sorted([t for t in titles if t[1] <= 0], key=lambda t: t[0])

    lines = [
        LineString(
            [
                (entity.dxf.start.x, entity.dxf.start.y),
                (entity.dxf.end.x, entity.dxf.end.y),
            ]
        )
        for entity in msp
        if entity.dxftype() == "LINE"
    ]

    output: list[str] = []

    def scan(label, x0, y0, x1, y1):
        output.append("=" * 78)
        output.append(f"{label}  box=({x0:.0f},{y0:.0f})-({x1:.0f},{y1:.0f})")

        region_lines = [
            line for line in lines
            if x0 <= line.bounds[0] and line.bounds[2] <= x1
            and y0 <= line.bounds[1] and line.bounds[3] <= y1
        ]
        polygons = []
        if region_lines:
            merged = unary_union(region_lines)
            polygons = sorted(
                (
                    poly for poly in polygonize(merged)
                    if poly.area >= 15_000
                ),
                key=lambda poly: -poly.area,
            )
        output.append(f"封闭区域 {len(polygons)}:")
        for poly in polygons:
            bx = poly.bounds
            output.append(
                f"  {bx[2]-bx[0]:7.0f} x {bx[3]-bx[1]:6.0f}"
                f"  at ({bx[0]:8.0f},{bx[1]:8.0f})"
                f"  area {poly.area/1e6:6.3f} m2"
            )

        dims = []
        for entity in msp:
            if entity.dxftype() != "DIMENSION":
                continue
            try:
                p1, p2 = entity.dxf.defpoint2, entity.dxf.defpoint3
                measure = entity.get_measurement()
            except Exception:
                continue
            if (
                x0 <= min(p1.x, p2.x) and max(p1.x, p2.x) <= x1
                and y0 <= min(p1.y, p2.y) and max(p1.y, p2.y) <= y1
            ):
                dims.append(
                    (
                        round(measure, 1),
                        str(entity.dxf.get("text", "")),
                    )
                )
        output.append(f"尺寸 {len(dims)}:")
        output.append("  " + ", ".join(
            f"{m:g}{('[' + t + ']') if t else ''}" for m, t in dims
        ))

        texts = []
        for entity in msp:
            if entity.dxftype() not in {"TEXT", "MTEXT"}:
                continue
            x, y = entity.dxf.insert.x, entity.dxf.insert.y
            if x0 <= x <= x1 and y0 <= y <= y1:
                value = str(
                    entity.dxf.text
                    if entity.dxftype() == "TEXT"
                    else entity.text or ""
                )
                texts.append(value.replace("\n", " ")[:60])
        output.append("文字: " + " | ".join(texts))

        linetypes: dict[str, int] = {}
        for entity in msp:
            if entity.dxftype() not in {"LINE", "LWPOLYLINE", "ARC", "CIRCLE"}:
                continue
            try:
                box = bbox.extents([entity])
            except Exception:
                continue
            if (
                x0 <= box.extmin.x and box.extmax.x <= x1
                and y0 <= box.extmin.y and box.extmax.y <= y1
            ):
                name = str(getattr(entity.dxf, "linetype", "") or "")
                if name.upper() not in {"BYLAYER", "CONTINUOUS", ""}:
                    linetypes[name] = linetypes.get(name, 0) + 1
        output.append(f"非实线: {linetypes}")

    for index, (x, y, label) in enumerate(upper):
        next_x = upper[index + 1][0] if index + 1 < len(upper) else x + 5000
        scan(label, x - 400, y - 100, (x + next_x) / 2 - 100, y + 4600)
    for index, (x, y, label) in enumerate(lower):
        next_x = lower[index + 1][0] if index + 1 < len(lower) else x + 5000
        scan(label, x - 400, y - 100, (x + next_x) / 2 - 100, min(y + 4300, 1600))

    REPORT.write_text("\n".join(output), encoding="utf-8")
    print(f"written {REPORT} ({len(output)} lines)")


if __name__ == "__main__":
    main()
