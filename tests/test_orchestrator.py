from src.ai_planning import ToolCall
from src.orchestrator import Orchestrator
from src.worker import Worker
from src.workflow_session import WorkflowSession, WorkflowStage


def _handlers():
    calls = []

    def record(name):
        def handler(arguments):
            calls.append((name, arguments))
            return {"name": name, "path": arguments.get("path")}

        return handler

    return {
        "analyze_drawing": record("analyze_drawing"),
        "match_profile": record("match_profile"),
        "read_dxf": record("read_dxf"),
        "audit_drawing": record("audit_drawing"),
        "assign_numbers": record("assign_numbers"),
        "confirm_numbering": record("confirm_numbering"),
        "nest_parts": record("nest_parts"),
        "postprocess": record("postprocess"),
        "write_dxf": record("write_dxf"),
        "write_report": record("write_report"),
    }, calls


def _valid_plan():
    return [
        ToolCall("analyze_drawing", {"path": "a.dxf"}),
        ToolCall("match_profile", {"path": "a.dxf"}),
        ToolCall("read_dxf", {"path": "a.dxf"}),
        ToolCall("audit_drawing", {"path": "a.dxf"}),
        ToolCall("assign_numbers", {"path": "a.dxf"}),
        ToolCall("confirm_numbering", {"decision": "confirmed"}),
        ToolCall("nest_parts", {"sheet_width": 3200, "sheet_height": 1600}),
        ToolCall("postprocess", {"result": {}}),
        ToolCall("write_dxf", {"path": "nested.dxf"}),
        ToolCall("write_report", {"path": "report.json"}),
    ]


def test_orchestrator_runs_valid_plan_to_completion():
    handlers, calls = _handlers()
    session = WorkflowSession(session_id="orchestrator-1")
    orchestrator = Orchestrator(session, Worker(handlers))

    outcome = orchestrator.run_plan(_valid_plan())

    assert outcome.completed is True
    assert outcome.blocked is False
    assert session.stage == WorkflowStage.COMPLETED
    assert len(calls) == 10
    assert calls[4][0] == "assign_numbers"
    assert calls[5][0] == "confirm_numbering"


def test_orchestrator_blocks_on_later_stage_tool():
    handlers, _calls = _handlers()
    session = WorkflowSession(session_id="orchestrator-2")
    orchestrator = Orchestrator(session, Worker(handlers))

    outcome = orchestrator.run_plan(
        [ToolCall("nest_parts", {"sheet_width": 3200, "sheet_height": 1600})]
    )

    assert outcome.completed is False
    assert outcome.blocked is True
    assert session.stage == WorkflowStage.BLOCKED


def test_orchestrator_blocks_on_failed_worker_result():
    def fail(_arguments):
        raise RuntimeError("dxf corrupt")

    session = WorkflowSession(session_id="orchestrator-3")
    orchestrator = Orchestrator(
        session,
        Worker({"analyze_drawing": fail}),
    )

    outcome = orchestrator.run_plan(
        [ToolCall("analyze_drawing", {"path": "bad.dxf"})]
    )

    assert outcome.blocked is True
    assert outcome.tool_results[-1].ok is False
    assert session.stage == WorkflowStage.BLOCKED
