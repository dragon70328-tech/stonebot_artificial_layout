import pytest

from src.conversation_planner import (
    ConversationPlanner,
    parse_conversation_intent,
)


class FakeLLMClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete_json(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return self.response


def test_parse_create_job_allows_missing_dxf_path_for_attachment_fallback():
    intent = parse_conversation_intent(
        {"action": "create_job", "width_mm": 3200, "height_mm": 1800}
    )

    assert intent["width_mm"] == 3200


def test_parse_create_job_requires_sheet_dimensions():
    with pytest.raises(ValueError, match="width_mm"):
        parse_conversation_intent({"action": "create_job", "height_mm": 1800})


def test_parse_create_job_rejects_non_numeric_dimensions():
    with pytest.raises(ValueError, match="numeric"):
        parse_conversation_intent(
            {"action": "create_job", "width_mm": "wide", "height_mm": 1800}
        )


def test_parse_review_job_allows_missing_job_id_for_active_job_fallback():
    assert parse_conversation_intent({"action": "review_job"}) == {
        "action": "review_job"
    }


def test_parse_confirm_job_requires_decision():
    with pytest.raises(ValueError, match="decision"):
        parse_conversation_intent({"action": "confirm_job"})


def test_parse_rejects_unknown_action():
    with pytest.raises(ValueError, match="unsupported"):
        parse_conversation_intent({"action": "delete_all"})


def test_planner_forwards_attachments_and_parses_json():
    client = FakeLLMClient(
        '{"action": "review_job", "job_id": "job-9", "max_reductions": 2}'
    )
    planner = ConversationPlanner(client)

    intent = planner.plan(
        [{"role": "user", "content": "能不能再少一张"}],
        attachments=[{"dxf_path": "C:/uploads/water.dxf"}],
    )

    assert intent == {
        "action": "review_job",
        "job_id": "job-9",
        "max_reductions": 2,
    }
    assert len(client.calls) == 1
    _, user_prompt = client.calls[0]
    assert "C:/uploads/water.dxf" in user_prompt
