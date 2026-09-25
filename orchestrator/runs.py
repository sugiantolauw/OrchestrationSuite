from __future__ import annotations

import dataclasses
import hashlib
import re
import uuid

from orchestrator.errors import (
    CandidatesUndecided,
    EngagementNotFound,
    FingerprintMismatch,
    ReviewActionRefused,
    RoleLookupFailed,
    RunCodeRevisionStale,
    RunNotAwaitingSignoff,
)
from orchestrator.fingerprint import verify_fingerprint
from orchestrator.signoff_policy import compute_sod_waived, evaluate_signoff, evaluate_step
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
    "review_prepared": "Preparation",
    "review_reviewed": "Review",
    "review_returned": "Review",
    "review_note_raised": "Review",
    "review_note_responded": "Review",
    "review_note_cleared": "Review",
    "review_action_refused": "Review",
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



# CLAUDE.md §11 "Run start opens the run page at once" (2026-09-25): the web
# tier (app/src/run_setup.py) pre-generates a run_id with THIS SAME function
# before the `runs` row exists, so it can navigate to /run/<run_id> while the
# real create_run() below still runs in the background -- generate_run_id()
# is factored out rather than inlined so both callers produce exactly one
# format. RUN_ID_RE is that format, exported for service.start_audit_run to
# validate a run_id it received from the web tier against (is_valid_run_id
# below) -- create_run() itself stays permissive: every existing internal
# caller (tests/, other orchestrator/ code) passes its own human-readable
# run_id for fixture clarity, a use this function has always allowed and
# which has nothing to do with the web-tier trust boundary the new
# parameter on start_audit_run is guarding.
RUN_ID_RE = re.compile(r"^RUN-[0-9A-F]{12}$")


def generate_run_id() -> str:
    return f"RUN-{uuid.uuid4().hex[:12].upper()}"


def is_valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_RE.match(run_id))


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

    run_id = run_id or generate_run_id()
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


# ── P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.3-3.4) ──
#
# `sign_off` below stays the SAME function every pre-P7 caller already uses,
# with a dual path (D-P7-9 / the many pre-P7 tests that call it directly with
# no `role_resolver`/`settings`, never having called `prepare`/`mark_reviewed`
# first): `state.review` is None (or stage != 'approval') for any run that
# never entered the P7 workflow, so it takes the LEGACY self-sign-off path
# exactly as before -- zero behaviour change for those callers. A run whose
# stage IS 'approval' only got there via `prepare()` + `mark_reviewed()`
# below, both of which already required a `role_resolver`/`settings`, so by
# the time `sign_off` needs them for the gated path they are always the
# caller's to supply (the App's service layer always does, WP B3b).


def _configured_groups(settings, role: str) -> tuple[str, ...]:
    return {
        "preparer": settings.review_preparer_groups,
        "reviewer": settings.review_reviewer_groups,
        "approver": settings.review_approver_groups,
    }[role]


def _step_id(run_id: str, action: str, state_version: int) -> str:
    # §3.4: deterministic on (run_id, action, the state_version the action was
    # attempted AGAINST, i.e. before save_state's own CAS increment) -- a
    # replayed click that resubmits the same request (never having seen a
    # response) is a no-op insert, not a duplicate row.
    return hashlib.sha256(f"{run_id}:{action}:{state_version}".encode("utf-8")).hexdigest()[:32]


def _resolve_role_or_refuse(role_resolver, persistence, state: RunState, action: str, actor: str, now: str):
    try:
        return role_resolver.roles_for(actor)
    except RoleLookupFailed:
        reason = "Could not verify group membership — try again."
        _emit(persistence, state, event_type="review_action_refused", actor=actor,
              message=f"{action} refused: {reason}", now=now)
        raise ReviewActionRefused(state.run_id, action, reason)


def _refuse(persistence, state: RunState, action: str, actor: str, reason: str, now: str) -> None:
    _emit(persistence, state, event_type="review_action_refused", actor=actor,
          message=f"{action} refused: {reason}", now=now)
    raise ReviewActionRefused(state.run_id, action, reason)


