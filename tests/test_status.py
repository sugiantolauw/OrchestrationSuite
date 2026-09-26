from __future__ import annotations

import dataclasses

import pytest

from orchestrator.errors import InvalidTransition
from orchestrator.state import STATUSES, RunState
from orchestrator.status import ALLOWED_TRANSITIONS, _PHASE_CHANGE_GATES, _PHASE_REQUIRED_FOR_STATUS, transition

_PHASE_FOR_STATUS = {
    "awaiting_confirmation": "plan",
    "awaiting_signoff": "execute",
    "completed": "export",
}


def _state(status: str, phase: str = "plan", *, mode: str = "playbook", plan_confirmed: bool = False,
           signoff: dict | None = None, options: dict | None = None, state_version: int = 3) -> RunState:
    return RunState(
        run_id="R",
        run_kind="fieldwork",
        mode=mode,
        phase=phase,
        audit_period=("2026-01-01", "2026-01-31"),
        objective="o",
        run_owner="alice",
        fingerprint_id="F",
        created_at="t0",
        last_state_change_at="t0",
        status=status,
        engagement_id="E1",
        state_version=state_version,
        plan_confirmed=plan_confirmed,
        signoff=signoff,
        options=options or {},
    )


# ── status-level ALLOWED_TRANSITIONS: every disallowed (from, to) pair rejects ──────────────
#
# This only tests the status allow-list, holding phase unchanged throughout (using
# whatever phase `to_status` requires, or the current phase when nothing is required).
# Phase-CHANGE legality (the gates) is a separate concern, tested below -- folding both
# into one generic matrix is what let the original version of this test paper over the
# gate bypasses an independent review found (B3).


def _phase_for_unchanged_transition(from_status: str, to_status: str) -> str:
    if to_status in _PHASE_FOR_STATUS:
        return _PHASE_FOR_STATUS[to_status]
    if from_status in _PHASE_FOR_STATUS:
        return _PHASE_FOR_STATUS[from_status]
    return "plan"


@pytest.mark.parametrize("from_status", STATUSES)
@pytest.mark.parametrize("to_status", STATUSES)
def test_disallowed_status_transitions_always_rejected(from_status, to_status):
    if from_status == "running" and to_status == "running":
        pytest.skip("running->running is a phase-change transition, covered below")
    allowed = ALLOWED_TRANSITIONS[from_status]
    phase = _phase_for_unchanged_transition(from_status, to_status)
    state = _state(from_status, phase=phase)
    if to_status not in allowed:
        with pytest.raises(InvalidTransition):
            transition(state, to_status, now="t1")


@pytest.mark.parametrize("from_status,to_status", [
    ("queued", "running"),
    ("queued", "failed"),
    ("running", "awaiting_confirmation"),
    ("running", "awaiting_signoff"),
    ("running", "completed"),
    ("running", "failed"),
    ("running", "interrupted"),
    ("awaiting_confirmation", "failed"),
    ("awaiting_signoff", "failed"),
])
def test_allowed_transitions_with_no_phase_change_succeed(from_status, to_status):
    phase = _phase_for_unchanged_transition(from_status, to_status)
    state = _state(from_status, phase=phase)
    result = transition(state, to_status, now="t1")
    assert result.status == to_status
    assert result.phase == phase
    assert result.phase_epoch == state.phase_epoch  # unchanged: no phase change


def test_terminal_statuses_have_no_outgoing_transitions():
    for terminal in ("completed", "failed"):
        assert ALLOWED_TRANSITIONS[terminal] == set()


# ── phase-change gates (B3 / NN3) ────────────────────────────────────────────────────────


def test_plan_to_execute_allowed_when_plan_confirmed():
    state = _state("running", phase="plan", plan_confirmed=True)
    result = transition(state, "running", now="t1", phase="execute")
    assert result.phase == "execute"
    assert result.next_node_index == 0
    assert result.phase_epoch == state.state_version + 1


def test_plan_to_execute_allowed_for_playbook_auto_confirm():
    state = _state("running", phase="plan", plan_confirmed=False, mode="playbook",
                    options={"auto_confirm_plan": True})
    result = transition(state, "running", now="t1", phase="execute")
    assert result.phase == "execute"


def test_plan_to_execute_blocked_for_playbook_auto_confirm_with_run_inputs():
    # Independent review 2026-09-25 item 1 ("run inputs" -- docs/specs/
    # P7_mapping_authoring_design.md §1.3 "Mandatory plan confirmation"): a
    # run with a declared column mapping makes confirmation mandatory even
    # in Playbook with auto_confirm_plan set.
    state = _state(
        "running", phase="plan", plan_confirmed=False, mode="playbook",
        options={"auto_confirm_plan": True, "run_inputs": {"mappings": {"expense_report": {"a": "b"}}}},
    )
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1", phase="execute")


