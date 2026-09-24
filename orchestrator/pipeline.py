from __future__ import annotations

import dataclasses
import hashlib
import json
import time

from orchestrator.adapters.protocols import NullTracing
from orchestrator.errors import FingerprintMismatch, InvalidTransition, NodeContractViolation
from orchestrator.fingerprint import verify_fingerprint
from orchestrator.state import LIFECYCLE, RunState, apply_node_output, from_json, node_owned_json
from orchestrator.status import transition

# Display labels for the Trace page (CLAUDE.md §9C non-blocking item; the UI's
# trace_event_row renders `stage` as plain text). A node name not listed here (there
# shouldn't be one, but a Skill's custom.py primitive could in principle add one) falls
# back to the raw node name rather than raising.
NODE_STAGE_LABELS: dict[str, str] = {
    "discover": "Source data",
    "profile": "Data profiling",
    "plan": "Plan",
    "execute": "Tests",
    "classify": "Exceptions",
    "find": "Findings",
    "prioritise": "Insights",
    "act": "Actions",
    "export": "Exports",
}

# node_attempts outcome/event_type -> the Trace page's status vocabulary
# (reference_app/src/platform/components.py's _TRACE_STATUS_STYLE: complete/running/
# pending/failed/awaiting_confirmation). node_started is the node currently executing,
# so it maps to 'running', not a bespoke 'started'.
_NODE_EVENT_STATUS: dict[str, str] = {
    "node_started": "running",
    "node_completed": "complete",
    "node_failed": "failed",
}


def _node_event_id(execution_key: str, event_type: str) -> str:
    return hashlib.sha256(f"{execution_key}:{event_type}".encode("utf-8")).hexdigest()[:32]


def _node_completed_message(node_name: str, events: list[dict]) -> str:
    # Independent review 2026-09-24 (CLAUDE.md §9B scenario 8 / §4.2's node
    # table, whose "Event" column names a specific label per node, e.g.
    # act -> "Actions drafted"): every node in orchestrator/nodes/
    # fieldwork.py already appends its own specific, informative message to
    # RunState.events (population counts, "N drafted" vs "not generated
    # (option off)", etc.) -- use it for the persisted node_completed
    # trace_events row instead of the generic "<node> completed"
    # placeholder, which discarded it. Falls back to the placeholder only
    # if the node genuinely appended nothing (defensive, not expected for
    # any real fieldwork node).
    if events and events[-1].get("node") == node_name:
        return events[-1].get("message") or f"{node_name} completed"
    return f"{node_name} completed"


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
            "stage": NODE_STAGE_LABELS.get(attempt["node_name"], attempt["node_name"]),
            "status": _NODE_EVENT_STATUS.get(event_type, event_type),
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


def _tracing_unavailable_event_id(run_id: str) -> str:
    # Keyed on run_id ONLY (never state_version) so this is written at most
    # once per run regardless of how many executor passes or node attempts
    # see tracing unavailable -- `append_trace_event` is INSERT OR IGNORE on
    # event_id (CLAUDE.md §2.3 rule 4 / non-negotiable 13: report it, don't
    # spam it, and never let it slow down or fail node execution).
    return hashlib.sha256(f"{run_id}:tracing_unavailable".encode("utf-8")).hexdigest()[:32]


def _note_tracing_unavailable(persistence, state: RunState, *, reason: str, now: str) -> None:
    persistence.append_trace_event(
        {
            "event_id": _tracing_unavailable_event_id(state.run_id),
            "run_id": state.run_id,
            "engagement_id": state.engagement_id,
            "event_type": "tracing_unavailable",
            "event_time": now,
            "stage": "Tracing",
            "status": state.status,
            "message": f"MLflow tracing unavailable -- run proceeding without it: {reason}",
            "duration_s": None,
            "node_name": None,
            "execution_key": None,
            "actor": "tracing",
            "state_version": state.state_version,
        }
    )


def _start_pipeline_trace(tracing, persistence, state: RunState, *, current_fingerprint: dict | None, now: str) -> None:
    # One MLflow run per pipeline run (CLAUDE.md §2.3), never per executor
    # pass -- start_run is idempotent per run_id. Never allowed to affect the
    # audit result: any failure here is caught and reported as a single
    # trace_event, never raised into the node loop.
    try:
        tracing.start_run(
            state.run_id,
            params={
                "run_id": state.run_id,
                "skill_id": state.skill_id,
                "skill_version": state.skill_version,
                "fingerprint_id": state.fingerprint_id,
                "code_revision": (current_fingerprint or {}).get("code_revision"),
            },
        )
    except Exception as exc:
        _note_tracing_unavailable(persistence, state, reason=repr(exc), now=now)
        return
    if not tracing.available:
        _note_tracing_unavailable(persistence, state, reason=tracing.unavailable_reason or "unknown", now=now)