def _prior_actors(review: dict) -> dict[str, str]:
    actors: dict[str, str] = {}
    if review.get("prepared"):
        actors["preparer"] = review["prepared"]["actor"]
    if review.get("reviewed"):
        actors["reviewer"] = review["reviewed"]["actor"]
    return actors


def _open_note_count(persistence, run_id: str) -> int:
    return sum(1 for n in persistence.list_review_notes(run_id) if n["state"] == "open")


def _load_awaiting_signoff(persistence, run_id: str) -> RunState:
    state = persistence.load_state(run_id)
    if state.status != "awaiting_signoff":
        raise RunNotAwaitingSignoff(run_id, state.status)
    return state


def _check_candidates_decided(persistence, run_id: str) -> list[dict]:
    candidates = persistence.list_candidates(run_id)
    undecided = [c["candidate_id"] for c in candidates if c["candidate_status"] == "candidate"]
    if undecided:
        raise CandidatesUndecided(run_id, undecided)
    return candidates


def prepare(persistence, run_id: str, *, actor: str, now: str, role_resolver, settings) -> RunState:
    """§3.3 "Mark as prepared": findings `draft` -> `prepared`; the current
    narration/theme generation is recorded as confirmed on the `review_steps`
    row, closing the §4.6 theme-confirmation gap with no new theme table."""
    state = _load_awaiting_signoff(persistence, run_id)
    _check_candidates_decided(persistence, run_id)

    review = state.review or {}
    stage = review.get("stage", "preparation")
    resolution = _resolve_role_or_refuse(role_resolver, persistence, state, "prepared", actor, now)
    decision = evaluate_step(
        "prepared", actor, stage=stage, resolution=resolution,
        configured_groups=settings.review_preparer_groups, prior_actors={},
        open_notes=_open_note_count(persistence, run_id), sod_mode=settings.review_sod_mode,
    )
    if not decision.allowed:
        _refuse(persistence, state, "prepared", actor, decision.reason, now)

    narration_generation = int((state.options or {}).get("narration_generation", 0) or 0)

    new_review = dict(review)
    new_review["stage"] = "review"
    new_review["prepared"] = {"actor": actor, "at": now, "role_source": decision.role_source}
    new_review.setdefault("returns", [])
    new_state = dataclasses.replace(state, review=new_review)
    # CAS FIRST (mirrors sign_off's existing ordering): only the racer whose
    # save_state wins may advance findings below -- a loser's save_state
    # raises StaleStateError here, before it ever touches a finding row, so
    # two concurrent "Mark as prepared" clicks can never both try to walk the
    # same finding forward (CLAUDE.md §9C failure-injection discipline).
    saved = persistence.save_state(new_state)

    for finding in persistence.list_findings(run_id):
        if (finding.get("review_state") or "draft") == "draft":
            persistence.set_finding_review_state(finding["finding_id"], to_state="prepared", actor=actor, now=now)

    persistence.append_review_step({
        "step_id": _step_id(run_id, "prepared", state.state_version),
        "run_id": run_id, "engagement_id": state.engagement_id, "action": "prepared",
        "actor": actor, "role": "preparer", "matched_group": decision.matched_group,
        "role_source": decision.role_source, "sod_mode": settings.review_sod_mode,
        "reason": None, "theme_generation": narration_generation,
        "narration_generation": narration_generation, "state_version": state.state_version, "at": now,
    })
    _emit(persistence, saved, event_type="review_prepared", actor=actor,
          message=f"Marked as prepared by {actor}", now=now)
    return saved


