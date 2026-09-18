"""LLM planner for correcting an already recognized numbered drawing."""

from __future__ import annotations

import json
import uuid
from typing import Any

from src.correction_contracts import CorrectionPlan
from src.openai_llm_client import OpenAICompatibleClient


CORRECTION_SYSTEM_PROMPT = """\
You convert a user's DXF correction request into a strict JSON plan.
Return only JSON. Do not edit files or execute code.

Allowed operations:
1. {"operation": "rename_number", "target": "P-0001", "value": "P-0001A", "reason": "..."}
2. {"operation": "swap_numbers", "target": "P-0001", "target_b": "P-0002", "reason": "..."}
3. {"operation": "set_material", "target": "P-0001", "value": "01B", "reason": "..."}
4. {"operation": "remove_panel", "target": "P-0001", "reason": "误识别"}
5. {"operation": "ignore_entity", "target": "handle-or-number", "reason": "..."}
6. {"operation": "fill_hole", "target": "hole-id-or-handle", "reason": "该轮廓不是孔洞"}
7. {"operation": "panel_to_hole", "target": "P-0002", "target_b": "P-0001", "reason": "误识别为独立面板"}
8. {"operation": "hole_to_panel", "target": "hole-id-or-handle", "value": "P-0010", "reason": "该孔洞实际是规格板"}
9. {"operation": "add_panel_from_entity", "target": "handle:H9", "value": "P-0020", "reason": "漏识别规格板"}
10. {"operation": "add_hole_from_entity", "target": "handle:H9", "target_b": "P-0001", "reason": "漏识别孔洞"}
11. {"operation": "merge_panels", "target": "P-0001", "target_b": "P-0002", "reason": "两块应为同一规格板"}
12. {"operation": "split_panel_vertical", "target": "P-0001", "position": 1200, "value": "P-0010", "reason": "按竖直线拆分"}
13. {"operation": "split_panel_horizontal", "target": "P-0001", "position": 600, "value": "P-0011", "reason": "按水平线拆分"}

Rules:
- Use only target identifiers present in the supplied drawing context.
- If the user says "这个" but no target_id is supplied and the target is ambiguous,
  return {"operations": []} rather than guessing.
- Do not rename to an existing number. Use swap_numbers for an exchange.
- Preserve all other panels unchanged.
- When an unrecognized entity should be a panel, use add_panel_from_entity.
- When an unrecognized entity should be a hole, use add_hole_from_entity and
  supply the containing panel in target_b.
"""


def parse_correction_plan(
    raw: Any,
    *,
    instruction: str,
) -> CorrectionPlan:
    """Validate an LLM correction response."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("correction plan is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("correction plan must be a JSON object")
    payload = dict(raw)
    payload.setdefault("plan_id", f"correction-{uuid.uuid4().hex}")
    payload["instruction"] = instruction
    return CorrectionPlan.model_validate(payload)


class CorrectionPlanner:
    def __init__(self, client: OpenAICompatibleClient | None = None) -> None:
        self.client = client or OpenAICompatibleClient()

    def plan(
        self,
        instruction: str,
        *,
        context: dict[str, Any],
        target_ids: list[str] | None = None,
    ) -> CorrectionPlan:
        prompt = {
            "instruction": instruction,
            "selected_target_ids": target_ids or [],
            "drawing_context": context,
        }
        raw = self.client.complete_json(
            CORRECTION_SYSTEM_PROMPT,
            json.dumps(prompt, ensure_ascii=False),
        )
        plan = parse_correction_plan(raw, instruction=instruction)
        if not plan.operations:
            raise ValueError("correction plan requires at least one operation")
        return plan
