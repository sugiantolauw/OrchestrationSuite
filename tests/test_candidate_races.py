"""T-C6 (docs/specs/P6_narration_design.md §11, §5.2): `decide_candidate`
racing `supersede_undecided` has exactly one winner; an accept without a
decided severity is refused; a reject without a reason is refused. P6 WP N9,
`orchestrator.service.decide_candidate`.

WP N8 (`orchestrator/narration/candidates.py`, `nodes/narration.py`'s own
candidate hook) is not merged into this branch -- out of this WP's scope to
build or depend on. `finding_candidates` rows are written directly via the
already-merged `persistence.write_candidates` (the exact persistence
contract N8 itself will call), never through a model."""

from __future__ import annotations

import pytest

from orchestrator import service
from orchestrator.errors import (
    CandidateAlreadyDecided,
    CandidateReasonRequired,
    CandidateSeverityRequired,
    CandidateSuperseded,
    CandidatesUndecided,
)
from tests.n9_test_support import harness_at_awaiting_signoff


def _candidate(run_id: str, *, candidate_id: str, generation: int = 0, rule_id: str = "SKILL-MINI.ai.abc123") -> dict:
    return {
        "candidate_id": candidate_id,
        "engagement_id": "ENG-DEFAULT",
        "skill_id": "SKILL-MINI",
        "generation": generation,
        "rule_id": rule_id,
        "title": "An AI-proposed candidate finding",
        "metrics_cited": ["hv_count"],
        "producing_test_ids": ["T1"],
        "proposed_severity": "Medium",
        "severity_reason": "Model-proposed reason.",
        "rationale": "Model rationale.",
        "monetary_basis": "none",
        "monetary_basis_note": "no declared monetary basis",
        "exposure_amount": None,
        "headline_eligible": False,
        "headline_ineligible_reason": "monetary_basis is none",
        "candidate_status": "candidate",
        "call_id": f"CALL-{candidate_id}",
    }


def test_accept_requires_decided_severity(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(run_id, [_candidate(run_id, candidate_id="C1")], now=clock())

    with pytest.raises(CandidateSeverityRequired):
        service.decide_candidate(ctx_app, run_id, "C1", decision="accepted", reason=None, decided_severity=None, actor="alice")

    row = h.persistence.list_candidates(run_id)[0]
    assert row["candidate_status"] == "candidate"


def test_reject_requires_a_non_empty_reason(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(run_id, [_candidate(run_id, candidate_id="C1")], now=clock())

    with pytest.raises(CandidateReasonRequired):
        service.decide_candidate(ctx_app, run_id, "C1", decision="rejected", reason="", decided_severity=None, actor="alice")
    with pytest.raises(CandidateReasonRequired):
        service.decide_candidate(ctx_app, run_id, "C1", decision="rejected", reason="   ", decided_severity=None, actor="alice")

    row = h.persistence.list_candidates(run_id)[0]
    assert row["candidate_status"] == "candidate"


def test_accept_stores_both_proposed_and_decided_severity(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(run_id, [_candidate(run_id, candidate_id="C1")], now=clock())

    result = service.decide_candidate(ctx_app, run_id, "C1", decision="accepted", reason=None, decided_severity="High", actor="alice")

    assert result["candidate_status"] == "accepted"
    assert result["proposed_severity"] == "Medium"
    assert result["decided_severity"] == "High"
    assert result["decided_by"] == "alice"


def test_second_decision_on_an_already_decided_candidate_is_refused(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(run_id, [_candidate(run_id, candidate_id="C1")], now=clock())

    service.decide_candidate(ctx_app, run_id, "C1", decision="accepted", reason=None, decided_severity="High", actor="alice")
    with pytest.raises(CandidateAlreadyDecided):
        service.decide_candidate(ctx_app, run_id, "C1", decision="rejected", reason="changed my mind", decided_severity=None, actor="bob")

    # exactly one decision survives -- the first
    row = h.persistence.list_candidates(run_id)[0]
    assert row["candidate_status"] == "accepted"
    assert row["decided_by"] == "alice"


def test_decide_candidate_racing_supersede_undecided_has_exactly_one_winner(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(run_id, [_candidate(run_id, candidate_id="C1", generation=0)], now=clock())

    # Simulate a regenerate's supersede_undecided(below_generation=1) winning the
    # race against a concurrent decide_candidate on the SAME (generation-0,
    # still-'candidate') row -- both are conditional UPDATEs guarded by
    # candidate_status='candidate' (§5.2), so whichever commits first wins.
    superseded = h.persistence.supersede_undecided(run_id, below_generation=1, now=clock())
    assert superseded == 1

    with pytest.raises(CandidateSuperseded):
        service.decide_candidate(ctx_app, run_id, "C1", decision="accepted", reason=None, decided_severity="High", actor="alice")

    row = h.persistence.list_candidates(run_id)[0]
    assert row["candidate_status"] == "superseded"
    assert row["decided_by"] is None  # the loser's decision never landed


def test_decide_candidate_winning_the_race_leaves_the_row_undecided_for_nobody_else(local_persistence, tmp_path, clock):
    # The reverse order: decide_candidate commits first, so the LATER
    # supersede_undecided (a regenerate that raced in behind it) must be a
    # no-op against this already-decided row -- a decision, once made, is
    # frozen (§5.2 "decided ones are frozen").
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(run_id, [_candidate(run_id, candidate_id="C1", generation=0)], now=clock())

    service.decide_candidate(ctx_app, run_id, "C1", decision="rejected", reason="not relevant this run", decided_severity=None, actor="alice")
    superseded = h.persistence.supersede_undecided(run_id, below_generation=1, now=clock())
    assert superseded == 0

    row = h.persistence.list_candidates(run_id)[0]
    assert row["candidate_status"] == "rejected"
    assert row["decision_reason"] == "not relevant this run"


def test_signoff_refused_while_any_candidate_is_undecided(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(
        run_id,
        [_candidate(run_id, candidate_id="C1", rule_id="SKILL-MINI.ai.aaa"), _candidate(run_id, candidate_id="C2", rule_id="SKILL-MINI.ai.bbb")],
        now=clock(),
    )
    service.decide_candidate(ctx_app, run_id, "C1", decision="accepted", reason=None, decided_severity="Low", actor="alice")

    with pytest.raises(CandidatesUndecided) as exc:
        service.sign_off(ctx_app, run_id, "alice")
    assert "decide every AI-proposed finding before sign-off" in str(exc.value)

    # still awaiting_signoff -- the refused sign-off changed nothing
    reloaded = h.persistence.load_state(run_id)
    assert reloaded.status == "awaiting_signoff"
    assert reloaded.signoff is None
