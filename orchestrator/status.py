from __future__ import annotations

import dataclasses
from typing import Callable

from orchestrator.errors import InvalidTransition
from orchestrator.state import RunState

# Allowed status transitions (CLAUDE.md §2.4, §9C). running -> running is allowed
# only as a phase change (e.g. playbook auto-confirm plan -> execute with no gate).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "failed"},
    "running": {
        "running",
        "awaiting_confirmation",
        "awaiting_signoff",
        "completed",
        "failed",
        "interrupted",
    },
    "awaiting_confirmation": {"queued", "failed"},
    "awaiting_signoff": {"queued", "failed"},
    "interrupted": {"queued"},
    "completed": set(),
    "failed": set(),
}

_PHASE_REQUIRED_FOR_STATUS: dict[str, str] = {
    "awaiting_confirmation": "plan",
    "awaiting_signoff": "execute",
    "completed": "export",
}

# CLAUDE.md §2.4 / NN3: the only two phase changes a run may ever make, each gated on
# the human decision that authorises it. `state` here is the state passed INTO
# transition() -- confirm_plan/sign_off set plan_confirmed/signoff on it (via
# dataclasses.replace) before calling transition(), so the gate sees the field it
# needs. Explorer mode can never satisfy the auto-confirm clause because it checks
# `mode == "playbook"` explicitly -- Explorer can only pass via plan_confirmed, which
# only the explicit confirm_plan() call sets.
def _has_run_inputs(state: RunState) -> bool:
    """Independent review 2026-09-25 item 1 ("run inputs"): a run with any
    declared column mapping, parameter override or unsupplied source makes
    plan confirmation mandatory even in Playbook -- the auditor confirms
    what they can see (docs/specs/P7_mapping_authoring_design.md §1.3
    "Mandatory plan confirmation"). start_audit_run also forces
    auto_confirm_plan=False whenever this is true, so the state machine
    enforces it independently of what the caller passed."""
    run_inputs = (state.options or {}).get("run_inputs") or {}
    return bool(run_inputs.get("mappings") or run_inputs.get("not_supplied") or run_inputs.get("parameters"))


def _signoff_gate(s: RunState) -> bool:
    if s.signoff is None:
        return False
    # P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.4):
    # a LEGACY signoff (no 'sod_mode' key -- every pre-P7 sign_off() call,
    # orchestrator.runs.sign_off's non-gated path) passes exactly as it did
    # before this feature existed: `signoff is not None` alone was already
    # sufficient. A P7-gated signoff always carries 'sod_mode', and passes
    # only when it is 'labelled' (SoD waived, labelled) or both
    # prepared_by/reviewed_by are recorded -- defence in depth: orchestrator.
    # runs.sign_off's gated path already enforces this before it ever sets
    # `signoff` at all, so this only matters if that path were ever bypassed.
    sod_mode = s.signoff.get("sod_mode")
    if sod_mode is None or sod_mode == "labelled":
        return True
    return bool(s.signoff.get("prepared_by") and s.signoff.get("reviewed_by"))


_PHASE_CHANGE_GATES: dict[tuple[str, str], Callable[[RunState], bool]] = {
    ("plan", "execute"): lambda s: bool(
        s.plan_confirmed
        or (s.mode == "playbook" and bool(s.options.get("auto_confirm_plan")) and not _has_run_inputs(s))
    ),
    ("execute", "export"): _signoff_gate,
}


def transition(
    state: RunState,
    to_status: str,
    *,
    now: str,
    reason: str | None = None,
    phase: str | None = None,
    restart_at_index: int | None = None,
) -> RunState:
    from_status = state.status
    allowed = ALLOWED_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise InvalidTransition(from_status, to_status)

    new_phase = phase if phase is not None else state.phase

    if from_status == "running" and to_status == "running" and new_phase == state.phase:
        raise InvalidTransition(
            from_status, to_status, detail="running->running requires a phase change"
        )

    if new_phase != state.phase:
        if from_status == "interrupted":
            # A resume must never smuggle a phase change past a gate -- it continues
            # exactly where the run was interrupted, nothing more (CLAUDE.md §9C).
            raise InvalidTransition(
                from_status, to_status, detail="interrupted->queued must not change phase"
            )
        gate = _PHASE_CHANGE_GATES.get((state.phase, new_phase))
        if gate is None:
            raise InvalidTransition(
                from_status,
                to_status,
                detail=f"phase change {state.phase!r} -> {new_phase!r} is not permitted",
            )
        if not gate(state):
            raise InvalidTransition(
                from_status,
                to_status,
                detail=f"phase change {state.phase!r} -> {new_phase!r} blocked: gate condition not satisfied",
            )

    required_phase = _PHASE_REQUIRED_FOR_STATUS.get(to_status)
    if required_phase is not None and new_phase != required_phase:
        raise InvalidTransition(
            from_status,
            to_status,
            detail=f"requires phase={required_phase!r}, got {new_phase!r}",
        )

    updates: dict = {
        "status": to_status,
        "phase": new_phase,
        "last_state_change_at": now,
        "status_reason": reason,
    }
    if new_phase != state.phase:
        updates["next_node_index"] = 0
        updates["phase_epoch"] = state.state_version + 1
    if restart_at_index is not None:
        # P6 WP N9 (docs/specs/P6_narration_design.md §5.5 Regenerate): a
        # same-phase "re-enter this phase from node N" transition -- unlike
        # an ordinary phase change, next_node_index is set to the given
        # index rather than reset to 0, but phase_epoch still advances so
        # node execution keys (run_id:phase:phase_epoch:node_name:attempt,
        # CLAUDE.md §4.1) are fresh and a replayed prior attempt is never
        # mistaken for this generation's.
        if restart_at_index < 0:
            raise ValueError(f"restart_at_index must be >= 0, got {restart_at_index!r}")
        updates["next_node_index"] = restart_at_index
        updates["phase_epoch"] = state.state_version + 1
    if to_status == "running" and state.started_at is None:
        updates["started_at"] = now
    if to_status in ("completed", "failed"):
        updates["completed_at"] = now

    return dataclasses.replace(state, **updates)
