import io
import json

import pytest

from src.llm_client import LLMError
from src.openai_llm_client import OpenAICompatibleClient


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_openai_client_parses_json_content(monkeypatch):
    response = FakeResponse(
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"tool_calls": []})
                        }
                    }
                ]
            }
        ).encode("utf-8")
    )
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return response

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        api_key="secret",
        base_url="https://example.test/v1",
        model="fake-model",
    )

    result = client.complete_json("system", "user")

    assert result == {"tool_calls": []}
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["auth"] == "Bearer secret"


def test_openai_client_requires_api_key(monkeypatch):
    client = OpenAICompatibleClient(api_key=None, base_url="https://example.test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(LLMError):
        client.complete_json("system", "user")
