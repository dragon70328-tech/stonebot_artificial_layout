"""根据已确认的判读结果生成规格板原位 DXF + 加工清单 Excel。

范围（本次已核对的三处）：
- 43楼 客用卫生间 B立面（9 件）
- 43楼 客用卫生间 C立面 壁龛（9 件）
- 43楼 过道造型墙面 凹槽（8 件）

输出：无图框、白色实线、只画规格板；过道/窗户不画；磨边做法只进 Excel。
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment

from src.xlsx_writer import write_xlsx


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output" / "analysis_yuefu"
DXF_PATH = OUT_DIR / "悦府顶层43-44层_规格板原位.dxf"
XLSX_PATH = OUT_DIR / "悦府顶层43-44层_加工清单.xlsx"

THICKNESS = 18.0


def panel(
    panel_id: str,
    room: str,
    material: str,
    length: float,
    width: float,
    rect: tuple[float, float, float, float],
    edge: str,
    note: str = "",
) -> dict:
    return {
        "id": panel_id,
        "room": room,
        "material": material,
        "length": length,
        "width": width,
        "thickness": THICKNESS,
        "rect": rect,
        "edge": edge,
        "note": note,
    }


def build_panels() -> list[dict]:
    ms04 = "MS-04 博士灰大理石"
    bathroom_b = "43楼 客用卫生间 B立面"
    bathroom_c = "43楼 客用卫生间 C立面（壁龛）"
    corridor = "43楼 过道造型墙面（凹槽）"
    unspecified = "未标注（见图纸材料表）"

    panels: list[dict] = []

    # ---------------- B 立面：上段 3 件 ----------------
    panels += [
        panel("B-U1", bathroom_b, ms04, 200, 1595,
              (33487, 3367, 33687, 4962),
              "靠窗一侧见光边", "窗1 左侧"),
        panel("B-U2", bathroom_b, ms04, 1060, 1595,
              (34617, 3367, 35677, 4962),
              "两侧见光边", "两窗之间"),
        panel("B-U3", bathroom_b, ms04, 60, 1595,
              (36612, 3367, 36672, 4962),
              "靠窗一侧见光边", "窗2 右侧"),
    ]
    # ---------------- B 立面：窗台板 3 件（100 宽） ----------------
    panels += [
        panel("B-S1", bathroom_b, ms04, 1130, 100,
              (33487, 3226, 34617, 3326),
              "两端45°撇底，与上下板粘接", "窗台板（水平）"),
        panel("B-S2", bathroom_b, ms04, 1060, 100,
              (34617, 3226, 35677, 3326),
              "两端45°撇底，与上下板粘接", "窗台板（水平）"),
        panel("B-S3", bathroom_b, ms04, 995, 100,
              (35677, 3226, 36672, 3326),
              "两端45°撇底，与上下板粘接", "窗台板（水平）"),
    ]
    # ---------------- B 立面：下段 3 件（1155 高） ----------------
    panels += [
        panel("B-L1", bathroom_b, ms04, 1130, 1155,
              (33487, 2071, 34617, 3226),
              "上口45°撇底接窗台板；地面50收口", ""),
        panel("B-L2", bathroom_b, ms04, 1060, 1155,
              (34617, 2071, 35677, 3226),
              "上口45°撇底接窗台板；地面50收口", ""),
        panel("B-L3", bathroom_b, ms04, 995, 1155,
              (35677, 2071, 36672, 3226),
              "上口45°撇底接窗台板；地面50收口", ""),
    ]

    # ---------------- C 立面壁龛：背板 1 件 ----------------
    panels.append(
        panel("C-BACK", bathroom_c, ms04, 380, 1276,
              (32819, -839, 33199, 437), "未见光（嵌固面）", "壁龛背板")
    )
    # ---------------- C 立面壁龛：侧板 6 件（每侧3段） ----------------
    for side, x0 in (("L", 32417), ("R", 33321)):
        for index, (y0, y1) in enumerate(
            ((-819, -419), (-401, -1), (17, 417)), start=1
        ):
            panels.append(
                panel(
                    f"C-SIDE-{side}{index}",
                    bathroom_c,
                    ms04,
                    280,
                    400,
                    (x0, y0, x0 + 280, y1),
                    "外缘撇底45度留1mm；内侧见光",
                    f"壁龛侧板 第{index}段",
                    )
            )
    # ---------------- C 立面壁龛：层板 2 件（图形按原位投影 18 高） ----------------
    for index, y0 in enumerate((-419, -1), start=1):
        panels.append(
            panel(
                f"C-SHELF{index}",
                bathroom_c,
                ms04,
                380,
                280,
                (32819, y0, 33199, y0 + 18),
                "三面见光；单长上下R2圆边",
                "图形为原位投影（板厚18）；成活 378×278",
            )
        )

    # ---------------- 过道造型凹槽：侧板 2 件 ----------------
    panels += [
        panel("R-SIDE-L", corridor, unspecified, 110, 700,
              (18897.7, -1221.1, 18915.7, -521.1),
              "与正面板撇底45°粘接", "图中为18宽投影"),
        panel("R-SIDE-R", corridor, unspecified, 110, 700,
              (22385.7, -1221.1, 22403.7, -521.1),
              "与正面板撇底45°粘接", "图中为18宽投影"),
    ]
    # ---------------- 过道造型凹槽：上下各3件（110×1169） ----------------
    bays = [
        (18897.7, 20065.7),
        (20065.7, 21233.7),
        (21233.7, 22403.7),
    ]
    for index, (x0, x1) in enumerate(bays, start=1):
        panels.append(
            panel(
                f"R-TOP{index}",
                corridor,
                unspecified,
                110,
                1169,
                (x0, -539.1, x1, -521.1),
                "与正面板撇底45°粘接",
                "上口横板；图中为18高投影",
            )
        )
    for index, (x0, x1) in enumerate(bays, start=1):
        panels.append(
            panel(
                f"R-BOT{index}",
                corridor,
                unspecified,
                110,
                1169,
                (x0, -1221.1, x1, -1203.1),
                "与正面板撇底45°粘接",
                "下口横板；图中为18高投影",
            )
        )
    return panels


def write_dxf(panels: list[dict], path: Path) -> None:
    doc = ezdxf.new("R2010")
    doc.units = units.MM
    msp = doc.modelspace()

    doc.layers.add("SPEC_PANEL", color=7, linetype="CONTINUOUS")
    doc.layers.add("ROOM_TEXT", color=7, linetype="CONTINUOUS")
    doc.layers.add("PANEL_ID", color=8, linetype="CONTINUOUS")

    for item in panels:
        x0, y0, x1, y1 = item["rect"]
        msp.add_lwpolyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            close=True,
            dxfattribs={"layer": "SPEC_PANEL", "color": 7},
        )
        width = abs(x1 - x0)
        height = abs(y1 - y0)
        if width >= 200 and height >= 200:
            label = msp.add_text(
                item["id"],
                dxfattribs={
                    "layer": "PANEL_ID",
                    "color": 8,
                    "height": 45,
                },
            )
            label.set_placement(
                ((x0 + x1) / 2, (y0 + y1) / 2),
                align=TextEntityAlignment.MIDDLE_CENTER,
            )

    room_labels = [
        ("43楼 客用卫生间 B立面", 33487, 5100),
        ("43楼 客用卫生间 C立面（壁龛）", 30441, 600),
        ("43楼 过道造型墙面（凹槽）", 18897.7, -300),
    ]
    for text, x, y in room_labels:
        entity = msp.add_text(
            text,
            dxfattribs={"layer": "ROOM_TEXT", "color": 7, "height": 90},
        )
        entity.set_placement((x, y), align=TextEntityAlignment.BOTTOM_LEFT)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(path))


def write_excel(panels: list[dict], path: Path) -> None:
    headers = [
        "编号",
        "房间/位置",
        "材料",
        "长(mm)",
        "宽(mm)",
        "厚(mm)",
        "数量",
        "面积(m²)",
        "磨边/边部做法",
        "备注",
    ]
    rows: list[list] = [headers]
    total_area = 0.0
    for item in panels:
        area = item["length"] * item["width"] / 1_000_000.0
        total_area += area
        rows.append(
            [
                item["id"],
                item["room"],
                item["material"],
                item["length"],
                item["width"],
                item["thickness"],
                1,
                round(area, 4),
                item["edge"],
                item["note"],
            ]
        )
    rows.append(
        [
            "合计",
            "",
            "",
            "",
            "",
            "",
            len(panels),
            round(total_area, 4),
            "",
            "",
        ]
    )
    write_xlsx(
        path,
        rows,
        sheet_name="加工清单",
        decimal_columns=(8,),
    )


def main() -> None:
    panels = build_panels()
    write_dxf(panels, DXF_PATH)
    write_excel(panels, XLSX_PATH)
    print(f"dxf:   {DXF_PATH}")
    print(f"xlsx:  {XLSX_PATH}")
    print(f"panels: {len(panels)}")


if __name__ == "__main__":
    main()