def _start_node_span(tracing, persistence, state: RunState, *, node_name: str, now: str) -> str:
    try:
        return tracing.start_span(run_id=state.run_id, node_name=node_name) or ""
    except Exception as exc:  # never let a tracing failure fail or slow the node
        _note_tracing_unavailable(persistence, state, reason=repr(exc), now=now)
        return ""


def _end_node_span(tracing, span_id: str, *, outcome: str, attributes: dict | None = None) -> None:
    if not span_id:
        return
    try:
        tracing.end_span(span_id, outcome=outcome, attributes=attributes)
    except Exception:
        pass  # CLAUDE.md §2.3: tracing never changes the audit result


def _end_pipeline_trace(tracing, run_id: str, *, status: str) -> None:
    # P3 gap-audit review: the parent MLflow run (start_pipeline_trace) must
    # reach a terminal state -- at every RunState terminal transition
    # (completed/failed/interrupted) AND at each HITL pause gate
    # (awaiting_confirmation/awaiting_signoff), since neither pause status is
    # revisited by this executor pass again. Never allowed to affect the
    # audit result (CLAUDE.md §2.3 rule 4): any failure here is swallowed,
    # exactly like _end_node_span.
    try:
        tracing.end_run(run_id, status=status)
    except Exception:
        pass


def _check_lifecycle_unchanged(state: RunState, result: RunState) -> None:
    # A node may only write NODE_OWNED fields (CLAUDE.md §4.1, B1) -- the pipeline loop
    # enforces this rather than trusting it, comparing every LIFECYCLE field between
    # the state a node was handed and the state it returned.
    changed = [
        name for name in LIFECYCLE
        if getattr(state, name) != getattr(result, name)
    ]
    if changed:
        raise NodeContractViolation(
            f"node returned changed lifecycle field(s) {sorted(changed)!r}; a node may "
            f"only write NODE_OWNED fields, lifecycle fields belong to the pipeline"
        )


def _select_recovered_attempt(attempts: list[dict], *, node_name: str, phase: str, phase_epoch: int, node_index: int) -> dict | None:
    # Recovery match (CLAUDE.md §9C/B2): same run/phase/phase_epoch/node_name/node_index,
    # outcome succeeded, highest attempt_number -- never ordered by a caller-supplied
    # timestamp, which is not trustworthy across a crash/restart.
    matching = [
        a for a in attempts
        if a["node_name"] == node_name
        and a["phase"] == phase
        and a.get("phase_epoch") == phase_epoch
        and a["node_index"] == node_index
        and a["outcome"] == "succeeded"
    ]
    if not matching:
        return None
    return max(matching, key=lambda a: a["attempt_number"])


