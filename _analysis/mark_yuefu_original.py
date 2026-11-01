"""生成「悦府顶层43-44层」原位对比 DXF。

原则：
- 原图实体一个不删、一个不改（只把符号斜线挪到独立图层，坐标不变）；
- 判读说明、板件一律画在图外空白区，不遮挡原图；
- 字号控制在 60~120（原图标注字高约 120，说明文字不抢视觉）。
"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment


SOURCE = Path(r"C:\Users\drago\Desktop\临时文件\AI识图文件\悦府顶层43-44层.dxf")
OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "output"
    / "analysis_yuefu"
    / "悦府顶层43-44层_原位对比_v3.dxf"
)

LAYERS = {
    "SB_SYMBOL_PASSAGE": 6,
    "SB_SYMBOL_WINDOW": 4,
    "SB_SYMBOL_AXONO": 4,
    "SB_NICHE_OPENING": 1,
    "SB_NICHE_PART": 1,
    "SB_RECESS": 30,
    "SB_RECESS_PART": 30,
    "SB_OPENING": 6,
    "SB_EDGE_SOLID": 30,
    "SB_TAG": 2,
    "SB_LEGEND": 7,
}


def main() -> None:
    doc = ezdxf.readfile(str(SOURCE))
    msp = doc.modelspace()
    for name, color in LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=color)

    extents = bbox.extents(msp)
    min_x, min_y = extents.extmin.x, extents.extmin.y
    max_x, max_y = extents.extmax.x, extents.extmax.y

    def rect(x0, y0, x1, y1, layer, color=None, lw=None):
        attribs = {"layer": layer}
        if color is not None:
            attribs["color"] = color
        if lw is not None:
            attribs["lineweight"] = lw
        return msp.add_lwpolyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            close=True,
            dxfattribs=attribs,
        )

    def text(value, x, y, *, layer="SB_LEGEND", height=80, color=7,
             align=TextEntityAlignment.BOTTOM_LEFT):
        entity = msp.add_text(
            value,
            dxfattribs={"layer": layer, "height": height, "color": color},
        )
        entity.set_placement((x, y), align=align)
        return entity

    def tag(value, x, y, color=2):
        return text(
            value, x, y, layer="SB_TAG", height=60, color=color
        )

    # ------------------------------------------------------------ 符号斜线
    passages = [
        (30931, -1619, 31831, 1081, "① 过道 900×2700"),
        (48512, 2262, 49412, 4962, "① 过道 900×2700"),
        (71605, 2262, 72505, 4962, "① 过道 900×2700"),
    ]
    for x0, y0, x1, y1, value in passages:
        rect(x0, y0, x1, y1, "SB_OPENING", 6)
        tag(value, x0, y1 + 60, color=6)

    windows = [
        (33687, 3367, 34617, 4962, "② 窗户 930"),
        (35677, 3367, 36612, 4962, "② 窗户 935"),
    ]
    for x0, y0, x1, y1, value in windows:
        rect(x0, y0, x1, y1, "SB_OPENING", 4)
        tag(value, x0, y1 + 60, color=4)

    axono = [
        (33644, -1277, 34267, -1096, "③ 轴测示意"),
        (79141, 2180, 79764, 2379, "③ 轴测示意"),
    ]
    for x0, y0, x1, y1, value in axono:
        rect(x0, y0, x1, y1, "SB_SYMBOL_AXONO", 4)
        tag(value, x0, y0 - 200, color=4)

    for entity in list(msp):
        if entity.dxftype() != "LINE":
            continue
        start, end = entity.dxf.start, entity.dxf.end
        length = math.hypot(end.x - start.x, end.y - start.y)
        if length < 1:
            continue
        angle = round(math.degrees(
            math.atan2(end.y - start.y, end.x - start.x)
        )) % 180
        if angle == 25 and 370 <= length <= 390:
            entity.dxf.layer = "SB_SYMBOL_AXONO"
            entity.dxf.color = 4

    # 过道/窗户的斜线符号：Z 形与 V 形
    for x1, y1, x2, y2 in [
        (31831, 1081, 30931, -269),
        (30931, -269, 31831, -1619),
        (49412, 4962, 48512, 3612),
        (48512, 3612, 49412, 2262),
        (72505, 4962, 71605, 3612),
        (71605, 3612, 72505, 2262),
    ]:
        for entity in msp:
            if entity.dxftype() != "LINE":
                continue
            s, e = entity.dxf.start, entity.dxf.end
            if (
                (abs(s.x - x1) <= 1.5 and abs(s.y - y1) <= 1.5
                 and abs(e.x - x2) <= 1.5 and abs(e.y - y2) <= 1.5)
                or (abs(e.x - x1) <= 1.5 and abs(e.y - y1) <= 1.5
                    and abs(s.x - x2) <= 1.5 and abs(s.y - y2) <= 1.5)
            ):
                entity.dxf.layer = "SB_SYMBOL_PASSAGE"
                entity.dxf.color = 6
                break
    for x1, y1, x2, y2 in [
        (33687, 3367, 33951, 4584),
        (33951, 4584, 34617, 4962),
        (35677, 3367, 36007, 4594),
        (36007, 4594, 36612, 4962),
    ]:
        for entity in msp:
            if entity.dxftype() != "LINE":
                continue
            s, e = entity.dxf.start, entity.dxf.end
            if (
                (abs(s.x - x1) <= 1.5 and abs(s.y - y1) <= 1.5
                 and abs(e.x - x2) <= 1.5 and abs(e.y - y2) <= 1.5)
                or (abs(e.x - x1) <= 1.5 and abs(e.y - y1) <= 1.5
                    and abs(s.x - x2) <= 1.5 and abs(s.y - y2) <= 1.5)
            ):
                entity.dxf.layer = "SB_SYMBOL_WINDOW"
                entity.dxf.color = 4
                break

    # ------------------------------------------------- 43楼客用卫生间壁龛
    niche = (32819, -839, 33199, 437)
    rect(*niche, "SB_NICHE_OPENING", 1)
    tag("④ 壁龛", 32819, 500, color=1)
    # 侧板 18 宽条（每侧 3 段 400，中间是层板缝）
    for x in (32819, 33181):
        for y0, y1 in ((-839, -439), (-421, -21), (-3, 437)):
            rect(x, y0, x + 18, y1, "SB_NICHE_PART", 1)
    for y in (-439, -21):
        rect(32819, y, 33199, y + 18, "SB_NICHE_PART", 1)
    # 壁龛开孔位置（墙厚 340，三段 400）
    rect(30441, -914, 30781, 322, "SB_NICHE_OPENING", 1)
    tag("④ 开孔位置", 30441, 380, color=1)

    # -------------------------------------------- 过道造型：凹槽（无背板）
    # 开口 3506×700，三跨（1169×3），深 110；侧板2、上下各3件。
    recess = (18897.7, -1221.1, 22403.7, -521.1)
    rect(*recess, "SB_RECESS", 30)
    tag("⑤ 凹槽 3506×700×深110", 18897.7, -450, color=30)
    for x in (20065.7, 21233.7):
        msp.add_line(
            (x, -1221.1), (x, -521.1),
            dxfattribs={"layer": "SB_RECESS", "color": 30},
        )
    # 平面 110 凹进
    rect(18877.7, 1923.7, 22423.7, 2033.7, "SB_EDGE_SOLID", 30)
    tag("⑤ 平面凹进 110", 18877.7, 2090, color=30)

    # ------------------------------------------------------------ 磨边/做法
    for x in (32417, 33601):
        for y0, y1 in ((-819, -419), (-401, -1), (17, 417)):
            msp.add_line(
                (x, y0), (x, y1),
                dxfattribs={"layer": "SB_EDGE_SOLID", "color": 30},
            )
    msp.add_line(
        (32819, -1259), (33199, -1259),
        dxfattribs={"layer": "SB_EDGE_SOLID", "color": 30},
    )
    tag("⑥ 撇底45°留1mm", 32417, -1500, color=30)

    # ================================================================
    # 图外说明区（全部在图面下方，不遮挡原图）
    # ================================================================
    legend_x = 18000.0
    legend_y = min_y - 1200.0
    text(
        "StoneBot 原位判读说明（图外区，不影响原图阅读）",
        legend_x, legend_y, height=150, color=7,
    )
    lines = [
        ("① 过道洞口 900×2700 ×3：原图两条等长斜线相连（Z形），"
         "通往客房/长辈房/书房。斜线已移入 SB_SYMBOL_PASSAGE，不参与轮廓。", 6),
        ("② 窗户洞口 930 / 935：原图两条斜线相连（V形）。"
         "斜线已移入 SB_SYMBOL_WINDOW，不参与轮廓。", 4),
        ("③ 轴测示意 ×2：25° 等长平行线组（长380），表示层板三面见光，"
         "不是平面几何，已移入 SB_SYMBOL_AXONO。", 4),
        ("④ 43楼客用卫生间壁龛：开口 380×1276，深 280，厚 18。"
         "背板 380×1276×1；侧板 280×400×6（每侧3段）；"
         "层板 380×280×2（成活 378×278，R2圆边收口2mm）。", 1),
        ("④-2 最左 340×三段400 为壁龛开孔位置（墙厚 340），不是独立壁龛。", 1),
        ("⑤ 过道造型墙面凹槽 3506×700，深 110，无背板（见顶部剖视图）。"
         "侧板 110×700×2；上下各 110×1169×3（共6件）；厚 18。"
         "非实线一圈 = 与正面板撇底45°粘接的返边。", 30),
        ("⑥ 线型语义：ZIGZAG=撇底45度留1mm；GAS_LINE=撇底45度粘接R2圆边；"
         "FENCELINE1=见光边。SB_EDGE_SOLID 已把相关边转成实线。", 30),
    ]
    for index, (value, color) in enumerate(lines):
        text(
            value,
            legend_x,
            legend_y - 260 - index * 260,
            height=90,
            color=color,
        )

    # ---------------------------------------------------- 图外板件示意：壁龛
    panel_x = 18000.0
    panel_y = legend_y - 260 - len(lines) * 260 - 500
    text("壁龛板件（比例 1:1）", panel_x, panel_y, height=110, color=1)
    rect(panel_x, panel_y - 1400, panel_x + 380, panel_y - 124, "SB_NICHE_PART", 1)
    text("背板 380×1276 ×1", panel_x, panel_y - 1560, height=70, color=1)
    rect(panel_x + 500, panel_y - 540, panel_x + 780, panel_y - 140, "SB_NICHE_PART", 1)
    text("侧板 280×400 ×6", panel_x + 500, panel_y - 700, height=70, color=1)
    rect(panel_x + 900, panel_y - 420, panel_x + 1280, panel_y - 140, "SB_NICHE_PART", 1)
    text("层板 380×280 ×2", panel_x + 900, panel_y - 580, height=70, color=1)

    # ------------------------------------------- 图外板件示意：过道造型凹槽
    recess_x = panel_x + 1800
    text("过道造型凹槽板件（比例 1:1）", recess_x, panel_y, height=110, color=30)
    for index in range(2):
        rect(
            recess_x + index * 160,
            panel_y - 840,
            recess_x + index * 160 + 110,
            panel_y - 140,
            "SB_RECESS_PART",
            30,
        )
    text("侧板 110×700 ×2", recess_x, panel_y - 1000, height=70, color=30)
    for row in range(2):
        for index in range(3):
            y_top = panel_y - 140 - row * 160
            rect(
                recess_x + 500 + index * 1200,
                y_top - 110,
                recess_x + 500 + index * 1200 + 1169,
                y_top,
                "SB_RECESS_PART",
                30,
            )
    text(
        "上下各 110×1169 ×3（上、下两排，共6件）",
        recess_x + 500,
        panel_y - 760,
        height=70,
        color=30,
    )
    text(
        "无背板（顶部剖视图）｜全部厚 18",
        recess_x + 500,
        panel_y - 860,
        height=70,
        color=30,
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(OUTPUT))
    print(f"written: {OUTPUT}")
    print(f"drawing extents: ({min_x:.0f},{min_y:.0f}) - ({max_x:.0f},{max_y:.0f})")


if __name__ == "__main__":
    main()
