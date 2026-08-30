from src.ai_planning import (
    ToolCall,
    allowed_tools_for_stage,
    validate_plan,
    validate_tool_call,
)
from src.workflow_session import WorkflowStage


def test_allowed_tools_follow_stage_gate():
    assert allowed_tools_for_stage(WorkflowStage.UPLOADED) == ["analyze_drawing"]
    assert "nest_parts" in allowed_tools_for_stage(
        WorkflowStage.NUMBERING_CONFIRMED
    )


def test_ai_cannot_call_later_stage_tool_early():
    result = validate_tool_call(
        WorkflowStage.READ,
        ToolCall(tool="nest_parts", arguments={"quality": "best"}),
    )

    assert result.valid is False
    assert any("not allowed" in error for error in result.errors)


def test_ai_cannot_write_dxf_before_confirmation():
    result = validate_tool_call(
        WorkflowStage.NESTED_REPORTED,
        ToolCall(tool="write_dxf", arguments={"path": "/tmp/out.dxf"}),
    )

    assert result.valid is False
    assert any("requires postprocess_confirmed" in error for error in result.errors)


def test_ai_plan_requires_structured_arguments():
    result = validate_plan(
        WorkflowStage.NUMBERING_CONFIRMED,
        [ToolCall(tool="nest_parts", arguments={})],
    )

    assert result.valid is False
    assert any("requires structured arguments" in error for error in result.errors)


def test_unknown_tool_is_rejected():
    result = validate_tool_call(
        WorkflowStage.READ,
        ToolCall(tool="run_python", arguments={"code": "print(1)"}),
    )

    assert result.valid is False
    assert any("unknown tool" in error for error in result.errors)