def run_phase(
    persistence, run_id: str, *, nodes_for: dict, skill=None, clock, worker_alive=None,
    current_fingerprint: dict, tracing=None,
) -> RunState:
    state = persistence.load_state(run_id)
    tracing = tracing or NullTracing()

    if state.status == "queued":
        # Fingerprint verification happens on EVERY executor pass, not just the first
        # (CLAUDE.md §3 non-negotiable 8, §9C non-blocking item): a run must never
        # execute a node against a setup that has drifted from the one it was created
        # under. A mismatch fails the run outright -- it never runs a node.
        stored_fingerprint = persistence.get_fingerprint(state.fingerprint_id)
        try:
            verify_fingerprint(stored_fingerprint, current_fingerprint)
        except FingerprintMismatch as exc:
            now_fail = clock()
            failed_state = transition(
                state, "failed", now=now_fail,
                reason=f"fingerprint mismatch, run never executed: {sorted(exc.differing_fields)}",
            )
            saved = persistence.save_state(failed_state)
            _end_pipeline_trace(tracing, run_id, status="FAILED")
            return saved
        state = _transition_and_save(persistence, state, "running", now=clock())

    if state.status != "running":
        raise InvalidTransition(state.status, "running", detail="run_phase requires the run to be queued or running")

    _start_pipeline_trace(tracing, persistence, state, current_fingerprint=current_fingerprint, now=clock())

    while True:
        nodes = nodes_for[state.run_kind][state.phase]
        idx = state.next_node_index

        while idx < len(nodes):
            if worker_alive is not None and not worker_alive():
                return state

            node_name, fn = nodes[idx]
            attempts = persistence.list_node_attempts(run_id)
            recovered_attempt = _select_recovered_attempt(
                attempts, node_name=node_name, phase=state.phase, phase_epoch=state.phase_epoch, node_index=idx,
            )

            if recovered_attempt is not None:
                # Write-ahead recovery: the node already ran and its attempt was durably
                # recorded as succeeded, but the authoritative run_state was never
                # advanced past it (crash between complete_node_attempt and save_state).
                # The loop invariant idx == state.next_node_index at this point is what
                # tells us this attempt has not yet been incorporated -- do not
                # re-execute fn.
                owned = json.loads(recovered_attempt["result_state_json"])
                recovered = apply_node_output(state, owned)
                recovered = dataclasses.replace(
                    recovered, next_node_index=idx + 1, current_node_attempt_id=recovered_attempt["attempt_id"],
                )
                state = persistence.save_state(recovered)
                _emit_node_event(
                    persistence, state, recovered_attempt, event_type="node_completed",
                    message=_node_completed_message(node_name, recovered.events),
                    now=recovered_attempt.get("completed_at") or clock(),
                )
                idx += 1
                continue

            # current_node_attempt_id is set below on completion (succeeded or failed),
            # not here on start: "currently executing" is already observable as the
            # open (outcome IS NULL) row this begin_node_attempt call creates, so
            # setting it here too would just be an extra CAS write per node for no new
            # information (CLAUDE.md §9C/B1).
            now_start = clock()
            attempt = persistence.begin_node_attempt(
                run_id=run_id,
                phase=state.phase,
                phase_epoch=state.phase_epoch,
                node_index=idx,
                node_name=node_name,
                state_version_before=state.state_version,
                now=now_start,
            )
            _emit_node_event(persistence, state, attempt, event_type="node_started", message=f"{node_name} started", now=now_start)

            span_id = _start_node_span(tracing, persistence, state, node_name=node_name, now=now_start)
            started_at = time.monotonic()
            try:
                result = fn(skill, state)
                _check_lifecycle_unchanged(state, result)
                # apply_node_output's own append-only check (events/errors must extend,
                # never rewrite) is a NodeContractViolation exactly like a bad lifecycle
                # write -- computed inside this same try so either is a node failure,
                # not an uncaught exception out of run_phase.
                new_state = apply_node_output(state, result)
            except Exception as exc:
                duration = time.monotonic() - started_at
                now_fail = clock()
                persistence.complete_node_attempt(
                    attempt["execution_key"], outcome="failed", now=now_fail, error_detail=repr(exc)
                )
                _end_node_span(
                    tracing, span_id, outcome="failed",
                    attributes={"attempt_number": attempt.get("attempt_number"), "duration_s": duration, "error": repr(exc)},
                )
                failed_state = transition(state, "failed", now=now_fail, reason=f"node {node_name!r} failed: {exc!r}")
                failed_state = dataclasses.replace(failed_state, current_node_attempt_id=attempt["attempt_id"])
                state = persistence.save_state(failed_state)
                _emit_node_event(
                    persistence, state, attempt, event_type="node_failed",
                    message=f"{node_name} failed: {exc!r}", now=now_fail, duration_s=duration,
                )
                _end_pipeline_trace(tracing, run_id, status="FAILED")
                return state

            duration = time.monotonic() - started_at
            _end_node_span(
                tracing, span_id, outcome="succeeded",
                attributes={"attempt_number": attempt.get("attempt_number"), "duration_s": duration},
            )
            now_end = clock()
            new_state = dataclasses.replace(
                new_state, next_node_index=idx + 1, current_node_attempt_id=attempt["attempt_id"],
            )
            persistence.complete_node_attempt(
                attempt["execution_key"],
                outcome="succeeded",
                now=now_end,
                state_version_after=state.state_version + 1,
                result_state_json=node_owned_json(new_state),
            )
            state = persistence.save_state(new_state)
            _emit_node_event(
                persistence, state, attempt, event_type="node_completed",
                message=_node_completed_message(node_name, new_state.events), now=now_end, duration_s=duration,
            )
            idx += 1

        # Every node in this phase has run. Decide the phase-boundary transition.
        if state.phase == "plan":
            auto_confirm = state.mode == "playbook" and bool(state.options.get("auto_confirm_plan"))
            if not auto_confirm:
                state = _transition_and_save(persistence, state, "awaiting_confirmation", now=clock())
                # A HITL pause gate (CLAUDE.md §2.4): this executor pass is done, and
                # confirm_plan's own resumed pass will start a fresh MLflow parent run
                # lookup that finds and reuses this same one (start_run's idempotent
                # tag search) -- never left dangling RUNNING for however long a human
                # takes to confirm (P3 gap-audit review).
                _end_pipeline_trace(tracing, run_id, status="FINISHED")
                return state
            # The plan->execute gate (status.py) accepts this via the auto-confirm
            # clause, but plan_confirmed is still set explicitly here so the audit
            # trail on `state` reflects that the plan WAS confirmed, not silently
            # skipped (CLAUDE.md §2.4, B3).
            state = dataclasses.replace(state, plan_confirmed=True)
            state = _transition_and_save(persistence, state, "running", now=clock(), phase="execute")
            continue
        if state.phase == "execute":
            state = _transition_and_save(persistence, state, "awaiting_signoff", now=clock())
            _end_pipeline_trace(tracing, run_id, status="FINISHED")  # HITL pause gate, as above
            return state
        if state.phase == "export":
            state = _transition_and_save(persistence, state, "completed", now=clock())
            _end_pipeline_trace(tracing, run_id, status="FINISHED")
            return state

        raise ValueError(f"unknown phase {state.phase!r}")
