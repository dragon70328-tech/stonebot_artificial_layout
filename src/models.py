"""数据模型定义：规格板（Part）、大板（Sheet）"""

from dataclasses import dataclass, field
from typing import Optional
from shapely.geometry import Polygon


@dataclass
class Part:
    """规格板：一个大封闭图形（含挖孔）"""
    id: int                          # 内部唯一标识
    number: str                      # 编号（P-0001 或原编号）
    polygon: Polygon                 # 带洞多边形（已减去挖孔）
    outer_polygon: Polygon           # 外轮廓多边形（不含挖孔）
    holes: list[Polygon] = field(default_factory=list)  # 挖孔列表
    original_number: Optional[str] = None  # DXF 中原有编号
    area: float = 0.0                # 有效面积（外轮廓面积 - 挖孔面积）
    label_position: tuple[float, float] = (0.0, 0.0)  # 编号标注位置
    outer_handle: str | None = None       # 原DXF中外轮廓实体handle
    hole_handles: list[str] = field(default_factory=list)  # 原DXF中孔洞实体handle

    def __post_init__(self):
        if self.area == 0.0:
            self.area = self.polygon.area


@dataclass
class Sheet:
    """大板：一块矩形板材"""
    index: int                       # 大板序号（1-based）
    width: float                     # X 方向长度
    height: float                    # Y 方向宽度
    thickness: float                 # 厚度
    parts: list[Part] = field(default_factory=list)
    remaining_area: float = 0.0      # 剩余面积

    def __post_init__(self):
        if self.remaining_area == 0.0:
            self.remaining_area = self.width * self.height

    @property
    def total_area(self) -> float:
        return self.width * self.height

    @property
    def used_area(self) -> float:
        return sum(p.area for p in self.parts)


@dataclass
class NestingResult:
    """排板结果"""
    sheets: list[Sheet]
    unit: str                        # 单位制：metric / imperial
    total_parts: int
    total_sheets: int
    total_part_area: float
    total_sheet_area: float

    @property
    def yield_rate(self) -> float:
        """出材率"""
        if self.total_sheet_area == 0:
            return 0.0
        return self.total_part_area / self.total_sheet_area * 100