def mark_reviewed(persistence, run_id: str, *, actor: str, now: str, role_resolver, settings) -> RunState:
    """§3.3 "Mark as reviewed": findings `prepared` -> `reviewed`."""
    state = _load_awaiting_signoff(persistence, run_id)

    review = state.review or {}
    stage = review.get("stage", "preparation")
    resolution = _resolve_role_or_refuse(role_resolver, persistence, state, "reviewed", actor, now)
    decision = evaluate_step(
        "reviewed", actor, stage=stage, resolution=resolution,
        configured_groups=settings.review_reviewer_groups, prior_actors=_prior_actors(review),
        open_notes=_open_note_count(persistence, run_id), sod_mode=settings.review_sod_mode,
    )
    if not decision.allowed:
        _refuse(persistence, state, "reviewed", actor, decision.reason, now)

    new_review = dict(review)
    new_review["stage"] = "approval"
    new_review["reviewed"] = {"actor": actor, "at": now, "role_source": decision.role_source}
    new_state = dataclasses.replace(state, review=new_review)
    # CAS FIRST -- see prepare()'s own comment above.
    saved = persistence.save_state(new_state)

    for finding in persistence.list_findings(run_id):
        if (finding.get("review_state") or "draft") == "prepared":
            persistence.set_finding_review_state(finding["finding_id"], to_state="reviewed", actor=actor, now=now)

    persistence.append_review_step({
        "step_id": _step_id(run_id, "reviewed", state.state_version),
        "run_id": run_id, "engagement_id": state.engagement_id, "action": "reviewed",
        "actor": actor, "role": "reviewer", "matched_group": decision.matched_group,
        "role_source": decision.role_source, "sod_mode": settings.review_sod_mode,
        "reason": None, "theme_generation": None, "narration_generation": None,
        "state_version": state.state_version, "at": now,
    })
    _emit(persistence, saved, event_type="review_reviewed", actor=actor,
          message=f"Marked as reviewed by {actor}", now=now)
    return saved


def return_to_preparer(persistence, run_id: str, *, actor: str, reason: str, now: str, role_resolver, settings) -> RunState:
    """§3.3 "Return to preparer": stage -> `preparation`, findings -> `draft`
    (the only backwards move). Candidate decisions and open notes are left
    untouched."""
    state = _load_awaiting_signoff(persistence, run_id)
    review = state.review or {}
    stage = review.get("stage", "preparation")
    if stage not in ("review", "approval"):
        _refuse(
            persistence, state, "returned", actor,
            f"This run is at stage {stage!r} -- returning is only valid during review or approval.", now,
        )
    if not reason:
        _refuse(persistence, state, "returned", actor, "A reason is required to return a run to the preparer.", now)

    required_role = "reviewer" if stage == "review" else "approver"
    resolution = _resolve_role_or_refuse(role_resolver, persistence, state, "returned", actor, now)
    if required_role not in resolution.roles:
        groups = _configured_groups(settings, required_role)
        _refuse(
            persistence, state, "returned", actor,
            f"You are not in a preparer/reviewer/approver group ({', '.join(groups) or 'none configured'}).", now,
        )

    new_review = dict(review)
    new_review["stage"] = "preparation"
    new_review["returns"] = list(review.get("returns", [])) + [{"actor": actor, "at": now, "reason": reason}]
    new_state = dataclasses.replace(state, review=new_review)
    # CAS FIRST -- see prepare()'s own comment above.
    saved = persistence.save_state(new_state)

    persistence.reset_findings_review_state(run_id, actor=actor, now=now)

    persistence.append_review_step({
        "step_id": _step_id(run_id, "returned", state.state_version),
        "run_id": run_id, "engagement_id": state.engagement_id, "action": "returned",
        "actor": actor, "role": required_role, "matched_group": resolution.matched_groups.get(required_role),
        "role_source": resolution.role_source, "sod_mode": settings.review_sod_mode,
        "reason": reason, "theme_generation": None, "narration_generation": None,
        "state_version": state.state_version, "at": now,
    })
    _emit(persistence, saved, event_type="review_returned", actor=actor,
          message=f"Returned to preparer by {actor}: {reason}", now=now)
    return saved


