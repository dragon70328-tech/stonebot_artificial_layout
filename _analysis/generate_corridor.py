"""43楼过道造型墙面：单独出图（凹槽8件规格板，图下列表）。"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment

from src.xlsx_writer import write_xlsx


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output" / "analysis_yuefu"
DXF_PATH = OUT_DIR / "43楼过道造型墙面_规格板原位_v5.dxf"
XLSX_PATH = OUT_DIR / "43楼过道造型墙面_加工清单_v5.xlsx"
T = 18.0

RECESS_PANELS = [
    ("R-SIDE-L", "侧板", 110, 700, 1),
    ("R-SIDE-R", "侧板", 110, 700, 1),
    ("R-TOP1", "上口板", 110, 1169, 1),
    ("R-TOP2", "上口板", 110, 1169, 1),
    ("R-TOP3", "上口板", 110, 1169, 1),
    ("R-BOT1", "下口板", 110, 1169, 1),
    ("R-BOT2", "下口板", 110, 1169, 1),
    ("R-BOT3", "下口板", 110, 1169, 1),
]

WALL_PANELS = [
    ("W-LEFT", "左侧整板", 1081, 2700, (2463, 2146, 3544, 4846)),
    ("W-RIGHT", "右侧整板", 668, 2700, (7050, 2146, 7718, 4846)),
    ("W-TOP1", "上段第1跨", 1169, 1000, (3544, 3846, 4712, 4846)),
    ("W-TOP2", "上段第2跨", 1169, 1000, (4712, 3846, 5880, 4846)),
    ("W-TOP3", "上段第3跨", 1169, 1000, (5880, 3846, 7050, 4846)),
    ("W-BOT1", "下段第1跨", 1169, 1000, (3544, 2146, 4712, 3146)),
    ("W-BOT2", "下段第2跨", 1169, 1000, (4712, 2146, 5880, 3146)),
    ("W-BOT3", "下段第3跨", 1169, 1000, (5880, 2146, 7050, 3146)),
]

EDGE = "与正面板撇底45°粘接"


def write_dxf() -> None:
    doc = ezdxf.new("R2000", setup=True)
    doc.units = units.MM
    doc.header["$DWGCODEPAGE"] = "ANSI_936"
    style = doc.styles.get("Standard")
    style.dxf.font = "SimSun.ttf"
    style.dxf.bigfont = ""
    doc.layers.add("REFERENCE", color=8, linetype="CONTINUOUS")
    doc.layers.add("SPEC_PANEL", color=7, linetype="CONTINUOUS")
    doc.layers.add("SPEC_NOTE", color=7, linetype="CONTINUOUS")
    doc.layers.add("DIM", color=4, linetype="CONTINUOUS")

    dimstyle = doc.dimstyles.add("ARCH")
    dimstyle.dxf.dimtxt = 80
    dimstyle.dxf.dimasz = 60
    dimstyle.dxf.dimblk = "ARCHTICK"
    dimstyle.dxf.dimexe = 40
    dimstyle.dxf.dimexo = 20
    dimstyle.dxf.dimgap = 20
    dimstyle.dxf.dimtad = 1
    dimstyle.dxf.dimclrd = 4
    dimstyle.dxf.dimclre = 4
    dimstyle.dxf.dimclrt = 4
    dimstyle.dxf.dimdec = 0
    msp = doc.modelspace()

    def rect(x0, y0, x1, y1, layer):
        msp.add_lwpolyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            close=True,
            dxfattribs={"layer": layer, "color": 8},
        )

    def text(value, x, y, height=80):
        entity = msp.add_text(
            value,
            dxfattribs={
                "layer": "SPEC_NOTE",
                "color": 7,
                "height": height,
                "style": "Standard",
            },
        )
        entity.set_placement((x, y), align=TextEntityAlignment.BOTTOM_LEFT)

    # 原位参考：墙体与凹槽轮廓（灰），规格板不画投影条
    rect(2463, 2146, 7718, 4846, "REFERENCE")
    rect(3544, 3146, 7050, 3846, "REFERENCE")
    for x in (4712, 5880):
        msp.add_line(
            (x, 3146), (x, 3846),
            dxfattribs={"layer": "REFERENCE", "color": 8},
        )

    # 凹槽四周的墙面规格板：原位白色实线
    for pid, _name, _length, _width, (x0, y0, x1, y1) in WALL_PANELS:
        msp.add_lwpolyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            close=True,
            dxfattribs={"layer": "SPEC_PANEL", "color": 7},
        )
        label = msp.add_text(
            pid,
            dxfattribs={
                "layer": "SPEC_PANEL",
                "color": 7,
                "height": 50,
                "style": "Standard",
            },
        )
        label.set_placement(
            ((x0 + x1) / 2, (y0 + y1) / 2),
            align=TextEntityAlignment.MIDDLE_CENTER,
        )

    text("43楼 过道造型墙面（原位参考）", 2463, 5000, 110)
    text("凹槽 3506×700，深110，三跨 1169", 3544, 3920, 80)

    # ------------------------------------------------------------ 平面图
    plan_segments = [
        ((2463, 6291), (3544, 6291)),
        ((2463, 6311), (3524, 6311)),
        ((3544, 6291), (3544, 6401)),
        ((3524, 6311), (3524, 6401)),
        ((3524, 6401), (7070, 6401)),
        ((7050, 6401), (7050, 6291)),
        ((7070, 6401), (7070, 6311)),
        ((7050, 6291), (7718, 6291)),
        ((7070, 6311), (7718, 6311)),
        ((2463, 6291), (2463, 6311)),
        ((7718, 6291), (7718, 6311)),
        ((7070, 6311), (7050, 6291)),
        ((3544, 6291), (3524, 6311)),
    ]
    for start, end in plan_segments:
        msp.add_line(
            start, end,
            dxfattribs={"layer": "REFERENCE", "color": 8},
        )
    text("43楼 过道造型 平面", 4481, 6600, 100)

    # ------------------------------------------------------------ 尺寸标注
    def hdim(p1, p2, y, value=None):
        dim = msp.add_linear_dim(
            base=(p1[0], y),
            p1=p1,
            p2=p2,
            dimstyle="ARCH",
        )
        if value is not None:
            dim.dimension.dxf.text = value
        dim.render()

    def vdim(p1, p2, x, value=None):
        dim = msp.add_linear_dim(
            base=(x, p1[1]),
            p1=p1,
            p2=p2,
            angle=90,
            dimstyle="ARCH",
        )
        if value is not None:
            dim.dimension.dxf.text = value
        dim.render()

    # 立面横向
    hdim((2463, 4846), (3544, 4846), 5150)
    hdim((3544, 4846), (4712, 4846), 5150, "1169")
    hdim((4712, 4846), (5880, 4846), 5150, "1169")
    hdim((5880, 4846), (7050, 4846), 5150, "1169")
    hdim((7050, 4846), (7718, 4846), 5150)
    hdim((2463, 4846), (7718, 4846), 5420)

    # 立面竖向
    vdim((2463, 2146), (2463, 4846), 2240)
    vdim((2463, 3846), (2463, 4846), 2360)
    vdim((2463, 3146), (2463, 3846), 2360)
    vdim((2463, 2146), (2463, 3146), 2360)

    # 平面横向与深度
    hdim((2463, 6401), (3544, 6401), 6650)
    hdim((3544, 6401), (7050, 6401), 6650)
    hdim((7050, 6401), (7718, 6401), 6650)
    vdim((3544, 6291), (3544, 6401), 3420)
    vdim((7050, 6291), (7050, 6401), 7170)

    # ------------------------------------------------------------ 轴测图
    # 30° 轴测：深度方向 (cos30, sin30)
    cos30 = math.cos(math.radians(30.0))
    sin30 = math.sin(math.radians(30.0))
    ax_ox, ax_oy = 8600.0, 2400.0

    def ax(x, y, depth):
        return (
            ax_ox + x + depth * cos30,
            ax_oy + y + depth * sin30,
        )

    def ax_box(x0, x1, y0, y1, d0, d1):
        """画一个长方体（8个顶点、12条边）。"""
        front = [
            ax(x0, y0, d0), ax(x1, y0, d0),
            ax(x1, y1, d0), ax(x0, y1, d0),
        ]
        back = [
            ax(x0, y0, d1), ax(x1, y0, d1),
            ax(x1, y1, d1), ax(x0, y1, d1),
        ]
        msp.add_lwpolyline(
            front + [front[0]], close=True,
            dxfattribs={"layer": "AXONO", "color": 7},
        )
        msp.add_lwpolyline(
            back + [back[0]], close=True,
            dxfattribs={"layer": "AXONO", "color": 8},
        )
        for point_a, point_b in zip(front, back):
            msp.add_line(
                point_a, point_b,
                dxfattribs={"layer": "AXONO", "color": 8},
            )

    doc.layers.add("AXONO", color=7, linetype="CONTINUOUS")
    W, H, D, TH = 3506.0, 700.0, 110.0, 18.0
    # 左右侧板
    ax_box(0, TH, 0, H, 0, D)
    ax_box(W - TH, W, 0, H, 0, D)
    # 上口三块
    for i in range(3):
        x0 = i * W / 3
        x1 = (i + 1) * W / 3
        ax_box(x0, x1, H - TH, H, 0, D)
    # 下口三块
    for i in range(3):
        x0 = i * W / 3
        x1 = (i + 1) * W / 3
        ax_box(x0, x1, 0, TH, 0, D)

    text("凹槽轴测（3506×700×深110，板厚18）", ax_ox, ax_oy + 950, 100)
    text("左右：110×700×18 ×2", ax_ox, ax_oy - 180, 70)
    text("上/下：110×1169×18 各3件（共6件）", ax_ox, ax_oy - 300, 70)
    text("正面板与返板：撇底45°粘接", ax_ox, ax_oy - 420, 70)

    # ------------------------------------------------------ 45°粘接节点放大
    jx, jy = 8600.0, 1000.0
    text("节点：45°撇底粘接（板厚18）", jx, jy + 420, 90)
    # 正面板（竖）
    msp.add_lwpolyline(
        [(jx, jy), (jx + TH, jy), (jx + TH, jy + 300), (jx, jy + 300)],
        close=True,
        dxfattribs={"layer": "AXONO", "color": 7},
    )
    # 返板（横）
    msp.add_lwpolyline(
        [(jx, jy), (jx + D, jy), (jx + D, jy + TH), (jx, jy + TH)],
        close=True,
        dxfattribs={"layer": "AXONO", "color": 7},
    )
    # 45° 割角线
    msp.add_line(
        (jx + TH, jy), (jx, jy + TH),
        dxfattribs={"layer": "AXONO", "color": 30},
    )
    text("45°", jx + 60, jy + 60, 60)
    text("板厚18", jx + 140, jy + 150, 60)

    # 图下规格板清单
    base_y = 1600
    text("墙面规格板（8件）+ 凹槽内壁规格板（8件），厚18", 2463, base_y, 100)
    lines = [
        "墙面：左 1081×2700 ×1；右 668×2700 ×1",
        "墙面：上段 1169×1000 ×3（图示分段1168/1168/1170，按标注1169计）",
        "墙面：下段 1169×1000 ×3（同上）",
        "凹槽：侧板 110×700 ×2（左右各1件）",
        "凹槽：上口板 110×1169 ×3；下口板 110×1169 ×3",
        "边部做法：与正面板撇底45°粘接",
        "合计 16 件 / 12.66 ㎡",
    ]
    for index, value in enumerate(lines):
        text(value, 2463, base_y - 200 - index * 180, 80)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(DXF_PATH), encoding="gbk")
    # ezdxf 固定写 ANSI_1252；中文 CAD 需要 ANSI_936
    DXF_PATH.write_bytes(
        DXF_PATH.read_bytes().replace(b"ANSI_1252", b"ANSI_936")
    )


def write_excel() -> None:
    headers = [
        "编号", "房间/位置", "材料", "长(mm)", "宽(mm)", "厚(mm)",
        "数量", "面积(m²)", "磨边/边部做法", "备注",
    ]
    rows = [headers]
    total = 0.0
    for pid, name, length, width, qty in RECESS_PANELS:
        area = length * width / 1_000_000.0
        total += area
        rows.append(
            [
                pid, "43楼 过道造型墙面（凹槽）",
                "未标注（见图纸材料表）",
                length, width, T, qty, round(area, 4), EDGE, name,
            ]
        )
    for pid, name, length, width, rect_ in WALL_PANELS:
        area = length * width / 1_000_000.0
        total += area
        rows.append(
            [
                pid, "43楼 过道造型墙面（墙面）",
                "未标注（见图纸材料表）",
                length, width, T, 1, round(area, 4),
                "与相邻板/凹槽交接按图", name,
            ]
        )
    rows.append(
        ["合计", "", "", "", "", "",
         len(RECESS_PANELS) + len(WALL_PANELS), round(total, 4), "", ""]
    )
    write_xlsx(XLSX_PATH, rows, sheet_name="加工清单", decimal_columns=(8,))


def main() -> None:
    write_dxf()
    write_excel()
    print(f"dxf:  {DXF_PATH}")
    print(f"xlsx: {XLSX_PATH}")


if __name__ == "__main__":
    main()
