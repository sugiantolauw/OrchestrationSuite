from __future__ import annotations

import dataclasses
import hashlib
import uuid

from orchestrator.errors import FingerprintMismatch
from orchestrator.fingerprint import verify_fingerprint
from orchestrator.state import RunState, validate
from orchestrator.status import transition


def _trace_event_id(run_id: str, event_type: str, state_version_after: int | None) -> str:
    return hashlib.sha256(f"{run_id}:{event_type}:{state_version_after}".encode("utf-8")).hexdigest()[:32]


def _emit(persistence, state: RunState, *, event_type: str, actor: str, message: str, now: str) -> None:
    persistence.append_trace_event(
        {
            "event_id": _trace_event_id(state.run_id, event_type, state.state_version),
            "run_id": state.run_id,
            "engagement_id": state.engagement_id,
            "event_type": event_type,
            "event_time": now,
            "stage": state.phase,
            "status": state.status,
            "message": message,
            "duration_s": None,
            "node_name": None,
            "execution_key": None,
            "actor": actor,
            "state_version": state.state_version,
        }
    )


def create_run(
    persistence,
    *,
    run_id: str | None = None,
    run_kind: str,
    engagement_id: str | None,
    skill_id: str | None,
    skill_version: str | None,
    mode: str,
    audit_period: tuple[str, str],
    objective: str,
    run_owner: str,
    business_unit: str | None = None,
    materiality: float | None = None,
    options: dict | None = None,
    fingerprint: dict,
    now: str,
) -> RunState:
    run_id = run_id or f"RUN-{uuid.uuid4().hex[:12].upper()}"
    state = RunState(
        run_id=run_id,
        run_kind=run_kind,
        engagement_id=engagement_id,
        skill_id=skill_id,
        skill_version=skill_version,
        mode=mode,
        phase="plan",
        audit_period=tuple(audit_period),
        objective=objective,
        business_unit=business_unit,
        materiality=materiality,
        options=options or {},
        run_owner=run_owner,
        fingerprint_id=fingerprint["fingerprint_id"],
        created_at=now,
        last_state_change_at=now,
        status="queued",
    )
    validate(state)
    new_state = persistence.create_run(state, fingerprint)
    _emit(persistence, new_state, event_type="run_created", actor=run_owner, message="Run created", now=now)
    return new_state


def confirm_plan(persistence, run_id: str, *, actor: str, now: str) -> RunState:
    state = persistence.load_state(run_id)
    new_state = transition(state, "queued", now=now, phase="execute")
    new_state = dataclasses.replace(new_state, plan_confirmed=True)
    saved = persistence.save_state(new_state)
    _emit(persistence, saved, event_type="plan_confirmed", actor=actor, message="Plan confirmed", now=now)
    return saved


def sign_off(persistence, run_id: str, *, actor: str, now: str) -> RunState:
    state = persistence.load_state(run_id)
    new_state = transition(state, "queued", now=now, phase="export")
    new_state = dataclasses.replace(new_state, signoff={"approver": actor, "timestamp": now})
    saved = persistence.save_state(new_state)
    _emit(persistence, saved, event_type="signed_off", actor=actor, message=f"Findings signed off by {actor}", now=now)
    return saved


def reject(persistence, run_id: str, *, actor: str, reason: str, now: str) -> RunState:
    state = persistence.load_state(run_id)
    new_state = transition(state, "failed", now=now, reason=reason)
    saved = persistence.save_state(new_state)
    _emit(persistence, saved, event_type="rejected", actor=actor, message=reason, now=now)
    return saved


def resume(persistence, run_id: str, *, actor: str, now: str, current_fingerprint: dict) -> RunState:
    state = persistence.load_state(run_id)
    stored_fingerprint = persistence.get_fingerprint(state.fingerprint_id)
    verify_fingerprint(stored_fingerprint, current_fingerprint)
    new_state = transition(state, "queued", now=now)
    saved = persistence.save_state(new_state)
    _emit(persistence, saved, event_type="resumed", actor=actor, message="Run resumed after interruption", now=now)
    return saved
