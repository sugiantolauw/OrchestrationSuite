"""P6 WP N9's sign-off changes (docs/specs/P6_narration_design.md §5.2) and
the independent-review audit gap this WP's brief calls out: sign-off is
refused while any AI-proposed finding is undecided; a successful sign-off
snapshots the narration state into `RunState.signoff`; and sign-off moves
every finding's `review_state` to `approved` (previously it stayed
`draft`), while the self-approval label stays exactly as it was."""

from __future__ import annotations

import pytest

from orchestrator import service
from tests.n9_test_support import harness_at_awaiting_signoff


def _candidate(run_id: str, *, candidate_id: str, rule_id: str) -> dict:
    return {
        "candidate_id": candidate_id, "engagement_id": "ENG-DEFAULT", "skill_id": "SKILL-MINI",
        "generation": 0, "rule_id": rule_id, "title": "An AI-proposed candidate finding",
        "metrics_cited": ["hv_count"], "producing_test_ids": ["T1"], "proposed_severity": "Medium",
        "severity_reason": "Model-proposed reason.", "rationale": "Model rationale.",
        "monetary_basis": "none", "monetary_basis_note": "no declared monetary basis",
        "exposure_amount": None, "headline_eligible": False,
        "headline_ineligible_reason": "monetary_basis is none", "candidate_status": "candidate",
        "call_id": f"CALL-{candidate_id}",
    }


def test_signoff_moves_every_finding_to_approved(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    findings_before = h.persistence.list_findings(run_id)
    assert findings_before
    assert all(f["review_state"] == "draft" for f in findings_before)

    service.sign_off(ctx_app, run_id, "alice")

    findings_after = h.persistence.list_findings(run_id)
    assert findings_after
    assert all(f["review_state"] == "approved" for f in findings_after)
    # Nothing else about a finding changed on the way through.
    before_by_id = {f["finding_id"]: f for f in findings_before}
    for f in findings_after:
        assert f["severity"] == before_by_id[f["finding_id"]]["severity"]
        assert f["observation"] == before_by_id[f["finding_id"]]["observation"]


def test_self_approval_labelling_is_unchanged_by_the_review_state_advance(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    assert state.run_owner == "alice"  # narration_test_support's default run_owner

    signed_off = service.sign_off(ctx_app, run_id, "alice")

    assert signed_off.signoff["approver"] == "alice"
    assert signed_off.signoff["self_approved"] is True
    assert signed_off.signoff["sod_enforced"] is False
    # Findings still moved to approved even though it's a self sign-off.
    assert all(f["review_state"] == "approved" for f in h.persistence.list_findings(run_id))


def test_signoff_snapshots_narration_generation_and_narrative_versions(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    narratives = h.persistence.get_narratives(run_id)
    assert narratives  # the mini fixture's DispatchingModelClient narrates every field

    signed_off = service.sign_off(ctx_app, run_id, "alice")

    narration_snapshot = signed_off.signoff["narration"]
    assert narration_snapshot["generation"] == 0
    versions = narration_snapshot["narrative_versions"]
    assert set(versions) == {n["narrative_id"] for n in narratives}
    for n in narratives:
        assert versions[n["narrative_id"]] == n["version"]
    assert narration_snapshot["candidate_decisions"] == []


def test_signoff_snapshot_includes_candidate_decisions(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(
        run_id,
        [_candidate(run_id, candidate_id="C1", rule_id="SKILL-MINI.ai.aaa"), _candidate(run_id, candidate_id="C2", rule_id="SKILL-MINI.ai.bbb")],
        now=clock(),
    )
    service.decide_candidate(ctx_app, run_id, "C1", decision="accepted", reason=None, decided_severity="High", actor="alice")
    service.decide_candidate(ctx_app, run_id, "C2", decision="rejected", reason="not applicable this run", decided_severity=None, actor="alice")

    signed_off = service.sign_off(ctx_app, run_id, "alice")

    decisions = {d["candidate_id"]: d for d in signed_off.signoff["narration"]["candidate_decisions"]}
    assert decisions["C1"]["decision"] == "accepted"
    assert decisions["C1"]["decided_by"] == "alice"
    assert decisions["C2"]["decision"] == "rejected"


def test_signoff_without_any_narration_or_candidates_keeps_the_original_signoff_shape(local_persistence, tmp_path, clock):
    # A run with narration disabled: no narratives, no candidates -- the
    # `signoff` dict must stay exactly the four keys it always had, so
    # existing exact-equality tests elsewhere (tests/test_e2e_trivial.py)
    # are unaffected by this WP's additive change.
    from tests.narration_test_support import make_narration_harness
    from orchestrator.nodes.fieldwork import NODES_FOR as FIELDWORK_NODES_FOR
    from orchestrator.pipeline import run_phase
    from tests.n9_test_support import app_context_for

    h = make_narration_harness(local_persistence, tmp_path, narration_enabled=False)
    fingerprint = h.persistence.get_fingerprint(h.state.fingerprint_id)
    state = run_phase(
        h.persistence, h.state.run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "awaiting_signoff"
    assert h.persistence.get_narratives(state.run_id) == []
    assert h.persistence.list_candidates(state.run_id) == []

    ctx_app = app_context_for(h, clock=clock)
    signed_off = service.sign_off(ctx_app, state.run_id, "alice")

    assert set(signed_off.signoff.keys()) == {"approver", "timestamp", "self_approved", "sod_enforced"}


def test_signoff_refused_while_undecided_leaves_findings_at_draft(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(run_id, [_candidate(run_id, candidate_id="C1", rule_id="SKILL-MINI.ai.aaa")], now=clock())

    from orchestrator.errors import CandidatesUndecided

    with pytest.raises(CandidatesUndecided):
        service.sign_off(ctx_app, run_id, "alice")

    assert all(f["review_state"] == "draft" for f in h.persistence.list_findings(run_id))
