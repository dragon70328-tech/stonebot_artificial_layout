from src.agent_cli import (
    QueuePlanProvider,
    build_initial_plan,
    plan_after_confirmation,
)
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
    )

    assert [call.tool for call in plan] == ["confirm_numbering", "nest_parts"]
    assert plan[1].arguments["sheet_width"] == 3200


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
