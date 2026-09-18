"""排板请求、板材规格和工艺策略契约。

这些对象是排板阶段的跨服务输入输出边界。几何算法可以替换，但大板、
让尺、厚度、刀缝和工艺限制必须通过同一套结构传递，禁止再由各个求解器
自行解释零散参数。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from src.contracts import ConfirmedDrawing, Contract


class CuttingProcess(str, Enum):
    BRIDGE = "bridge"
    WATERJET = "waterjet"
    LASER = "laser"


class OversizeMode(str, Enum):
    TOTAL = "total"
    PER_SIDE = "per_side"


class SharedEdgeMode(str, Enum):
    STRAIGHT = "straight"
    ANY_CONTOUR = "any_contour"


class SheetSpec(Contract):
    """标称板材、实际可利用尺寸和可选让尺的统一描述。"""

    nominal_width_mm: float = Field(gt=0)
    nominal_height_mm: float = Field(gt=0)
    usable_width_mm: float = Field(gt=0)
    usable_height_mm: float = Field(gt=0)
    thickness_mm: float = Field(default=20.0, gt=0)

    oversize_enabled: bool = False
    oversize_mode: OversizeMode = OversizeMode.TOTAL
    oversize_width_mm: float = Field(default=0.0, ge=0)
    oversize_height_mm: float = Field(default=0.0, ge=0)

    @classmethod
    def from_legacy(
        cls,
        width_mm: float,
        height_mm: float,
        thickness_mm: float = 20.0,
        *,
        usable_width_mm: float | None = None,
        usable_height_mm: float | None = None,
    ) -> "SheetSpec":
        """兼容旧 CLI/API 参数，未提供可利用尺寸时视为标称尺寸。"""
        return cls(
            nominal_width_mm=width_mm,
            nominal_height_mm=height_mm,
            usable_width_mm=usable_width_mm or width_mm,
            usable_height_mm=usable_height_mm or height_mm,
            thickness_mm=thickness_mm,
        )

    @property
    def oversize_added_width_mm(self) -> float:
        if not self.oversize_enabled:
            return 0.0
        if self.oversize_mode == OversizeMode.PER_SIDE:
            return self.oversize_width_mm * 2
        return self.oversize_width_mm

    @property
    def oversize_added_height_mm(self) -> float:
        if not self.oversize_enabled:
            return 0.0
        if self.oversize_mode == OversizeMode.PER_SIDE:
            return self.oversize_height_mm * 2
        return self.oversize_height_mm

    @property
    def effective_width_mm(self) -> float:
        return self.usable_width_mm + self.oversize_added_width_mm

    @property
    def effective_height_mm(self) -> float:
        return self.usable_height_mm + self.oversize_added_height_mm

    def dimensions(self) -> dict[str, float]:
        """返回前端和报告都可直接展示的派生尺寸。"""
        return {
            "nominal_width_mm": self.nominal_width_mm,
            "nominal_height_mm": self.nominal_height_mm,
            "usable_width_mm": self.usable_width_mm,
            "usable_height_mm": self.usable_height_mm,
            "oversize_added_width_mm": self.oversize_added_width_mm,
            "oversize_added_height_mm": self.oversize_added_height_mm,
            "effective_width_mm": self.effective_width_mm,
            "effective_height_mm": self.effective_height_mm,
            "thickness_mm": self.thickness_mm,
        }


class ProcessPolicy(Contract):
    """用户选定的加工方法及其硬约束。"""

    process: CuttingProcess
    rotation_degrees: list[float] = Field(
        default_factory=lambda: [0.0, 90.0, 180.0, 270.0]
    )
    arbitrary_rotation: bool = False
    allow_shared_edge: bool = True
    shared_edge_mode: SharedEdgeMode = SharedEdgeMode.STRAIGHT
    kerf_mm: float = Field(default=0.0, ge=0)
    min_gap_mm: float = Field(default=0.0, ge=0)

    @classmethod
    def bridge(
        cls,
        *,
        kerf_mm: float,
        min_gap_mm: float = 0.0,
        rotation_degrees: list[float] | None = None,
        allow_shared_edge: bool = True,
    ) -> "ProcessPolicy":
        return cls(
            process=CuttingProcess.BRIDGE,
            rotation_degrees=rotation_degrees or [0.0, 90.0, 180.0, 270.0],
            arbitrary_rotation=False,
            allow_shared_edge=allow_shared_edge,
            shared_edge_mode=SharedEdgeMode.STRAIGHT,
            kerf_mm=kerf_mm,
            min_gap_mm=min_gap_mm,
        )

    @classmethod
    def waterjet(
        cls,
        *,
        min_gap_mm: float = 0.0,
        kerf_mm: float = 0.0,
        allow_shared_edge: bool = True,
    ) -> "ProcessPolicy":
        return cls(
            process=CuttingProcess.WATERJET,
            arbitrary_rotation=True,
            allow_shared_edge=allow_shared_edge,
            shared_edge_mode=SharedEdgeMode.ANY_CONTOUR,
            kerf_mm=kerf_mm,
            min_gap_mm=min_gap_mm,
        )

    @classmethod
    def laser(
        cls,
        *,
        min_gap_mm: float = 0.0,
        kerf_mm: float = 0.0,
        allow_shared_edge: bool = True,
    ) -> "ProcessPolicy":
        return cls(
            process=CuttingProcess.LASER,
            arbitrary_rotation=True,
            allow_shared_edge=allow_shared_edge,
            shared_edge_mode=SharedEdgeMode.ANY_CONTOUR,
            kerf_mm=kerf_mm,
            min_gap_mm=min_gap_mm,
        )

    @model_validator(mode="after")
    def validate_rotation_policy(self) -> "ProcessPolicy":
        if self.process == CuttingProcess.BRIDGE:
            if self.arbitrary_rotation:
                raise ValueError("bridge process cannot use arbitrary rotation")
            if self.shared_edge_mode != SharedEdgeMode.STRAIGHT:
                raise ValueError("bridge process only allows straight shared edges")
            for angle in self.rotation_degrees:
                normalized = float(angle) % 90.0
                if min(normalized, 90.0 - normalized) > 1e-9:
                    raise ValueError(
                        "bridge rotation angles must be multiples of 90 degrees"
                    )
        return self

    def validation_errors(self) -> list[str]:
        """返回尚不能执行排板的原因，不替代后续几何校验。"""
        errors: list[str] = []
        if self.process == CuttingProcess.BRIDGE and self.kerf_mm <= 0:
            errors.append("bridge process requires kerf_mm > 0")
        return errors

    @property
    def is_ready(self) -> bool:
        return not self.validation_errors()


class NestingRequest(Contract):
    """一次排板请求的完整输入快照。"""

    request_id: str
    confirmed_drawing: ConfirmedDrawing
    sheet: SheetSpec
    process: ProcessPolicy
    target_sheet_count: int | None = Field(default=None, ge=1)
    feedback: dict[str, Any] = Field(default_factory=dict)
    seed: int = 0
    budget_seconds: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def validate_process_ready(self) -> "NestingRequest":
        errors = self.process.validation_errors()
        if errors:
            raise ValueError("; ".join(errors))
        return self


class NestingRevision(Contract):
    """一次不可变排板结果，供比较和反馈重排使用。"""

    revision_id: str
    request: NestingRequest
    algorithm: str
    layout: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    validation_errors: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
