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
        severity_basis="threshold",
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
    skill_id = f"SKILL-{uid}"
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
    skill_id = f"SKILL-{uid}"
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
    skill_id = f"SKILL-{uid}"
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
    skill_id = f"SKILL-{uid}-a"
    other_skill_id = f"SKILL-{uid}-b"
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
    assert persistence.get_skill_version(f"SKILL-{uid}", "9.9.9") is None


# ── risk / control register ──────────────────────────────────────────────────────────


def test_upsert_risks_insert_then_update_descriptive_fields(persistence, uid):
    risk_id = f"RSK-{uid}"
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
    risk_id = f"RSK-{uid}"
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
    risk_id = f"RSK-{uid}"
    persistence.upsert_risks(
        [{"risk_id": risk_id, "title": "t", "status": "rejected", "source": "manual"}], now=canonical_ts(0)
    )
    with pytest.raises(RiskStatusRegression):
        persistence.upsert_risks(
            [{"risk_id": risk_id, "title": "t", "status": "proposed", "source": "manual"}], now=canonical_ts(1)
        )


def test_upsert_risks_forward_status_movement_allowed(persistence, uid):
    risk_id = f"RSK-{uid}"
    persistence.upsert_risks(
        [{"risk_id": risk_id, "title": "t", "status": "proposed", "source": "manual"}], now=canonical_ts(0)
    )
    persistence.upsert_risks(
        [{"risk_id": risk_id, "title": "t", "status": "accepted", "source": "manual"}], now=canonical_ts(1)
    )
    rows = [r for r in persistence.list_risks() if r["risk_id"] == risk_id]
    assert rows[0]["status"] == "accepted"


def test_upsert_controls_insert_then_update(persistence, uid):
    control_id = f"CTL-{uid}"
    risk_id = f"RSK-{uid}"
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


def test_upsert_risks_single_call_with_mixed_new_and_existing_rows(persistence, uid):
    # P3/P4 perf gap review 2026-09-25: upsert_risks/upsert_controls now issue
    # a single batched write per call rather than one per row -- this pins
    # that a multi-row batch containing BOTH a pre-existing risk (must
    # UPDATE, keep its created_at) and a brand-new one (must INSERT) is
    # handled correctly in one call, not just the single-risk-per-call shape
    # the other tests above exercise.
    existing_id = f"RSK-{uid}-existing"
    new_id = f"RSK-{uid}-new"
    persistence.upsert_risks(
        [{"risk_id": existing_id, "title": "Original", "status": "proposed", "source": "manual"}],
        now=canonical_ts(0),
    )
    persistence.upsert_risks(
        [
            {"risk_id": existing_id, "title": "Updated", "status": "accepted", "source": "manual"},
            {"risk_id": new_id, "title": "Brand new", "status": "proposed", "source": "manual"},
        ],
        now=canonical_ts(1),
    )
    rows = {r["risk_id"]: r for r in persistence.list_risks() if r["risk_id"] in (existing_id, new_id)}
    assert rows[existing_id]["title"] == "Updated"
    assert rows[existing_id]["status"] == "accepted"
    assert rows[existing_id]["created_at"] == canonical_ts(0)  # sticky: first-seen timestamp
    assert rows[new_id]["title"] == "Brand new"
    assert rows[new_id]["created_at"] == canonical_ts(1)


def test_upsert_controls_single_call_with_mixed_new_and_existing_rows(persistence, uid):
    existing_id = f"CTL-{uid}-existing"
    new_id = f"CTL-{uid}-new"
    persistence.upsert_controls(
        [{"control_id": existing_id, "title": "Original", "risk_id": f"RSK-{uid}"}], now=canonical_ts(0)
    )
    persistence.upsert_controls(
        [
            {"control_id": existing_id, "title": "Updated", "risk_id": f"RSK-{uid}", "type": "detective"},
            {"control_id": new_id, "title": "Brand new", "risk_id": f"RSK-{uid}"},
        ],
        now=canonical_ts(1),
    )
    rows = {c["control_id"]: c for c in persistence.list_controls() if c["control_id"] in (existing_id, new_id)}
    assert rows[existing_id]["title"] == "Updated"
    assert rows[existing_id]["type"] == "detective"
    assert rows[existing_id]["created_at"] == canonical_ts(0)
    assert rows[new_id]["title"] == "Brand new"
    assert rows[new_id]["created_at"] == canonical_ts(1)


def test_list_risks_and_controls_scoped_to_engagement(persistence, uid):
    eng_a = f"ENG-{uid}-a"
    eng_b = f"ENG-{uid}-b"
    risk_a = f"RSK-{uid}-a"
    risk_b = f"RSK-{uid}-b"
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
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
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


