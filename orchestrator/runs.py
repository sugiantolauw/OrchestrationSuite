from __future__ import annotations

import dataclasses
import hashlib
import uuid

from orchestrator.errors import (
    CandidatesUndecided,
    EngagementNotFound,
    FingerprintMismatch,
    RunCodeRevisionStale,
    RunNotAwaitingSignoff,
)
from orchestrator.fingerprint import verify_fingerprint
from orchestrator.signoff_policy import evaluate_signoff
from orchestrator.state import ENGAGEMENT_SCOPED_KINDS, RunState, validate
from orchestrator.status import transition

# CLAUDE.md §4.8 item 2 / P1B: findings.review_state's forward-only sequence
# (persistence_local._REVIEW_STATE_ORDER duplicates this literal -- both are
# the DDL's CHECK constraint's own enum, not a value either module derives
# from the other). Self sign-off (CLAUDE.md §11 "accept all defaults, allow
# self sign-off for now") has no separate preparer/reviewer step yet, so
# sign_off() below walks every finding straight from wherever it currently
# sits to 'approved', one allowed step at a time (P7 replaces this with a
# real preparer -> reviewer -> approver workflow that stops short of
# 'approved' until each role has actually acted).
_REVIEW_STATE_SEQUENCE = ("draft", "prepared", "reviewed", "approved")


def _advance_findings_to_approved(persistence, run_id: str, *, actor: str, now: str) -> None:
    """Independent-review audit gap (CLAUDE.md §11, this WP's brief): sign-off
    is the control that says what leaves the system (§2.4) -- a finding a
    human signed off on must not still read 'draft' in the XLSX/PPTX export
    and on `/workspace/tne`. `set_finding_review_state` only ever advances one
    step (persistence_local._REVIEW_STATE_ORDER), so a finding already past
    'draft' (P7-era preparer/reviewer steps, once they exist) is simply
    walked the rest of the way rather than re-driven from the start."""
    for finding in persistence.list_findings(run_id):
        current = finding.get("review_state") or "draft"
        if current not in _REVIEW_STATE_SEQUENCE:
            continue
        for step in _REVIEW_STATE_SEQUENCE[_REVIEW_STATE_SEQUENCE.index(current) + 1 :]:
            persistence.set_finding_review_state(finding["finding_id"], to_state=step, actor=actor, now=now)


def _trace_event_id(run_id: str, event_type: str, state_version_after: int | None) -> str:
    return hashlib.sha256(f"{run_id}:{event_type}:{state_version_after}".encode("utf-8")).hexdigest()[:32]


# Trace page stage vocabulary for lifecycle (non-node) events (CLAUDE.md §9C non-blocking
# item; reference_app/src/platform/components.py's trace_event_row renders `stage` as
# free text, so this is display-only). Falls back to the run's phase for anything else.
_LIFECYCLE_STAGE_LABELS: dict[str, str] = {
    "run_created": "Initialisation",
    "plan_confirmed": "Plan",
    "signed_off": "Sign-off",
    "resumed": "Recovery",
    "narration_regenerate_requested": "Narration",
}