def test_plan_to_execute_blocked_for_playbook_auto_confirm_with_not_supplied():
    state = _state(
        "running", phase="plan", plan_confirmed=False, mode="playbook",
        options={"auto_confirm_plan": True, "run_inputs": {"not_supplied": {"booking_detail": {"reason": "x"}}}},
    )
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1", phase="execute")


def test_plan_to_execute_allowed_for_playbook_auto_confirm_with_empty_run_inputs():
    # An empty run_inputs dict (present but nothing declared) never trips the gate.
    state = _state(
        "running", phase="plan", plan_confirmed=False, mode="playbook",
        options={"auto_confirm_plan": True, "run_inputs": {"mappings": {}, "not_supplied": {}, "parameters": {}}},
    )
    result = transition(state, "running", now="t1", phase="execute")
    assert result.phase == "execute"


def test_plan_to_execute_allowed_for_playbook_with_run_inputs_when_explicitly_confirmed():
    state = _state(
        "running", phase="plan", plan_confirmed=True, mode="playbook",
        options={"auto_confirm_plan": True, "run_inputs": {"mappings": {"expense_report": {"a": "b"}}}},
    )
    result = transition(state, "running", now="t1", phase="execute")
    assert result.phase == "execute"


def test_plan_to_execute_blocked_without_confirmation_or_auto_confirm():
    state = _state("running", phase="plan", plan_confirmed=False, mode="playbook", options={})
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1", phase="execute")


def test_explorer_plan_to_execute_never_auto_confirms():
    # Explorer can never auto-confirm (CLAUDE.md §2.4) -- even with the auto_confirm_plan
    # option set (which should never happen for Explorer, but the gate must not trust
    # that), only an explicit plan_confirmed=True can pass it.
    state = _state("running", phase="plan", plan_confirmed=False, mode="explorer",
                    options={"auto_confirm_plan": True})
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1", phase="execute")


def test_explorer_plan_to_execute_allowed_once_explicitly_confirmed():
    state = _state("running", phase="plan", plan_confirmed=True, mode="explorer")
    result = transition(state, "running", now="t1", phase="execute")
    assert result.phase == "execute"


def test_execute_to_export_allowed_when_signed_off():
    state = _state("running", phase="execute", signoff={"approver": "alice", "timestamp": "t0"})
    result = transition(state, "running", now="t1", phase="export")
    assert result.phase == "export"
    assert result.phase_epoch == state.state_version + 1


def test_execute_to_export_blocked_without_signoff():
    state = _state("running", phase="execute", signoff=None)
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1", phase="export")


@pytest.mark.parametrize("from_phase,to_phase", [
    ("plan", "export"),
    ("execute", "plan"),
    ("export", "plan"),
    ("export", "execute"),
])
def test_every_other_phase_change_is_rejected_outright(from_phase, to_phase):
    state = _state(
        "running", phase=from_phase,
        plan_confirmed=True, signoff={"approver": "alice", "timestamp": "t0"},  # gates satisfied, still must reject
    )
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1", phase=to_phase)


def test_interrupted_to_queued_must_not_change_phase():
    state = _state("interrupted", phase="execute")
    result = transition(state, "queued", now="t1")  # no phase kwarg -> stays 'execute'
    assert result.phase == "execute"
    assert result.phase_epoch == state.phase_epoch


def test_interrupted_to_queued_with_explicit_phase_change_rejected():
    state = _state("interrupted", phase="execute", signoff={"approver": "alice", "timestamp": "t0"})
    with pytest.raises(InvalidTransition, match="must not change phase"):
        transition(state, "queued", now="t1", phase="export")


def test_running_to_queued_is_rejected_a_still_running_run_cannot_be_resumed():
    """Round-4A live test 4.18 ('restart mid-run') observed a fresh
    instance apparently continuing an orphaned run's execution with no
    run ever passing through an explicit `interrupted` state. Traced: the
    documented path (orchestrator.reaper/executor.reap_orphaned_runs_with_leases,
    called at App start and on every admission-loop tick, CLAUDE.md §2.3
    rule 2 / §9C) only reaps a run whose LEASE has actually expired past
    its grace period -- a run restarted quickly, before that window
    elapses, correctly stays `running`, neither reaped nor re-admitted (it
    is not `queued`). orchestrator.runs.resume() (the only documented path
    to continue an orphaned run) calls exactly this transition
    (`"running" -> "queued"`, never called directly on an un-reaped run in
    the real service/executor code path) -- this asserts the state
    machine itself refuses it, which is what makes a genuine 'resume a
    still-running run without ever marking it interrupted' impossible
    through the documented API. If this test starts failing, that is the
    regression 4.18 was worried about; today it holds, so 4.18's
    observation is most likely its own test harness calling the executor
    directly rather than the real restart/reap/resume path (see the round's
    own 'Known limits' notes on other one-shot-script artifacts)."""
    state = _state("running", phase="execute")
    with pytest.raises(InvalidTransition):
        transition(state, "queued", now="t1")


