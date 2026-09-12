from src.agent_loop import AgentLoop, StaticPlanProvider
from src.ai_planning import ToolCall
from src.session_store import WorkflowSessionStore
from src.worker import Worker
from src.workflow_session import WorkflowSession, WorkflowStage


def _worker_with_handlers():
    def ok(arguments):
        return {"ok": True}

    return Worker(
        {
            "analyze_drawing": ok,
            "match_profile": ok,
            "read_dxf": ok,
            "audit_drawing": ok,
            "assign_numbers": ok,
            "write_numbered_dxf": ok,
            "confirm_numbering": ok,
            "nest_parts": ok,
            "postprocess": ok,
            "write_dxf": ok,
            "write_report": ok,
        }
    )


def test_agent_loop_reaches_numbering_confirmation_and_persists(tmp_path):
    store = WorkflowSessionStore(tmp_path)
    session = WorkflowSession(session_id="agent-loop-1")
    provider = StaticPlanProvider(
        [
            [
                ToolCall("analyze_drawing", {"path": "a.dxf"}),
                ToolCall("match_profile", {"path": "a.dxf"}),
                ToolCall("read_dxf", {"path": "a.dxf"}),
                ToolCall("audit_drawing", {"path": "a.dxf"}),
                ToolCall("assign_numbers", {"path": "a.dxf"}),
                ToolCall("write_numbered_dxf", {"path": "numbered.dxf"}),
            ]
        ]
    )
    loop = AgentLoop(session, _worker_with_handlers(), provider, store=store)

    result = loop.run_next()

    assert result.confirmation_required is True
    assert session.stage == WorkflowStage.AUDITED
    assert store.exists(session.session_id)
    loaded = store.load(session.session_id)
    assert loaded.stage == WorkflowStage.AUDITED


def test_agent_loop_can_resume_from_persisted_session(tmp_path):
    store = WorkflowSessionStore(tmp_path)
    session = WorkflowSession(session_id="agent-loop-2")
    provider = StaticPlanProvider(
        [
            [
                ToolCall("analyze_drawing", {"path": "a.dxf"}),
                ToolCall("match_profile", {"path": "a.dxf"}),
                ToolCall("read_dxf", {"path": "a.dxf"}),
                ToolCall("audit_drawing", {"path": "a.dxf"}),
                ToolCall("assign_numbers", {"path": "a.dxf"}),
                ToolCall("write_numbered_dxf", {"path": "numbered.dxf"}),
            ],
            [
                ToolCall("confirm_numbering", {"decision": "confirmed"}),
                ToolCall("nest_parts", {"sheet_width": 1000, "sheet_height": 800}),
            ],
        ]
    )
    loop = AgentLoop(session, _worker_with_handlers(), provider, store=store)
    loop.run_next()

    loaded_session = store.load(session.session_id)
    resumed_loop = AgentLoop(
        loaded_session,
        _worker_with_handlers(),
        provider,
        store=store,
    )
    resumed_loop.run_next()

    assert loaded_session.stage == WorkflowStage.NESTED_REPORTED
