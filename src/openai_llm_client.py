"""OpenAI-compatible chat completions client using only the standard library.

The agent does not depend on the OpenAI SDK. Set ``OPENAI_API_KEY`` and
optionally ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``; the client sends a normal
``/chat/completions`` request and returns the first assistant message as JSON.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from src.llm_client import LLMError


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini"
        self.timeout_s = timeout_s

    def complete_json(self, system_prompt: str, user_prompt: str) -> Any:
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not configured")
        content = self._chat(system_prompt, user_prompt)
        if isinstance(content, (dict, list)):
            return content
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError("model returned invalid JSON") from exc

    def _chat(self, system_prompt: str, user_prompt: str) -> Any:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMError(f"LLM HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM request failed: {exc.reason}") from exc

        payload = json.loads(body)
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("model returned no choices")
        message = choices[0].get("message") or {}
        return message.get("content", "")
