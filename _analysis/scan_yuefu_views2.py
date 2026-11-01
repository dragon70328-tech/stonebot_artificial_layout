"""逐立面精确扫描（显式视图边界）。"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf import bbox
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union


SOURCE = Path(r"C:\Users\drago\Desktop\临时文件\AI识图文件\悦府顶层43-44层.dxf")
REPORT = Path(__file__).resolve().parent / "yuefu_views_report2.txt"

VIEWS = [
    ("43楼客用卫生间 A立面", 29062, 1800, 32527, 6000),
    ("43楼客用卫生间 B立面", 33094, 1800, 36720, 6000),
    ("43楼客用卫生间 B立面-右剖/放大", 37099, 1800, 40122, 6000),
    ("43楼长辈房卫生间 A立面", 42139, 1800, 45422, 6000),
    ("43楼长辈房卫生间 B立面", 46778, 1800, 50599, 6000),
    ("43楼长辈房卫生间 B立面-右剖", 51075, 1800, 51861, 6000),
    ("44楼女孩房卫生间 A立面", 57963, -1000, 60528, 5000),
    ("44楼女孩房卫生间 B立面", 61097, -1000, 65548, 5000),
    ("44楼书房卫生间 A立面", 70315, 1800, 73576, 6000),
    ("44楼书房卫生间 B立面", 74341, 1800, 81227, 6000),
    ("44楼主人房卫生间 A立面", 107083, 2200, 110137, 7000),
    ("44楼主人房卫生间 B立面", 117204, 2200, 120575, 7000),
    ("43楼 过道造型墙面", 17483, -3000, 23098, 1600),
    ("43楼客用卫生间 C立面", 29062, -3000, 35555, 1600),
    ("43楼客用卫生间 C立面-右剖/放大", 35555, -3000, 38603, 1600),
    ("43楼长辈房卫生间 C立面", 42139, -3000, 50511, 1600),
    ("43楼长辈房卫生间 C立面-右侧", 51188, -3000, 53583, 1600),
    ("44楼书房卫生间 C立面", 70085, -3000, 78000, 4500),
    ("44楼书房卫生间 D立面", 78000, -3000, 81844, 4500),
    ("44楼男孩房卫生间 浴缸背景墙", 87597, -1500, 93006, 4500),
    ("44楼主人房卫生间 C立面", 104858, -1500, 108722, 4500),
    ("44楼主人房卫生间 D立面", 109847, -1500, 115491, 4500),
]


def main() -> None:
    doc = ezdxf.readfile(str(SOURCE))
    msp = doc.modelspace()
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

    for label, x0, y0, x1, y1 in VIEWS:
        output.append("=" * 76)
        output.append(f"{label}   ({x0},{y0})-({x1},{y1})")
        region = [
            line for line in lines
            if x0 <= line.bounds[0] and line.bounds[2] <= x1
            and y0 <= line.bounds[1] and line.bounds[3] <= y1
        ]
        polygons = []
        if region:
            merged = unary_union(region)
            polygons = sorted(
                (p for p in polygonize(merged) if p.area >= 15_000),
                key=lambda p: -p.area,
            )
        output.append(f"封闭区域 {len(polygons)}")
        for poly in polygons[:20]:
            b = poly.bounds
            output.append(
                f"   {b[2]-b[0]:7.0f} x {b[3]-b[1]:6.0f}"
                f"  @({b[0]:8.0f},{b[1]:8.0f})  {poly.area/1e6:6.3f} m2"
            )

        dims = []
        for entity in msp:
            if entity.dxftype() != "DIMENSION":
                continue
            try:
                p1, p2 = entity.dxf.defpoint2, entity.dxf.defpoint3
                value = entity.get_measurement()
            except Exception:
                continue
            if (
                x0 <= min(p1.x, p2.x) and max(p1.x, p2.x) <= x1
                and y0 <= min(p1.y, p2.y) and max(p1.y, p2.y) <= y1
            ):
                dims.append((round(value, 1), str(entity.dxf.get("text", ""))))
        output.append("尺寸: " + ", ".join(
            f"{v:g}{('[' + t + ']') if t else ''}" for v, t in dims
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
                texts.append(value.replace("\n", " ")[:50])
        output.append("文字: " + " | ".join(texts))

        linetypes: dict[str, int] = {}
        for entity in msp:
            name = str(getattr(entity.dxf, "linetype", "") or "")
            if name.upper() in {"BYLAYER", "CONTINUOUS", ""}:
                continue
            try:
                box = bbox.extents([entity])
            except Exception:
                continue
            if (
                x0 <= box.extmin.x and box.extmax.x <= x1
                and y0 <= box.extmin.y and box.extmax.y <= y1
            ):
                linetypes[name] = linetypes.get(name, 0) + 1
        output.append(f"非实线: {linetypes}")

    REPORT.write_text("\n".join(output), encoding="utf-8")
    print(f"written {REPORT}")


if __name__ == "__main__":
    main()
