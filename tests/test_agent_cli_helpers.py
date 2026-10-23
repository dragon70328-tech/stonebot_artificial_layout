import pytest

from src.agent_cli import (
    QueuePlanProvider,
    build_agent_plan_provider,
    build_initial_plan,
    plan_after_confirmation,
)
from src.llm_client import LLMError
from src.llm_plan_provider import LLMPlanProvider
from src.workflow_session import WorkflowStage


def test_build_initial_plan_reaches_numbering_check_dxf():
    plan = build_initial_plan(
        "a.dxf",
        "numbered.dxf",
        skip_unnumbered=True,
    )

    tools = [call.tool for call in plan]
    assert tools == [
        "analyze_drawing",
        "match_profile",
        "read_dxf",
        "audit_drawing",
        "assign_numbers",
        "write_numbered_dxf",
    ]
    assert plan[-1].arguments == {"path": "numbered.dxf"}


def test_plan_after_numbering_confirmation_starts_nesting():
    plan = plan_after_confirmation(
        WorkflowStage.AUDITED.value,
        width_mm=3200,
        height_mm=1600,
        thickness_mm=20,
        nested_path="nested.dxf",
        report_path="report.json",
        trials=1,
        seed=0,
        budget=0,
        quick=True,
        pairing=False,
        beam=True,
        beam_width=250,
        compress=True,
        rect_fast=False,
        low_fill_rework=True,
        low_fill_threshold=0.45,
        candidate_mode="nfp_lite",
    )

    assert [call.tool for call in plan] == ["confirm_numbering", "nest_parts"]
    assert plan[1].arguments["sheet_width"] == 3200
    assert plan[1].arguments["beam_width"] == 250
    assert plan[1].arguments["rect_fast"] is False
    assert plan[1].arguments["low_fill_rework"] is True
    assert plan[1].arguments["low_fill_threshold"] == 0.45
    assert plan[1].arguments["candidate_mode"] == "nfp_lite"


def test_plan_after_sheet_confirmation_runs_postprocess():
    plan = plan_after_confirmation(
        WorkflowStage.NESTED_REPORTED.value,
        width_mm=3200,
        height_mm=1600,
        thickness_mm=20,
        nested_path="nested.dxf",
        report_path="report.json",
        trials=1,
        seed=0,
        budget=0,
        quick=True,
        pairing=False,
    )

    assert [call.tool for call in plan] == ["postprocess"]


def test_queue_plan_provider_returns_fifo_then_empty():
    provider = QueuePlanProvider()
    provider.enqueue([{"tool": "a"}])
    provider.enqueue([{"tool": "b"}])

    assert provider.plan(None, None) == [{"tool": "a"}]
    assert provider.plan(None, None) == [{"tool": "b"}]
    assert provider.plan(None, None) == []


def test_build_agent_plan_provider_defaults_to_queue():
    provider = build_agent_plan_provider("queue")

    assert isinstance(provider, QueuePlanProvider)


def test_build_agent_plan_provider_llm_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(LLMError):
        build_agent_plan_provider("llm")


def test_build_agent_plan_provider_llm_returns_llm_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    provider = build_agent_plan_provider("llm")

    assert isinstance(provider, LLMPlanProvider)
    assert provider.client.api_key == "secret"


def test_build_agent_plan_provider_rejects_unknown():
    with pytest.raises(ValueError):
        build_agent_plan_provider("unknown")
