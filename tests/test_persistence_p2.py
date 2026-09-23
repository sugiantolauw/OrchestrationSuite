from __future__ import annotations

import pytest

from orchestrator.errors import (
    FindingNotFound,
    InvalidReviewStateTransition,
    NonDraftFindingWouldBeDeleted,
    RiskStatusRegression,
    SkillVersionConflict,
)
from tests.conftest import canonical_ts

# P2: skill versions, the risk/control register and findings (CLAUDE.md §4.6, §4.8, §4.9,
# §8 table-to-phase ownership). Runs against every persistence backend (local_memory,
# local_file, delta) via the `persistence` fixture in conftest.py, same contract as
# tests/test_persistence_contract.py.


def _finding(finding_id, *, rule_id, title="A finding", severity="High", **overrides):
    base = dict(
        finding_id=finding_id,
        rule_id=rule_id,
        test_id="T1",
        title=title,
        severity=severity,
        severity_rule="hv_count > thresholds.high_threshold",
        threshold_refs=[
            {
                "id": "high_threshold",
                "value": 10,
                "unit": "count",
                "provenance_type": "analyst-set",
                "pending_policy_confirmation": True,
            }
        ],
        analyst_set_severity=True,
        metrics_cited={"hv_count": {"value": 15, "unit": "count", "source_ref": {"sources": [], "columns": [], "grain": "row", "population": "pop"}}},
        observation="15 claims.",
        recommendation="Review these claims.",
        management_questions=["Why so many?"],
        control_id="CTL-1",
        risk_id="RSK-1",
        assertion="operating",
        exposure_amount=None,
        exposure_basis="pending P3 de-duplicated exposure",
        review_state="draft",
    )
    base.update(overrides)
    return base


# ── skill versions ───────────────────────────────────────────────────────────────────


def test_record_skill_version_insert_and_fetch(persistence, uid):
    skill_id = uid("SKILL")
    row = persistence.record_skill_version(
        skill_id=skill_id, version="1.0.0", content_hash="h1", content={"manifest": {"a": 1}},
        created_by="alice", now=canonical_ts(0),
    )
    assert row["skill_id"] == skill_id
    assert row["content"] == {"manifest": {"a": 1}}
    assert row["status"] == "draft"

    fetched = persistence.get_skill_version(skill_id, "1.0.0")
    assert fetched is not None
    assert fetched["content"] == {"manifest": {"a": 1}}
    assert fetched["content_hash"] == "h1"


def test_record_skill_version_same_key_same_hash_is_noop(persistence, uid):
    skill_id = uid("SKILL")
    first = persistence.record_skill_version(
        skill_id=skill_id, version="1.0.0", content_hash="h1", content={"a": 1},
        created_by="alice", now=canonical_ts(0),
    )
    second = persistence.record_skill_version(
        skill_id=skill_id, version="1.0.0", content_hash="h1", content={"a": 1},
        created_by="bob", now=canonical_ts(1),
    )
    # the second call is a no-op: it returns the ORIGINAL row, not a new one created_by bob
    assert second["created_by"] == "alice"
    assert second["created_at"] == canonical_ts(0)
    assert len(persistence.list_skill_versions(skill_id)) == 1


def test_record_skill_version_same_key_different_hash_conflicts(persistence, uid):
    skill_id = uid("SKILL")
    persistence.record_skill_version(
        skill_id=skill_id, version="1.0.0", content_hash="h1", content={"a": 1},
        created_by="alice", now=canonical_ts(0),
    )
    with pytest.raises(SkillVersionConflict):
        persistence.record_skill_version(
            skill_id=skill_id, version="1.0.0", content_hash="h2", content={"a": 2},
            created_by="bob", now=canonical_ts(1),
        )


def test_list_skill_versions_scoped_to_skill_id(persistence, uid):
    skill_id = uid("SKILL")
    other_skill_id = uid("SKILL")
    persistence.record_skill_version(
        skill_id=skill_id, version="1.0.0", content_hash="h1", content={}, created_by="a", now=canonical_ts(0)
    )
    persistence.record_skill_version(
        skill_id=skill_id, version="1.1.0", content_hash="h2", content={}, created_by="a", now=canonical_ts(1)
    )
    persistence.record_skill_version(
        skill_id=other_skill_id, version="1.0.0", content_hash="h3", content={}, created_by="a", now=canonical_ts(2)
    )
    versions = {v["version"] for v in persistence.list_skill_versions(skill_id)}
    assert versions == {"1.0.0", "1.1.0"}


def test_get_skill_version_missing_returns_none(persistence, uid):
    assert persistence.get_skill_version(uid("SKILL"), "9.9.9") is None


