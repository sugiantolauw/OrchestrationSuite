from __future__ import annotations

import dataclasses
import hashlib
import time

from orchestrator.errors import InvalidTransition
from orchestrator.state import RunState, from_json, to_json
from orchestrator.status import transition


def _node_event_id(execution_key: str, event_type: str) -> str:
    return hashlib.sha256(f"{execution_key}:{event_type}".encode("utf-8")).hexdigest()[:32]


def _emit_node_event(
    persistence, state: RunState, attempt: dict, *, event_type: str, message: str, now: str, duration_s: float | None = None
) -> None:
    persistence.append_trace_event(
        {
            "event_id": _node_event_id(attempt["execution_key"], event_type),
            "run_id": state.run_id,
            "engagement_id": state.engagement_id,
            "event_type": event_type,
            "event_time": now,
            "stage": attempt["node_name"],
            "status": event_type.replace("node_", ""),
            "message": message,
            "duration_s": duration_s,
            "node_name": attempt["node_name"],
            "execution_key": attempt["execution_key"],
            "actor": "pipeline",
            "state_version": state.state_version,
        }
    )


def _transition_and_save(persistence, state: RunState, to_status: str, *, now: str, phase: str | None = None, reason: str | None = None) -> RunState:
    new_state = transition(state, to_status, now=now, phase=phase, reason=reason)
    return persistence.save_state(new_state)


def run_phase(persistence, run_id: str, *, nodes_for: dict, skill=None, clock, worker_alive=None) -> RunState:
    state = persistence.load_state(run_id)

    if state.status == "queued":
        state = _transition_and_save(persistence, state, "running", now=clock())

    if state.status != "running":
        raise InvalidTransition(state.status, "running", detail="run_phase requires the run to be queued or running")

    while True:
        nodes = nodes_for[state.run_kind][state.phase]
        idx = state.next_node_index

        while idx < len(nodes):
            if worker_alive is not None and not worker_alive():
                return state

            node_name, fn = nodes[idx]
            existing = [a for a in persistence.list_node_attempts(run_id) if a["node_name"] == node_name]
            latest = existing[-1] if existing else None

            if latest is not None and latest["outcome"] == "succeeded":
                # Write-ahead recovery: the node already ran and its attempt was durably
                # recorded as succeeded, but the authoritative run_state was never advanced
                # past it (crash between complete_node_attempt and save_state). The loop
                # invariant idx == state.next_node_index at this point is what tells us this
                # attempt has not yet been incorporated — do not re-execute fn.
                recovered = from_json(latest["result_state_json"])
                recovered = dataclasses.replace(recovered, state_version=state.state_version)
                state = persistence.save_state(recovered)
                idx += 1
                continue

            now_start = clock()
            attempt = persistence.begin_node_attempt(
                run_id=run_id,
                phase=state.phase,
                node_index=idx,
                node_name=node_name,
                state_version_before=state.state_version,
                now=now_start,
            )
            _emit_node_event(persistence, state, attempt, event_type="node_started", message=f"{node_name} started", now=now_start)

            started_at = time.monotonic()
            try:
                result = fn(skill, state)
            except Exception as exc:
                duration = time.monotonic() - started_at
                now_fail = clock()
                persistence.complete_node_attempt(
                    attempt["execution_key"], outcome="failed", now=now_fail, error_detail=repr(exc)
                )
                failed_state = transition(state, "failed", now=now_fail, reason=f"node {node_name!r} failed: {exc!r}")
                state = persistence.save_state(failed_state)
                _emit_node_event(
                    persistence, state, attempt, event_type="node_failed",
                    message=f"{node_name} failed: {exc!r}", now=now_fail, duration_s=duration,
                )
                return state

            duration = time.monotonic() - started_at
            now_end = clock()
            new_state = dataclasses.replace(result, next_node_index=idx + 1)
            persistence.complete_node_attempt(
                attempt["execution_key"],
                outcome="succeeded",
                now=now_end,
                state_version_after=state.state_version + 1,
                result_state_json=to_json(new_state),
            )
            state = persistence.save_state(new_state)
            _emit_node_event(
                persistence, state, attempt, event_type="node_completed",
                message=f"{node_name} completed", now=now_end, duration_s=duration,
            )
            idx += 1

        # Every node in this phase has run. Decide the phase-boundary transition.
        if state.phase == "plan":
            auto_confirm = state.mode == "playbook" and bool(state.options.get("auto_confirm_plan"))
            if not auto_confirm:
                return _transition_and_save(persistence, state, "awaiting_confirmation", now=clock())
            state = _transition_and_save(persistence, state, "running", now=clock(), phase="execute")
            continue
        if state.phase == "execute":
            return _transition_and_save(persistence, state, "awaiting_signoff", now=clock())
        if state.phase == "export":
            return _transition_and_save(persistence, state, "completed", now=clock())

        raise ValueError(f"unknown phase {state.phase!r}")