def test_write_findings_persists_analyst_set_severity_and_severity_basis(persistence, uid):
    """CLAUDE.md §0.4/G8, P2/P3 gate review item 3: analyst_set_severity and
    severity_basis (migration 004) must round-trip through both backends and
    must NEVER default to False/None when the caller supplied them -- a
    finding with a fixed (non-threshold) severity and one with an
    analyst-set threshold severity are both exercised, and a finding that
    omits them entirely (a caller bug) must come back None, not False."""
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
    fixed = _finding(
        f"{run_id}:T1", rule_id=f"{skill_id}.T1",
        analyst_set_severity=True, severity_basis="fixed",
    )
    threshold = _finding(
        f"{run_id}:T2", rule_id=f"{skill_id}.T2",
        analyst_set_severity=False, severity_basis="threshold",
    )
    omitted = dict(_finding(f"{run_id}:T3", rule_id=f"{skill_id}.T3"))
    omitted.pop("analyst_set_severity", None)
    omitted.pop("severity_basis", None)

    written = persistence.write_findings(
        run_id, [fixed, threshold, omitted], engagement_id="ENG-DEFAULT",
        skill_id=skill_id, skill_version="1.0.0", now=canonical_ts(0),
    )
    by_id = {f["finding_id"]: f for f in written}
    assert by_id[fixed["finding_id"]]["analyst_set_severity"] is True
    assert by_id[fixed["finding_id"]]["severity_basis"] == "fixed"
    assert by_id[threshold["finding_id"]]["analyst_set_severity"] is False
    assert by_id[threshold["finding_id"]]["severity_basis"] == "threshold"
    assert by_id[omitted["finding_id"]]["analyst_set_severity"] is None
    assert by_id[omitted["finding_id"]]["severity_basis"] is None

    listed = {f["finding_id"]: f for f in persistence.list_findings(run_id)}
    assert listed[fixed["finding_id"]]["analyst_set_severity"] is True
    assert listed[threshold["finding_id"]]["analyst_set_severity"] is False
    assert listed[omitted["finding_id"]]["analyst_set_severity"] is None


def test_write_findings_persists_indeterminate_severity(persistence, uid):
    """Item 4 (CLAUDE.md NN14, P2/P3 gate review), migration 005: a finding
    whose severity ladder could not be evaluated (a missing metric) round-
    trips its severity='Indeterminate' / severity_basis='indeterminate'
    through both backends -- the CHECK constraint on both must accept these
    exact values, not just 'High'/'Medium'/'Low' and 'fixed'/'threshold'."""
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
    finding = _finding(
        f"{run_id}:T1", rule_id=f"{skill_id}.T1",
        severity="Indeterminate", severity_rule="severity rule 'x > thresholds.y' could not be evaluated: metric(s) ['x'] unavailable",
        analyst_set_severity=False, severity_basis="indeterminate",
    )
    written = persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    assert written[0]["severity"] == "Indeterminate"
    assert written[0]["severity_basis"] == "indeterminate"
    assert written[0]["analyst_set_severity"] is False

    listed = persistence.list_findings(run_id)
    assert listed[0]["severity"] == "Indeterminate"
    assert listed[0]["severity_basis"] == "indeterminate"


def test_write_findings_persists_monetary_basis(persistence, uid):
    """B2 (CLAUDE.md P2/P3 gate review), migration 006: every valid
    monetary_basis value round-trips through both backends -- the CHECK
    constraint must accept 'spend', 'excess', 'approved_not_spent' and
    'none', and a finding that omits it comes back None (never defaulted)."""
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
    findings = [
        _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1", monetary_basis="spend"),
        _finding(f"{run_id}:T2", rule_id=f"{skill_id}.T2", monetary_basis="excess"),
        _finding(f"{run_id}:T3", rule_id=f"{skill_id}.T3", monetary_basis="approved_not_spent"),
        _finding(f"{run_id}:T4", rule_id=f"{skill_id}.T4", monetary_basis="none"),
    ]
    omitted = dict(_finding(f"{run_id}:T5", rule_id=f"{skill_id}.T5"))
    omitted.pop("monetary_basis", None)
    findings.append(omitted)

    written = persistence.write_findings(
        run_id, findings, engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    by_id = {f["finding_id"]: f for f in written}
    assert by_id[f"{run_id}:T1"]["monetary_basis"] == "spend"
    assert by_id[f"{run_id}:T2"]["monetary_basis"] == "excess"
    assert by_id[f"{run_id}:T3"]["monetary_basis"] == "approved_not_spent"
    assert by_id[f"{run_id}:T4"]["monetary_basis"] == "none"
    assert by_id[f"{run_id}:T5"]["monetary_basis"] is None

    listed = {f["finding_id"]: f for f in persistence.list_findings(run_id)}
    assert listed[f"{run_id}:T1"]["monetary_basis"] == "spend"
    assert listed[f"{run_id}:T5"]["monetary_basis"] is None


def test_write_findings_idempotent_rewrite_same_set_no_duplicates(persistence, uid):
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
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
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
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
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
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
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
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
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
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
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    out = persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    assert out[0]["prior_finding_id"] is None
    assert out[0]["recurrence_count"] == 0


# ── finding review state ────────────────────────────────────────────────────────────


def test_set_finding_review_state_forward_step_by_step(persistence, uid):
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
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
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
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
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    persistence.set_finding_review_state(finding["finding_id"], to_state="prepared", actor="alice", now=canonical_ts(1))
    with pytest.raises(InvalidReviewStateTransition):
        persistence.set_finding_review_state(finding["finding_id"], to_state="draft", actor="alice", now=canonical_ts(2))


def test_set_finding_review_state_rejects_same_state(persistence, uid):
    run_id = f"RUN-{uid}"
    skill_id = f"SKILL-{uid}"
    finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    persistence.write_findings(
        run_id, [finding], engagement_id="ENG-DEFAULT", skill_id=skill_id, skill_version="1.0.0",
        now=canonical_ts(0),
    )
    with pytest.raises(InvalidReviewStateTransition):
        persistence.set_finding_review_state(finding["finding_id"], to_state="draft", actor="alice", now=canonical_ts(1))


def test_set_finding_review_state_missing_finding_raises(persistence, uid):
    with pytest.raises(FindingNotFound):
        persistence.set_finding_review_state(f"RUN-{uid}:NOPE", to_state="prepared", actor="alice", now=canonical_ts(0))
