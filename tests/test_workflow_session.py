import pytest

from src.workflow_session import WorkflowSession, WorkflowStage


def _session():
    return WorkflowSession(session_id="job-1")


def test_valid_forward_flow_records_history():
    session = _session()
    for stage in WorkflowStage:
        if stage in {WorkflowStage.BLOCKED, WorkflowStage.CANCELLED}:
            continue
        if stage == WorkflowStage.UPLOADED:
            continue
        session.transition(stage)

    assert session.current_stage == "completed"
    assert len(session.history) == 8
    assert session.history[-1].stage == "completed"


def test_invalid_forward_transition_is_rejected():
    session = _session()
    with pytest.raises(ValueError):
        session.transition("read")


def test_backtrack_returns_to_earlier_stage():
    session = _session()
    session.transition("analyzed")
    session.transition("profile_matched")

    session.backtrack("analyzed")

    assert session.current_stage == "analyzed"
    assert session.history[-1].summary["backtracked_from"] == "profile_matched"


def test_block_resume_round_trip():
    session = _session()
    session.transition("analyzed")

    session.block("编号图层缺失")

    assert session.current_stage == "blocked"
    with pytest.raises(ValueError):
        session.transition("profile_matched")

    session.resume("profile_matched")
    assert session.current_stage == "profile_matched"


def test_cancel_terminal_session():
    session = _session()
    session.cancel("用户取消")

    assert session.current_stage == "cancelled"
    with pytest.raises(ValueError):
        session.transition("analyzed")