# ── risk / control register ──────────────────────────────────────────────────────────


def test_upsert_risks_insert_then_update_descriptive_fields(persistence, uid):
    risk_id = uid("RSK")
    persistence.upsert_risks(
        [{"risk_id": risk_id, "title": "Original title", "status": "proposed", "source": "manual"}],
        now=canonical_ts(0),
    )
    persistence.upsert_risks(
        [{"risk_id": risk_id, "title": "Updated title", "status": "accepted", "source": "manual"}],
        now=canonical_ts(1),
    )
    rows = [r for r in persistence.list_risks() if r["risk_id"] == risk_id]
    assert len(rows) == 1
    assert rows[0]["title"] == "Updated title"
    assert rows[0]["status"] == "accepted"
    assert rows[0]["created_at"] == canonical_ts(0)  # sticky: first-seen timestamp


def test_upsert_risks_status_regression_from_accepted_to_proposed_raises(persistence, uid):
    risk_id = uid("RSK")
    persistence.upsert_risks(
        [{"risk_id": risk_id, "title": "t", "status": "accepted", "source": "manual"}], now=canonical_ts(0)
    )
    with pytest.raises(RiskStatusRegression):
        persistence.upsert_risks(
            [{"risk_id": risk_id, "title": "t", "status": "proposed", "source": "manual"}], now=canonical_ts(1)
        )
    # the rejected write must not have applied
    rows = [r for r in persistence.list_risks() if r["risk_id"] == risk_id]
    assert rows[0]["status"] == "accepted"


def test_upsert_risks_status_regression_from_rejected_to_proposed_raises(persistence, uid):
    risk_id = uid("RSK")
    persistence.upsert_risks(
        [{"risk_id": risk_id, "title": "t", "status": "rejected", "source": "manual"}], now=canonical_ts(0)
    )
    with pytest.raises(RiskStatusRegression):
        persistence.upsert_risks(
            [{"risk_id": risk_id, "title": "t", "status": "proposed", "source": "manual"}], now=canonical_ts(1)
        )


def test_upsert_risks_forward_status_movement_allowed(persistence, uid):
    risk_id = uid("RSK")
    persistence.upsert_risks(
        [{"risk_id": risk_id, "title": "t", "status": "proposed", "source": "manual"}], now=canonical_ts(0)
    )
    persistence.upsert_risks(
        [{"risk_id": risk_id, "title": "t", "status": "accepted", "source": "manual"}], now=canonical_ts(1)
    )
    rows = [r for r in persistence.list_risks() if r["risk_id"] == risk_id]
    assert rows[0]["status"] == "accepted"


def test_upsert_controls_insert_then_update(persistence, uid):
    control_id = uid("CTL")
    risk_id = uid("RSK")
    persistence.upsert_controls(
        [{"control_id": control_id, "title": "Original", "risk_id": risk_id}], now=canonical_ts(0)
    )
    persistence.upsert_controls(
        [{"control_id": control_id, "title": "Updated", "risk_id": risk_id, "type": "detective"}],
        now=canonical_ts(1),
    )
    rows = [c for c in persistence.list_controls() if c["control_id"] == control_id]
    assert len(rows) == 1
    assert rows[0]["title"] == "Updated"
    assert rows[0]["type"] == "detective"
    assert rows[0]["created_at"] == canonical_ts(0)


def test_list_risks_and_controls_scoped_to_engagement(persistence, uid):
    eng_a = uid("ENG")
    eng_b = uid("ENG")
    risk_a = uid("RSK")
    risk_b = uid("RSK")
    persistence.upsert_risks(
        [
            {"risk_id": risk_a, "engagement_id": eng_a, "title": "a", "status": "proposed", "source": "manual"},
            {"risk_id": risk_b, "engagement_id": eng_b, "title": "b", "status": "proposed", "source": "manual"},
        ],
        now=canonical_ts(0),
    )
    scoped = {r["risk_id"] for r in persistence.list_risks(engagement_id=eng_a)}
    assert scoped == {risk_a}


# ── findings ──────────────────────────────────────────────────────────────────────────


