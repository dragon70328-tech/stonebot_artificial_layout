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

Rules:
- Use only target identifiers present in the supplied drawing context.
- If the user says "这个" but no target_id is supplied and the target is ambiguous,
  return {"operations": []} rather than guessing.
- Do not rename to an existing number. Use swap_numbers for an exchange.
- Preserve all other panels unchanged.
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
