"""
本体约束模块：工艺约束、材料规格、排板策略。
所有约束均可通过预置模板快速选择，或自由组合。

使用方式：
    # 预置模板
    from src.constraints import PROFILE_TEXTURED_ONE_SET, PROFILE_MIN_SHEETS
    profile = PROFILE_TEXTURED_ONE_SET

    # 自由组合
    from src.constraints import NestingProfile
    profile = NestingProfile(
        rotation=[0, 90],
        min_gap=60,
        group_mode=None,
        slide_to_edge=True,
        align_edges=True,
        sheet_thickness=20,
    )

    # 查询标准规格
    from src.constraints import STANDARD_SHEET_SIZES, STANDARD_THICKNESSES
    for w, h, label in STANDARD_SHEET_SIZES:
        print(f"{label}: {w}x{h}")
"""

from dataclasses import dataclass, field
from typing import Optional, Literal

# 分组模式
GroupMode = Optional[Literal["one_set_per_sheet"]]
# one_set_per_sheet: 一张大板排一套完整户型（需配合 part.group_id 使用）


# ================================================================
#  标准材料规格
# ================================================================

# 标准大板尺寸：(宽度 X, 高度 Y, 标签)
# 注意：大板通常 X=长边（纹理方向），Y=短边
STANDARD_SHEET_SIZES: list[tuple[float, float, str]] = [
    (3200, 1600, "3200×1600"),
    (3200, 1800, "3200×1800"),
    (3000, 1400, "3000×1400"),
    (3000, 1200, "3000×1200"),
    (2700, 1800, "2700×1800"),
    (3000, 1800, "3000×1800"),
]

# 标准厚度 (mm)
STANDARD_THICKNESSES: list[float] = [15.0, 20.0, 25.0, 30.0]


def get_sheet_size(label: str) -> tuple[float, float] | None:
    """按标签查找标准大板尺寸，如 '3200x1800' -> (3200, 1800)"""
    for w, h, lbl in STANDARD_SHEET_SIZES:
        if lbl.replace("×", "x") == label.replace("×", "x"):
            return (w, h)
    return None


def get_sheet_size_by_index(idx: int) -> tuple[float, float, str] | None:
    """按序号获取标准大板尺寸 (1-based)"""
    if 1 <= idx <= len(STANDARD_SHEET_SIZES):
        return STANDARD_SHEET_SIZES[idx - 1]
    return None


def find_closest_sheet(width: float, height: float) -> tuple[float, float, str]:
    """找到面积最接近的标准大板尺寸"""
    target = width * height
    best = min(STANDARD_SHEET_SIZES,
               key=lambda s: abs(s[0] * s[1] - target))
    return best


# ================================================================
#  NestingProfile
# ================================================================

@dataclass
class NestingProfile:
    """排板约束参数集，所有字段均可独立覆盖"""

    # ---- 旋转 ----
    # 允许的旋转角度列表，如 [0] 或 [0, 90, 180, 270]
    rotation: list[int] = field(default_factory=lambda: [0, 90, 180, 270])

    # ---- 切割间距 ----
    # 面板之间最小安全切割间距（mm），0 表示不要求
    min_gap: float = 0.0

    # ---- 分组 ----
    # None: 不分组，所有面板统一排板
    # "one_set_per_sheet": 同一套（group_id 相同）的面板放同一张板
    group_mode: GroupMode = None

    # ---- 后处理 ----
    # 排板完成后将面板推往大板边缘/角落，空出内部切割通道
    slide_to_edge: bool = True

    # 推边时尽量让相邻面板边缘对齐在同一直线上
    align_edges: bool = True

    # ---- 材料参数（记录用，不影响排板） ----
    # 大板厚度（mm），未来用于工时/成本核算
    sheet_thickness: float = 20.0

    # ---- 边缘余量 ----
    # 面板距大板边缘最小距离（mm），人造石大板按精确尺寸供货，通常为 0
    edge_margin: float = 0.0

    @property
    def thickness_label(self) -> str:
        return f"{self.sheet_thickness:.0f}mm"

    def with_overrides(self, **kwargs) -> "NestingProfile":
        """返回一个覆盖部分字段的新 profile"""
        d = {f.name: getattr(self, f.name)
             for f in self.__dataclass_fields__.values()}
        d.update(kwargs)
        return NestingProfile(**d)


# ================================================================
#  预置模板
# ================================================================

PROFILE_TEXTURED_ONE_SET = NestingProfile(
    rotation=[0],              # 纹路方向不可旋转
    min_gap=100.0,             # 安全切割间距 ≥ 10cm
    group_mode="one_set_per_sheet",
    slide_to_edge=True,
    align_edges=True,
)

PROFILE_MIN_SHEETS = NestingProfile(
    rotation=[0, 90, 180, 270],  # 无纹路，允许任意旋转套切
    min_gap=0.0,                  # 追求最少板数，不要求间距
    group_mode=None,              # 不分组
    slide_to_edge=True,
    align_edges=True,
)

# 折中方案：无纹路大板但要求切割间距
PROFILE_BALANCED = NestingProfile(
    rotation=[0, 90, 180, 270],
    min_gap=60.0,
    group_mode=None,
    slide_to_edge=True,
    align_edges=True,
)

# 仅排板不后处理（快速预览用）
PROFILE_QUICK = NestingProfile(
    rotation=[0, 90, 180, 270],
    min_gap=0.0,
    group_mode=None,
    slide_to_edge=False,
    align_edges=False,
)


# ================================================================
#  预置模板注册表
# ================================================================

PROFILES: dict[str, NestingProfile] = {
    "textured": PROFILE_TEXTURED_ONE_SET,
    "min_sheets": PROFILE_MIN_SHEETS,
    "balanced": PROFILE_BALANCED,
    "quick": PROFILE_QUICK,
}

PROFILE_HELP = {
    "textured": "纹路大板，一套一板，禁止旋转，间距≥10cm",
    "min_sheets": "最少大板数，允许旋转套切，不要求间距",
    "balanced": "折中方案，允许旋转，间距≥6cm",
    "quick": "快速预览，无后处理",
}
