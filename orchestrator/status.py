from __future__ import annotations

import dataclasses

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


def transition(
    state: RunState,
    to_status: str,
    *,
    now: str,
    reason: str | None = None,
    phase: str | None = None,
) -> RunState:
    from_status = state.status
    allowed = ALLOWED_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise InvalidTransition(from_status, to_status)

    if from_status == "running" and to_status == "running":
        new_phase = phase if phase is not None else state.phase
        if new_phase == state.phase:
            raise InvalidTransition(
                from_status, to_status, detail="running->running requires a phase change"
            )
    else:
        new_phase = phase if phase is not None else state.phase

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
    if to_status == "running" and state.started_at is None:
        updates["started_at"] = now
    if to_status in ("completed", "failed"):
        updates["completed_at"] = now

    return dataclasses.replace(state, **updates)
