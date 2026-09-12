"""LLM client boundary.

The agent loop does not import any vendor SDK. A client only needs to return a
JSON-serializable value for a system/user prompt pair; the provider decides how
to interpret that value as a tool plan.
"""

from __future__ import annotations

from typing import Any, Protocol


class LLMClient(Protocol):
    def complete_json(self, system_prompt: str, user_prompt: str) -> Any:
        """Return structured JSON-compatible content for the given prompts."""


class LLMError(RuntimeError):
    """Raised when the model boundary fails before a plan can be parsed."""
