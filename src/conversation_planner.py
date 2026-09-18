"""Convert conversation messages into executable SaaS job intents."""

from __future__ import annotations

import json
from typing import Any

from src.openai_llm_client import OpenAICompatibleClient


CONVERSATION_SYSTEM_PROMPT = """\
You are the planning layer of a stone fabrication SaaS.
Given a user conversation, return only a JSON object.

Allowed actions:
1. {"action": "create_job", "dxf_path": "<local dxf path>", "width_mm": 3200, "height_mm": 1800, "thickness_mm": 20, "usable_width_mm": 3200, "usable_height_mm": 1800, "oversize_enabled": false, "oversize_mode": "total", "oversize_width_mm": 0, "oversize_height_mm": 0, "kerf_mm": 5, "profile": "min_sheets", "process": "bridge", "allow_shared_edge": true, "provider": "queue", "budget": 0, "quick": true, "trials": 1, "seed": 0, "pairing": false, "skip_unnumbered": true}
2. {"action": "review_job", "job_id": "<job id, optional if current job is known>", "max_reductions": 1}
3. {"action": "get_job", "job_id": "<job id, optional>"}
4. {"action": "confirm_job", "job_id": "<job id, optional>", "decision": "confirm"}
5. {"action": "run_job", "job_id": "<job id, optional>"}
6. {"action": "cancel_job", "job_id": "<job id, optional>"}

Do not execute code. Do not invent a dxf_path if the user did not upload one.
Use the current conversation job when no job_id is supplied.
For bridge cutting, kerf_mm is mandatory and must be greater than zero.
"""


def parse_conversation_intent(raw: Any) -> dict[str, Any]:
    """Validate LLM output and return a normalized intent dict."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("conversation intent is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("conversation intent must be a JSON object")

    action = raw.get("action")
    if action == "create_job":
        required = ("width_mm", "height_mm")
        missing = [key for key in required if not raw.get(key)]
        if missing:
            raise ValueError(f"create_job intent is missing {', '.join(missing)}")
        try:
            float(raw["width_mm"])
            float(raw["height_mm"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "create_job width_mm and height_mm must be numeric"
            ) from exc
    elif action in {"review_job", "get_job", "confirm_job", "run_job", "cancel_job"}:
        if action == "confirm_job" and not raw.get("decision"):
            raise ValueError("confirm_job intent is missing decision")
        if action == "review_job":
            max_reductions = raw.get("max_reductions", 1)
            if isinstance(max_reductions, bool) or not isinstance(
                max_reductions, (int, float)
            ):
                raise ValueError("review_job max_reductions must be numeric")
    else:
        raise ValueError(f"unsupported conversation action: {action!r}")
    return raw


class ConversationPlanner:
    def __init__(self, client: OpenAICompatibleClient | None = None) -> None:
        self.client = client or OpenAICompatibleClient()

    def plan(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        prompt = {
            "messages": messages,
            "attachments": attachments or [],
        }
        raw = self.client.complete_json(
            CONVERSATION_SYSTEM_PROMPT,
            json.dumps(prompt, ensure_ascii=False),
        )
        return parse_conversation_intent(raw)