def test_write_findings_round_trip_json_fields_intact(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    written = persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    assert len(written) == 1
    f = written[0]
    assert f["finding_id"] == finding["finding_id"]
    assert f["threshold_refs"] == finding["threshold_refs"]
    assert f["metrics_cited"] == finding["metrics_cited"]
    assert f["evidence_refs"] == []  # not produced by findings.py yet -- defaults to []
    assert f["management_questions"] == finding["management_questions"]
    assert f["review_state"] == "draft"

    listed = persistence.list_findings(run_id)
    assert listed == written


def test_write_findings_idempotent_rewrite_same_set_no_duplicates(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(1),
    )
    rows = persistence.list_findings(run_id)
    assert len(rows) == 1
    assert rows[0]["finding_id"] == finding["finding_id"]


def test_write_findings_rewrite_does_not_reset_advanced_review_state(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    persistence.set_finding_review_state(finding["finding_id"], to_state="prepared", actor="alice", now=canonical_ts(1))
    rewritten = persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(2),
    )
    assert rewritten[0]["review_state"] == "prepared"


def test_write_findings_shrinking_set_deletes_only_drafts(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    f1 = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1", severity="High")
    f2 = _finding(f"{run_id}:T2", rule_id=f"{skill_id}.T2", severity="Low")
    persistence.write_findings(
        run_id, [f1, f2], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    shrunk = persistence.write_findings(
        run_id, [f1], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(1),
    )
    assert [f["finding_id"] for f in shrunk] == [f1["finding_id"]]
    assert [f["finding_id"] for f in persistence.list_findings(run_id)] == [f1["finding_id"]]


def test_write_findings_shrinking_set_with_non_draft_finding_raises_and_leaves_data_untouched(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    f1 = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1", severity="High")
    f2 = _finding(f"{run_id}:T2", rule_id=f"{skill_id}.T2", severity="Low")
    persistence.write_findings(
        run_id, [f1, f2], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    persistence.set_finding_review_state(f2["finding_id"], to_state="prepared", actor="alice", now=canonical_ts(1))

    with pytest.raises(NonDraftFindingWouldBeDeleted):
        persistence.write_findings(
            run_id, [f1], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
            now=canonical_ts(2),
        )

    still = persistence.list_findings(run_id)
    assert {f["finding_id"] for f in still} == {f1["finding_id"], f2["finding_id"]}
    assert [f for f in still if f["finding_id"] == f2["finding_id"]][0]["review_state"] == "prepared"


def test_list_findings_ordered_by_severity_then_rule_id(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    low = _finding(f"{run_id}:B", rule_id=f"{skill_id}.B", severity="Low")
    high_a = _finding(f"{run_id}:A", rule_id=f"{skill_id}.A", severity="High")
    high_b = _finding(f"{run_id}:C", rule_id=f"{skill_id}.C", severity="High")
    medium = _finding(f"{run_id}:D", rule_id=f"{skill_id}.D", severity="Medium")
    persistence.write_findings(
        run_id, [low, high_b, medium, high_a], engagement_id="ENG-DEFAULT", skill_id=skill_id,
        skill_version="1.0.0", now=canonical_ts(0),
    )
    ordered = [f["rule_id"] for f in persistence.list_findings(run_id)]
    assert ordered == [f"{skill_id}.A", f"{skill_id}.C", f"{skill_id}.D", f"{skill_id}.B"]


def test_write_findings_defaults_rollforward_fields_when_absent(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    out = persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    assert out[0]["prior_finding_id"] is None
    assert out[0]["recurrence_count"] == 0


# ── finding review state ────────────────────────────────────────────────────────────


def test_set_finding_review_state_forward_step_by_step(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    for to_state in ("prepared", "reviewed", "approved"):
        updated = persistence.set_finding_review_state(
            finding["finding_id"], to_state=to_state, actor="alice", now=canonical_ts(1)
        )
        assert updated["review_state"] == to_state


def test_set_finding_review_state_rejects_skip_ahead(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    with pytest.raises(InvalidReviewStateTransition):
        persistence.set_finding_review_state(
            finding["finding_id"], to_state="approved", actor="alice", now=canonical_ts(1)
        )


def test_set_finding_review_state_rejects_backward(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    persistence.set_finding_review_state(finding["finding_id"], to_state="prepared", actor="alice", now=canonical_ts(1))
    with pytest.raises(InvalidReviewStateTransition):
        persistence.set_finding_review_state(finding["finding_id"], to_state="draft", actor="alice", now=canonical_ts(2))


def test_set_finding_review_state_rejects_same_state(persistence, uid):
    run_id = uid("RUN")
    skill_id = uid("SKILL")
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    with pytest.raises(InvalidReviewStateTransition):
        persistence.set_finding_review_state(finding["finding_id"], to_state="draft", actor="alice", now=canonical_ts(1))


def test_set_finding_review_state_missing_finding_raises(persistence, uid):
    with pytest.raises(FindingNotFound):
        persistence.set_finding_review_state(uid("RUN") + ":NOPE", to_state="prepared", actor="alice", now=canonical_ts(0))
