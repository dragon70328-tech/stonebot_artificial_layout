"""单位制管理：英制（inch）与公制（mm）切换"""

from enum import Enum


class UnitSystem(Enum):
    METRIC = "metric"      # 公制：mm
    IMPERIAL = "imperial"  # 英制：inch

# 单位标签
UNIT_LABELS = {
    UnitSystem.METRIC: "mm",
    UnitSystem.IMPERIAL: "inch",
}

# 换算因子：1 inch = 25.4 mm
INCH_TO_MM = 25.4


def convert_to_mm(value: float, unit: UnitSystem) -> float:
    """将值转换为 mm（内部计算统一使用 mm）"""
    if unit == UnitSystem.IMPERIAL:
        return value * INCH_TO_MM
    return value


def convert_from_mm(value: float, unit: UnitSystem) -> float:
    """将 mm 转换为目标单位"""
    if unit == UnitSystem.IMPERIAL:
        return value / INCH_TO_MM
    return value
