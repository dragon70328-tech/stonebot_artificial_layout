"""就地修改 v5：去掉图内板号，改为图外注释 + 引线 + 单位说明。

只改 output/analysis_yuefu/43楼过道造型墙面_规格板原位_v5.dxf，
绝不触碰原始图纸。
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "output" / "analysis_yuefu" / "43楼过道造型墙面_规格板原位_v5.dxf"


def main() -> None:
    doc = ezdxf.readfile(str(TARGET), encoding="gbk")
    msp = doc.modelspace()

    # 清理上一次追加的 NOTE 层（保持脚本可重复执行）；板号保持原位不动
    for entity in list(msp):
        if entity.dxf.layer == "NOTE":
            msp.delete_entity(entity)

    # 图内/尺寸带的文字：删除或移到图外
    for entity in list(msp):
        if entity.dxftype() != "TEXT":
            continue
        value = str(entity.dxf.text or "")
        if value.startswith("凹槽 3506×700"):
            msp.delete_entity(entity)
        elif value.startswith("43楼 过道造型墙面"):
            entity.set_placement(
                (2463, 80), align=TextEntityAlignment.BOTTOM_LEFT
            )
        elif value.startswith("43楼 过道造型 平面"):
            entity.set_placement(
                (4481, 6820), align=TextEntityAlignment.BOTTOM_LEFT
            )

    if "NOTE" not in doc.layers:
        doc.layers.add("NOTE", color=7, linetype="CONTINUOUS")

    def text(value, x, y, height=80):
        entity = msp.add_text(
            value,
            dxfattribs={
                "layer": "NOTE",
                "color": 7,
                "height": height,
                "style": "Standard",
            },
        )
        entity.set_placement((x, y), align=TextEntityAlignment.BOTTOM_LEFT)
        return entity

    def leader(start, bend, target):
        msp.add_line(
            start, bend, dxfattribs={"layer": "NOTE", "color": 8}
        )
        msp.add_line(
            bend, target, dxfattribs={"layer": "NOTE", "color": 8}
        )
        # 箭头（小三角）
        dx, dy = target[0] - bend[0], target[1] - bend[1]
        length = (dx * dx + dy * dy) ** 0.5 or 1.0
        ux, uy = dx / length, dy / length
        bx, by = target[0] - ux * 60, target[1] - uy * 60
        msp.add_lwpolyline(
            [
                (target[0], target[1]),
                (bx - uy * 25, by + ux * 25),
                (bx + uy * 25, by - ux * 25),
            ],
            close=True,
            dxfattribs={"layer": "NOTE", "color": 8},
        )

    text("墙面规格板 8 件", 7950, 4720, 90)
    text("左 1081×2700、右 668×2700；上/下各 3 件 1169×1000", 7950, 4560)
    leader((7920, 4720), (7820, 4720), (7718, 4300))

    text("凹槽内壁规格板 8 件", 7950, 4240, 90)
    text("侧板 110×700×2；上/下口板 110×1169×3", 7950, 4080)
    leader((7920, 4240), (7820, 4240), (7050, 3600))

    text("板厚 18，凹槽深 110，与正面板撇底45°粘接", 7950, 3760)
    leader((7920, 3760), (7820, 3760), (7050, 3300))

    text("单位：mm", 7950, 3480, 90)

    doc.saveas(str(TARGET), encoding="gbk")
    TARGET.write_bytes(
        TARGET.read_bytes().replace(b"ANSI_1252", b"ANSI_936")
    )
    print(f"updated in place: {TARGET}")


if __name__ == "__main__":
    main()