def _emit(persistence, state: RunState, *, event_type: str, actor: str, message: str, now: str) -> None:
    persistence.append_trace_event(
        {
            "event_id": _trace_event_id(state.run_id, event_type, state.state_version),
            "run_id": state.run_id,
            "engagement_id": state.engagement_id,
            "event_type": event_type,
            "event_time": now,
            "stage": _LIFECYCLE_STAGE_LABELS.get(event_type, state.phase),
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
    data_assets: list[dict] | None = None,
    fingerprint: dict,
    now: str,
) -> RunState:
    if run_kind in ENGAGEMENT_SCOPED_KINDS:
        # engagement_id=None already fails RunState.validate() below, but that raises
        # ValueError with no way to distinguish "missing" from "does not exist" -- a
        # caller (the UI) needs to tell those apart, so check existence explicitly
        # (CLAUDE.md §4.8, P1B).
        if engagement_id is None or persistence.get_engagement(engagement_id) is None:
            raise EngagementNotFound(engagement_id or "")

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
        # data_assets is written in this SAME initial insert, never a
        # follow-up save_state (CLAUDE.md §2.3 rule 2 concurrency note): the
        # run becomes visible to any already-running executor's admission
        # loop (find_runs(["queued"])) the moment this row lands, and a
        # second write racing that executor's own first CAS transition
        # (queued -> running) would lose -- StaleStateError, observed live
        # against the deployed App during the P3 integration pass.
        data_assets=data_assets or [],
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
    # plan_confirmed is set BEFORE transition() so the plan->execute gate (status.py)
    # can see it on the state it is validating (CLAUDE.md §2.4, B3).
    state = dataclasses.replace(state, plan_confirmed=True)
    new_state = transition(state, "queued", now=now, phase="execute")
    saved = persistence.save_state(new_state)
    _emit(persistence, saved, event_type="plan_confirmed", actor=actor, message="Plan confirmed", now=now)
    return saved


def sign_off(persistence, run_id: str, *, actor: str, now: str) -> RunState:
    state = persistence.load_state(run_id)
    if state.status != "awaiting_signoff":
        raise RunNotAwaitingSignoff(run_id, state.status)

    # P6 §5.2 / §7 UI-3: "Sign-off is refused while any current-generation row
    # is candidate" -- checked BEFORE anything else is written, so a rejected
    # call leaves nothing touched (the same "check first" discipline
    # write_findings uses for NonDraftFindingWouldBeDeleted).
    candidates = persistence.list_candidates(run_id)
    undecided = [c["candidate_id"] for c in candidates if c["candidate_status"] == "candidate"]
    if undecided:
        raise CandidatesUndecided(run_id, undecided)

    policy = evaluate_signoff(actor=actor, run_owner=state.run_owner)
    signoff = {
        "approver": actor,
        "timestamp": now,
        "self_approved": policy["self_approved"],
        "sod_enforced": policy["sod_enforced"],
    }
    # §5.2: "Sign-off snapshots the decisions and the exact narrative versions
    # signed off into RunState.signoff" -- attached only when this run
    # actually has narration/candidates to snapshot, so a run that never
    # touched narration (narration off, or a pre-P6 fixture/test) gets
    # EXACTLY the four-key `signoff` dict it always has -- purely additive,
    # never a shape change for a run this feature does not apply to.
    narratives = persistence.get_narratives(run_id)
    if narratives or candidates:
        signoff["narration"] = {
            "generation": int((state.options or {}).get("narration_generation", 0) or 0),
            "narrative_versions": {n["narrative_id"]: n["version"] for n in narratives},
            "candidate_decisions": [
                {
                    "candidate_id": c["candidate_id"],
                    "rule_id": c["rule_id"],
                    "decision": c["candidate_status"],
                    "decided_by": c.get("decided_by"),
                    "decided_at": c.get("decided_at"),
                }
                for c in candidates
                if c["candidate_status"] in ("accepted", "rejected")
            ],
        }

    # signoff is set BEFORE transition() so the execute->export gate (status.py) can
    # see it on the state it is validating (CLAUDE.md §2.4, B3).
    state = dataclasses.replace(state, signoff=signoff)
    new_state = transition(state, "queued", now=now, phase="export")
    saved = persistence.save_state(new_state)

    # Independent-review audit gap (this WP's brief): sign-off is the gate
    # that says what leaves the system -- move every finding to 'approved' so
    # the exports and /workspace/tne agree with what was actually signed off.
    _advance_findings_to_approved(persistence, run_id, actor=actor, now=now)

    message = f"Findings signed off by {actor}"
    if policy["self_approved"]:
        message += " — self-approved (segregation of duties not enforced)"
    accepted = sum(1 for c in candidates if c["candidate_status"] == "accepted")
    rejected = sum(1 for c in candidates if c["candidate_status"] == "rejected")
    if accepted or rejected:
        message += f"; {accepted} AI-proposed accepted, {rejected} rejected"
    _emit(persistence, saved, event_type="signed_off", actor=actor, message=message, now=now)
    return saved


def regenerate_narration(
    persistence, run_id: str, *, actor: str, now: str, narrate_node_index: int
) -> RunState:
    """P6 WP N9 (docs/specs/P6_narration_design.md §5.5): bumps
    `options.narration_generation`, supersedes this run's still-undecided
    candidates from any earlier generation (a decision, once made, is
    frozen -- §5.2: "decided ones are frozen"), and re-enters the execute
    phase at `narrate` via `transition(restart_at_index=...)` -- the phase
    itself never changes, only `next_node_index`/`phase_epoch`, so the
    executor's very next pass replays only `narrate` and `act` (CLAUDE.md
    §4.1's phase_epoch addition) before returning to `awaiting_signoff`."""
    state = persistence.load_state(run_id)
    if state.status != "awaiting_signoff":
        raise RunNotAwaitingSignoff(run_id, state.status)

    generation = int((state.options or {}).get("narration_generation", 0) or 0) + 1
    options = dict(state.options or {})
    options["narration_generation"] = generation
    options["narration_generation_requested_by"] = actor

    # Called here (service/state-machine layer), not left solely to the
    # `narrate` node's own generation>0 branch (P6 WP N8): the invariant
    # "undecided candidates become superseded, never deleted" must hold the
    # instant regeneration is requested, not only once the executor gets
    # around to running narrate. `supersede_undecided`'s own row-level CAS
    # (candidate_status='candidate' -> 'superseded') makes a second call from
    # inside narrate() a safe no-op, never a double-supersede.
    superseded_count = persistence.supersede_undecided(run_id, below_generation=generation, now=now)

    state = dataclasses.replace(state, options=options)
    new_state = transition(state, "queued", now=now, restart_at_index=narrate_node_index)
    saved = persistence.save_state(new_state)

    message = f"Narration regeneration requested by {actor} (generation {generation})"
    if superseded_count:
        message += f"; {superseded_count} undecided AI-proposed finding(s) superseded"
    _emit(persistence, saved, event_type="narration_regenerate_requested", actor=actor, message=message, now=now)
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
    # CLAUDE.md §11 "Paused runs across a code deploy" / independent review
    # 2026-09-24 gap #11: an interrupted run's own fingerprint check follows
    # the same phase rule as the executor's admission-time one (orchestrator/
    # executor.py, orchestrator/pipeline.py) -- code_revision may differ ONLY
    # once the execute phase has completed (state.phase == "export"; sign_off
    # is what advances phase to "export", and it requires execute to have
    # already produced this run's numbers). Every other hashed field must
    # still match exactly either way.
    allow_code_revision_diff = state.phase == "export"
    try:
        verify_fingerprint(stored_fingerprint, current_fingerprint, allow_code_revision_diff=allow_code_revision_diff)
    except FingerprintMismatch as exc:
        if allow_code_revision_diff:
            raise  # some OTHER field differs too -- a real mismatch, not the narrow allowed case
        raise RunCodeRevisionStale(run_id, state.phase, exc.differing_fields) from exc
    if allow_code_revision_diff:
        current_code_revision = current_fingerprint.get("code_revision")
        if stored_fingerprint.get("code_revision") != current_code_revision:
            # Recorded, never silent (CLAUDE.md NN14): run_fingerprints stays
            # immutable, so the code revision actually resuming this run's
            # export is recorded on the run record and in the append-only
            # override history instead.
            persistence.record_export_code_revision(
                run_id, fingerprint_id=state.fingerprint_id,
                code_revision=current_code_revision, now=now,
            )
    new_state = transition(state, "queued", now=now)
    saved = persistence.save_state(new_state)
    _emit(persistence, saved, event_type="resumed", actor=actor, message="Run resumed after interruption", now=now)
    return saved