def raise_review_note(
    persistence, run_id: str, *, actor: str, body: str, finding_id: str | None, now: str, role_resolver, settings
) -> dict:
    """§3.3 "Raise note": reviewer during `review`, approver during `approval`."""
    state = _load_awaiting_signoff(persistence, run_id)
    review = state.review or {}
    stage = review.get("stage", "preparation")
    if stage not in ("review", "approval"):
        _refuse(
            persistence, state, "note_raised", actor,
            f"This run is at stage {stage!r} -- notes may only be raised during review or approval.", now,
        )
    required_role = "reviewer" if stage == "review" else "approver"
    resolution = _resolve_role_or_refuse(role_resolver, persistence, state, "note_raised", actor, now)
    if required_role not in resolution.roles:
        groups = _configured_groups(settings, required_role)
        _refuse(
            persistence, state, "note_raised", actor,
            f"You are not in a preparer/reviewer/approver group ({', '.join(groups) or 'none configured'}).", now,
        )

    note_id = hashlib.sha256(f"{run_id}:{actor}:{now}:{finding_id or ''}".encode("utf-8")).hexdigest()[:32]
    note = persistence.add_review_note({
        "note_id": note_id, "run_id": run_id, "engagement_id": state.engagement_id,
        "finding_id": finding_id, "raised_by": actor, "raised_at": now, "body": body,
        "raised_role": required_role,
    })
    target = f"finding {finding_id}" if finding_id else "the run"
    _emit(persistence, state, event_type="review_note_raised", actor=actor,
          message=f"Note raised by {actor} on {target}", now=now)
    return note


def respond_to_review_note(
    persistence, run_id: str, note_id: str, *, actor: str, response: str, now: str, role_resolver, settings
) -> dict:
    """§3.3 "Respond to note": the preparer only, during either review or
    approval -- the note stays open until `clear_a_review_note` clears it."""
    state = _load_awaiting_signoff(persistence, run_id)
    resolution = _resolve_role_or_refuse(role_resolver, persistence, state, "note_responded", actor, now)
    if "preparer" not in resolution.roles:
        groups = _configured_groups(settings, "preparer")
        _refuse(
            persistence, state, "note_responded", actor,
            f"You are not in a preparer/reviewer/approver group ({', '.join(groups) or 'none configured'}).", now,
        )
    ok = persistence.respond_review_note(note_id, response=response, actor=actor, role="preparer", now=now)
    if not ok:
        _refuse(persistence, state, "note_responded", actor, "This note is no longer open.", now)
    _emit(persistence, state, event_type="review_note_responded", actor=actor,
          message=f"Note responded by {actor}", now=now)
    return next(n for n in persistence.list_review_notes(run_id) if n["note_id"] == note_id)


def clear_a_review_note(
    persistence, run_id: str, note_id: str, *, actor: str, now: str, role_resolver, settings
) -> dict:
    """§3.3 "Clear note": the raiser, or anyone currently holding the
    raiser's role -- requires the note to already have a response."""
    state = _load_awaiting_signoff(persistence, run_id)
    notes = persistence.list_review_notes(run_id)
    note = next((n for n in notes if n["note_id"] == note_id), None)
    if note is None:
        _refuse(persistence, state, "note_cleared", actor, "Note not found.", now)

    resolution = _resolve_role_or_refuse(role_resolver, persistence, state, "note_cleared", actor, now)
    raiser_role = note.get("raised_role")
    allowed = actor == note.get("raised_by") or (raiser_role and raiser_role in resolution.roles)
    if not allowed:
        _refuse(
            persistence, state, "note_cleared", actor,
            "Only the person who raised this note, or anyone holding their role, may clear it.", now,
        )
    ok = persistence.clear_review_note(note_id, actor=actor, role=raiser_role or "", now=now)
    if not ok:
        _refuse(persistence, state, "note_cleared", actor, "Respond to this note before clearing it.", now)
    _emit(persistence, state, event_type="review_note_cleared", actor=actor,
          message=f"Note cleared by {actor}", now=now)
    return next(n for n in persistence.list_review_notes(run_id) if n["note_id"] == note_id)