def test_awaiting_confirmation_to_queued_requires_plan_confirmed_and_execute_phase():
    state = _state("awaiting_confirmation", phase="plan", plan_confirmed=True)
    result = transition(state, "queued", now="t1", phase="execute")
    assert result.phase == "execute"
    assert result.status == "queued"


def test_awaiting_confirmation_to_queued_without_confirmation_rejected():
    state = _state("awaiting_confirmation", phase="plan", plan_confirmed=False)
    with pytest.raises(InvalidTransition):
        transition(state, "queued", now="t1", phase="execute")


def test_awaiting_confirmation_to_queued_with_phase_export_rejected():
    # A bypass an independent review found: skipping straight from the plan gate to the
    # export phase must be rejected exactly like any other illegal phase change,
    # regardless of plan_confirmed.
    state = _state("awaiting_confirmation", phase="plan", plan_confirmed=True)
    with pytest.raises(InvalidTransition):
        transition(state, "queued", now="t1", phase="export")


def test_awaiting_signoff_to_queued_requires_signoff_and_export_phase():
    state = _state("awaiting_signoff", phase="execute", signoff={"approver": "alice", "timestamp": "t0"})
    result = transition(state, "queued", now="t1", phase="export")
    assert result.phase == "export"


def test_awaiting_signoff_to_queued_without_signoff_rejected():
    state = _state("awaiting_signoff", phase="execute", signoff=None)
    with pytest.raises(InvalidTransition):
        transition(state, "queued", now="t1", phase="export")


def test_running_execute_to_export_without_signoff_rejected():
    # Named bypass: a run in 'running'/'execute' must not reach 'export' without a
    # recorded sign-off, even via the running->running auto-transition path.
    state = _state("running", phase="execute", signoff=None)
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1", phase="export")


# ── running->running mechanics ───────────────────────────────────────────────────────────


def test_running_to_running_same_phase_rejected():
    state = _state("running", phase="plan", plan_confirmed=True)
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1")
    with pytest.raises(InvalidTransition):
        transition(state, "running", now="t1", phase="plan")


def test_running_to_running_phase_change_resets_next_node_index():
    state = _state("running", phase="plan", plan_confirmed=True)
    state = dataclasses.replace(state, next_node_index=5)
    result = transition(state, "running", now="t1", phase="execute")
    assert result.phase == "execute"
    assert result.next_node_index == 0


def test_phase_change_bumps_phase_epoch_to_state_version_plus_one():
    state = _state("running", phase="plan", plan_confirmed=True, state_version=7)
    result = transition(state, "running", now="t1", phase="execute")
    assert result.phase_epoch == 8


def test_no_phase_change_leaves_phase_epoch_untouched():
    state = _state("running", phase="plan", state_version=7)
    result = transition(state, "awaiting_confirmation", now="t1")
    assert result.phase_epoch == state.phase_epoch


# ── phase requirements for specific target statuses ─────────────────────────────────────


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


# ── timestamps ────────────────────────────────────────────────────────────────────────


def test_started_at_set_on_first_entry_to_running():
    state = _state("queued", phase="plan")
    assert state.started_at is None
    result = transition(state, "running", now="t1")
    assert result.started_at == "t1"


def test_started_at_not_overwritten_on_subsequent_running():
    state = _state("queued", phase="plan")
    first = transition(state, "running", now="t1")
    second_input = dataclasses.replace(first, status="running", plan_confirmed=True)
    result = transition(second_input, "running", now="t9", phase="execute")
    assert result.started_at == "t1"


def test_completed_at_set_on_completed_and_failed():
    state = _state("running", phase="export", signoff={"approver": "alice", "timestamp": "t0"})
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


# ── gate table sanity ────────────────────────────────────────────────────────────────────


def test_phase_change_gates_cover_exactly_plan_execute_and_execute_export():
    assert set(_PHASE_CHANGE_GATES.keys()) == {("plan", "execute"), ("execute", "export")}


def test_phase_required_for_status_table_unchanged():
    assert _PHASE_REQUIRED_FOR_STATUS == {
        "awaiting_confirmation": "plan",
        "awaiting_signoff": "execute",
        "completed": "export",
    }
