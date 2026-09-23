from __future__ import annotations

import pytest

from orchestrator.errors import InvalidTransition
from orchestrator.state import STATUSES, RunState
from orchestrator.status import ALLOWED_TRANSITIONS, transition

_PHASE_FOR_STATUS = {
    "awaiting_confirmation": "plan",
    "awaiting_signoff": "execute",
    "completed": "export",
}


def _state(status: str, phase: str = "plan") -> RunState:
    return RunState(
        run_id="R",
        run_kind="fieldwork",
        mode="playbook",
        phase=phase,
        audit_period=("2026-01-01", "2026-01-31"),
        objective="o",
        run_owner="alice",
        fingerprint_id="F",
        created_at="t0",
        last_state_change_at="t0",
        status=status,
        engagement_id="E1",
        state_version=3,
    )


def _valid_phase_for(status: str, current_phase: str) -> str:
    return _PHASE_FOR_STATUS.get(status, current_phase)


@pytest.mark.parametrize("from_status", STATUSES)
@pytest.mark.parametrize("to_status", STATUSES)
def test_transition_matrix(from_status, to_status):
    allowed = ALLOWED_TRANSITIONS[from_status]
    current_phase = "execute" if from_status == "running" else _PHASE_FOR_STATUS.get(from_status, "plan")
    state = _state(from_status, phase=current_phase)

    if to_status not in allowed:
        with pytest.raises(InvalidTransition):
            transition(state, to_status, now="t1")
        return

    if from_status == "running" and to_status == "running":
        # requires an explicit phase change
        with pytest.raises(InvalidTransition):
            transition(state, to_status, now="t1")
        new_phase = "export" if current_phase != "export" else "execute"
        result = transition(state, to_status, now="t1", phase=new_phase)
        assert result.phase == new_phase
        assert result.next_node_index == 0
        return

    target_phase = _valid_phase_for(to_status, current_phase)
    result = transition(state, to_status, now="t1", phase=target_phase if target_phase != current_phase else None)
    assert result.status == to_status


def test_running_to_running_same_phase_rejected():
    state = _state("running", phase="plan")
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1")
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1", phase="plan")


def test_running_to_running_phase_change_resets_next_node_index():
    import dataclasses

    state = _state("running", phase="plan")
    state = dataclasses.replace(state, next_node_index=5)
    result = transition(state, "running", now="t1", phase="execute")
    assert result.phase == "execute"
    assert result.next_node_index == 0


def test_awaiting_confirmation_requires_plan_phase():
    state = _state("running", phase="execute")
    with pytest.raises(InvalidTransition):
        transition(state, "awaiting_confirmation", now="t1")


def test_awaiting_signoff_requires_execute_phase():
    state = _state("running", phase="plan")
    with pytest.raises(InvalidTransition):
        transition(state, "awaiting_signoff", now="t1")


def test_completed_requires_export_phase():
    state = _state("running", phase="execute")
    with pytest.raises(InvalidTransition):
        transition(state, "completed", now="t1")


def test_started_at_set_on_first_entry_to_running():
    state = _state("queued", phase="plan")
    assert state.started_at is None
    result = transition(state, "running", now="t1")
    assert result.started_at == "t1"


def test_started_at_not_overwritten_on_subsequent_running():
    import dataclasses

    state = _state("queued", phase="plan")
    first = transition(state, "running", now="t1")
    second_input = dataclasses.replace(first, status="running")
    result = transition(second_input, "running", now="t9", phase="execute")
    assert result.started_at == "t1"


def test_completed_at_set_on_completed_and_failed():
    state = _state("running", phase="export")
    result = transition(state, "completed", now="t5")
    assert result.completed_at == "t5"

    state2 = _state("running", phase="plan")
    result2 = transition(state2, "failed", now="t6", reason="boom")
    assert result2.completed_at == "t6"
    assert result2.status_reason == "boom"


def test_last_state_change_at_updated():
    state = _state("queued", phase="plan")
    result = transition(state, "running", now="t42")
    assert result.last_state_change_at == "t42"


def test_terminal_statuses_have_no_outgoing_transitions():
    for terminal in ("completed", "failed"):
        assert ALLOWED_TRANSITIONS[terminal] == set()
