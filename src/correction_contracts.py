"""原位 DXF 对话纠错的结构化契约。

LLM 只能生成这里定义的纠正操作，具体修改由确定性工具执行。
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import Field, model_validator

from src.contracts import ActorKind, Contract


class CorrectionOperationType(str, Enum):
    RENAME_NUMBER = "rename_number"
    SWAP_NUMBERS = "swap_numbers"
    SET_MATERIAL = "set_material"
    REMOVE_PANEL = "remove_panel"
    IGNORE_ENTITY = "ignore_entity"


class CorrectionOperation(Contract):
    """一个可由确定性工具执行的图纸纠正动作。"""

    operation: CorrectionOperationType
    target: str = Field(min_length=1)
    target_b: str | None = None
    value: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def validate_required_values(self) -> "CorrectionOperation":
        needs_value = {
            CorrectionOperationType.RENAME_NUMBER,
            CorrectionOperationType.SET_MATERIAL,
        }
        if self.operation in needs_value and not self.value:
            raise ValueError(f"{self.operation} requires value")
        if self.operation == CorrectionOperationType.SWAP_NUMBERS and not self.target_b:
            raise ValueError("swap_numbers requires target_b")
        return self


class CorrectionPlan(Contract):
    """一次用户对话产生的完整纠正计划。"""

    plan_id: str = Field(default_factory=lambda: f"correction-{uuid.uuid4().hex}")
    instruction: str = Field(min_length=1)
    operations: list[CorrectionOperation] = Field(min_length=1)
    created_by: ActorKind = ActorKind.AI

    @model_validator(mode="after")
    def reject_noop_swap(self) -> "CorrectionPlan":
        for operation in self.operations:
            if (
                operation.operation == CorrectionOperationType.SWAP_NUMBERS
                and operation.target == operation.target_b
            ):
                raise ValueError("swap_numbers targets must be different")
        return self


class CorrectionResult(Contract):
    """一次纠正执行后的可审计结果。"""

    plan_id: str
    revision: int = Field(ge=1)
    before_digest: str
    after_digest: str
    artifact_path: str
    operations_applied: list[str] = Field(default_factory=list)
    removed_part_ids: list[int] = Field(default_factory=list)
    part_numbers: list[str] = Field(default_factory=list)
