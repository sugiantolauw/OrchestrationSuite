from __future__ import annotations

import hashlib

from orchestrator.errors import StaleStateError
from orchestrator.status import transition


def _trace_event_id(run_id: str, event_type: str, state_version_after: int | None) -> str:
    return hashlib.sha256(f"{run_id}:{event_type}:{state_version_after}".encode("utf-8")).hexdigest()[:32]


def reap_orphaned_runs(persistence, *, now: str, actor: str = "reaper") -> list[str]:
    reaped: list[str] = []
    for run_id in persistence.find_runs(["running"]):
        reason = f"Executor not alive at App start; run orphaned at {now}"
        persistence.close_open_attempts(run_id, outcome="interrupted", now=now, error_detail=reason)
        state = persistence.load_state(run_id)
        if state.status != "running":
            # someone else already moved it on (e.g. it finished between find_runs and load_state)
            continue
        new_state = transition(state, "interrupted", now=now, reason=reason)
        try:
            saved = persistence.save_state(new_state)
        except StaleStateError:
            continue
        persistence.append_trace_event(
            {
                "event_id": _trace_event_id(run_id, "run_interrupted", saved.state_version),
                "run_id": run_id,
                "engagement_id": saved.engagement_id,
                "event_type": "run_interrupted",
                "event_time": now,
                "stage": saved.phase,
                "status": saved.status,
                "message": reason,
                "duration_s": None,
                "node_name": None,
                "execution_key": None,
                "actor": actor,
                "state_version": saved.state_version,
            }
        )
        reaped.append(run_id)
    return reaped