def sign_off(
    persistence, run_id: str, *, actor: str, now: str, role_resolver=None, settings=None
) -> RunState:
    state = persistence.load_state(run_id)
    if state.status != "awaiting_signoff":
        raise RunNotAwaitingSignoff(run_id, state.status)

    # P6 §5.2 / §7 UI-3, P7 §3.3 "re-checks it": "Sign-off is refused while
    # any current-generation row is candidate" -- checked BEFORE anything
    # else is written, so a rejected call leaves nothing touched (the same
    # "check first" discipline write_findings uses for
    # NonDraftFindingWouldBeDeleted).
    candidates = _check_candidates_decided(persistence, run_id)

    review = state.review or {}
    stage = review.get("stage")
    # A run only reaches stage 'approval' by having gone through prepare()
    # and mark_reviewed() above, both of which already required a
    # role_resolver/settings -- any OTHER run (state.review is None, or the
    # workflow never ran) takes the pre-P7 self-sign-off path unchanged.
    p7_active = stage == "approval"

    prior_actors: dict[str, str] = {}
    decision = None
    if p7_active:
        prior_actors = _prior_actors(review)
        resolution = _resolve_role_or_refuse(role_resolver, persistence, state, "approved", actor, now)
        decision = evaluate_step(
            "approved", actor, stage=stage, resolution=resolution,
            configured_groups=settings.review_approver_groups, prior_actors=prior_actors,
            open_notes=_open_note_count(persistence, run_id), sod_mode=settings.review_sod_mode,
        )
        if not decision.allowed:
            _refuse(persistence, state, "approved", actor, decision.reason, now)

        all_actors = dict(prior_actors)
        all_actors["approver"] = actor
        sod_waived = compute_sod_waived(all_actors)
        signoff = {
            "approver": actor,
            "timestamp": now,
            "self_approved": bool(sod_waived),
            "sod_enforced": settings.review_sod_mode == "enforced",
            "sod_mode": settings.review_sod_mode,
            "prepared_by": prior_actors.get("preparer"),
            "reviewed_by": prior_actors.get("reviewer"),
            "role_source": decision.role_source,
            "sod_waived": sod_waived,
            "open_notes_at_signoff": 0,
        }
    else:
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
    # EXACTLY the four/twelve-key `signoff` dict it always has -- purely
    # additive, never a shape change for a run this feature does not apply to.
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

    if p7_active:
        for finding in persistence.list_findings(run_id):
            current = finding.get("review_state") or "draft"
            if current in _REVIEW_STATE_SEQUENCE:
                for step in _REVIEW_STATE_SEQUENCE[_REVIEW_STATE_SEQUENCE.index(current) + 1 :]:
                    persistence.set_finding_review_state(finding["finding_id"], to_state=step, actor=actor, now=now)
        persistence.append_review_step({
            "step_id": _step_id(run_id, "approved", state.state_version),
            "run_id": run_id, "engagement_id": state.engagement_id, "action": "approved",
            "actor": actor, "role": "approver", "matched_group": decision.matched_group,
            "role_source": decision.role_source, "sod_mode": settings.review_sod_mode,
            "reason": None, "theme_generation": None, "narration_generation": None,
            "state_version": state.state_version, "at": now,
        })
    else:
        # Independent-review audit gap (this WP's brief): sign-off is the gate
        # that says what leaves the system -- move every finding to 'approved' so
        # the exports and /workspace/tne agree with what was actually signed off.
        _advance_findings_to_approved(persistence, run_id, actor=actor, now=now)

    message = f"Findings signed off by {actor}"
    if p7_active:
        message += f" (prepared by {prior_actors.get('preparer')}, reviewed by {prior_actors.get('reviewer')})"
    if signoff["self_approved"]:
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
