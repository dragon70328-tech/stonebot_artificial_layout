from src.ai_planning import ToolCall
from src.tool_schemas import get_tool_spec, tool_schema_payload
from src.workflow_session import WorkflowStage


def test_tool_schema_rejects_unknown_arguments():
    spec = get_tool_spec("analyze_drawing")
    errors = spec.validate_arguments(
        ToolCall(tool="analyze_drawing", arguments={"path": "a.dxf", "extra": 1})
    )

    assert errors == ["tool analyze_drawing received unknown arguments: extra"]


def test_tool_schema_requires_declared_fields():
    spec = get_tool_spec("analyze_drawing")
    errors = spec.validate_arguments(ToolCall(tool="analyze_drawing", arguments={}))

    assert any("requires argument" in error for error in errors)


def test_tool_schema_registry_is_json_serializable():
    payload = tool_schema_payload()

    assert payload["analyze_drawing"]["stage"] == WorkflowStage.UPLOADED.value
    assert payload["write_dxf"]["production_write"] is True
    assert payload["confirm_numbering"]["advances_to"] == (
        WorkflowStage.NUMBERING_CONFIRMED.value
    )
