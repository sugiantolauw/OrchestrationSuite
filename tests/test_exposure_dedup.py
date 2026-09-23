"""CLAUDE.md P2/P3 gate review item 5: orchestrator.nodes.fieldwork.prioritise's
exposure de-duplication, exercised directly against contrived flagged_rows/
population data (never the shared tests/fixtures/skills/mini fixture's own
plan.yaml, which this session does not own -- the mini skill's harness is
reused only for its Skill/persistence/NodeContext plumbing, with
ctx.skill.plan and ctx.data_source.read_population monkeypatched per test).

Covers:
  * a group of flagged rows that all share ONE amount (the ratio_per_group
    attendee-grain shape, T3.3b in the real Skill) collapses to a single
    monetary entry, not summed once per row;
  * a group of flagged rows with DIFFERENT amounts (the split_detection
    detected-cluster shape, T5.1) is NOT collapsed -- every member is its
    own entry and all are summed;
  * a finding whose flags carry no amount_column at all gets
    exposure_amount=None / exposure_basis="non-monetary finding", never 0.0;
  * two findings citing overlapping flagged rows: exposure is only ever
    counted once per finding, and the run headline is the union, not the
    naive sum (a second, more elaborate case than
    test_p3_nodes.py::test_prioritise_dedupes_exposure_across_findings).
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from orchestrator.contract import ContractViolation
from orchestrator.nodes.fieldwork import prioritise
from tests.test_p3_nodes import _make_harness


def _flagged_row(source, row_key, flag, group_id=None):
    return {"source": source, "row_key": row_key, "flag": flag, "group_id": group_id}


def _finding(finding_id, *, test_id, title="A finding", severity="High"):
    return dict(
        finding_id=finding_id, rule_id=f"RULE.{test_id}", test_id=test_id, title=title,
        severity=severity, severity_rule=None, threshold_refs=[], proposed_severity=None,
        proposed_severity_reason=None, metrics_cited={}, observation="x", recommendation="x",
        management_questions=[], control_id=None, risk_id=None, assertion="operating",
        exposure_amount=None, exposure_basis=None, review_state="draft",
        analyst_set_severity=True, severity_basis="fixed",
    )


def _rig(local_persistence, tmp_path, *, populations, tests, read_population):
    """Real mini-skill harness for its plumbing; plan/population/reader are
    replaced with the scenario under test."""
    h = _make_harness(local_persistence, tmp_path)
    h.ctx.skill.plan = {"populations": populations, "tests": tests}
    h.ctx.data_source.read_population = read_population
    return h


def test_shared_amount_group_collapses_to_one_entry(local_persistence, tmp_path):
    # Three attendee rows of ONE $900 entertainment entry -- summing per row
    # would triple-count it; the fix collapses them to one $900 entry.
    # Source is "claims" (bound by _make_harness's mini-skill contract) even
    # though this scenario represents an attendee-grain population -- only
    # the source's binding needs to be real here, its content is stubbed.
    def read_population(source, *, version=None):
        assert source == "claims"
        return pd.DataFrame({"__row_key": ["a1", "a2", "a3"], "Entry Amount": [900.0, 900.0, 900.0]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"attendees_pop": {"source": "claims", "amount_column": "Entry Amount"}},
        tests=[{"test_id": "TG1", "flag": "RF_ENTERTAIN", "primitive": "ratio_per_group"}],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "a1", "RF_ENTERTAIN", group_id="G1"),
        _flagged_row("claims", "a2", "RF_ENTERTAIN", group_id="G1"),
        _flagged_row("claims", "a3", "RF_ENTERTAIN", group_id="G1"),
    ])
    h.persistence.write_findings(
        h.state.run_id, [_finding("F1", test_id="TG1")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    f = state.findings[0]
    assert f["exposure_amount"] == 900.0, "three attendee rows of one $900 entry must collapse, not sum to $2700"

    headline = h.persistence.get_run_metrics(state.run_id)["run_exposure_headline"]
    assert headline["value"] == 900.0


def test_different_amount_group_is_not_collapsed(local_persistence, tmp_path):
    # Three DIFFERENT-amount lines clustered into one detected split group
    # (split_detection's group_id shape) -- each is its own entry and all
    # three must be summed, never collapsed to one member's amount.
    def read_population(source, *, version=None):
        assert source == "claims"
        return pd.DataFrame({"__row_key": ["c1", "c2", "c3"], "Amount": [1000.0, 1500.0, 2000.0]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"claims_pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{
            "test_id": "TG2", "primitive": "split_detection",
            "params": {"flag_same_day": "RF_SPLIT_SAMEDAY", "flag_window": "RF_SPLIT_WINDOW"},
        }],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "c1", "RF_SPLIT_SAMEDAY", group_id="SPLIT1"),
        _flagged_row("claims", "c2", "RF_SPLIT_SAMEDAY", group_id="SPLIT1"),
        _flagged_row("claims", "c3", "RF_SPLIT_SAMEDAY", group_id="SPLIT1"),
    ])
    h.persistence.write_findings(
        h.state.run_id, [_finding("F2", test_id="TG2")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    f = state.findings[0]
    assert f["exposure_amount"] == 4500.0, "a split-claim group's three DIFFERENT amounts must all be summed"


def test_finding_with_no_monetary_column_gets_none_never_zero(local_persistence, tmp_path):
    h = _rig(
        local_persistence, tmp_path,
        populations={},  # no population declares an amount_column at all
        tests=[{"test_id": "TG3", "not_testable": {"reason": "n/a", "flags": ["RF_NONMONETARY"]}}],
        read_population=lambda source, **k: pd.DataFrame({"__row_key": []}),
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("approvals", "x1", "RF_NONMONETARY"),
    ])
    h.persistence.write_findings(
        h.state.run_id, [_finding("F3", test_id="TG3")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    assert state.findings[0]["exposure_amount"] is None
    # exposure_basis isn't part of RunState's compact projection -- check the
    # full persisted row.
    full = h.persistence.list_findings(state.run_id)[0]
    assert full["exposure_amount"] is None
    assert full["exposure_basis"] == "non-monetary finding"


def test_overlapping_findings_never_double_count_the_headline(local_persistence, tmp_path):
    def read_population(source, *, version=None):
        return pd.DataFrame({"__row_key": ["r1", "r2", "r3"], "Amount": [100.0, 200.0, 300.0]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[
            {"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance"},
            {"test_id": "TB", "flag": "RF_B", "primitive": "threshold_exceedance"},
        ],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "r1", "RF_A"),
        _flagged_row("claims", "r2", "RF_A"),
        _flagged_row("claims", "r2", "RF_B"),  # shared row between TA and TB
        _flagged_row("claims", "r3", "RF_B"),
    ])
    h.persistence.write_findings(
        h.state.run_id, [_finding("FA", test_id="TA"), _finding("FB", test_id="TB")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    by_id = {f["finding_id"]: f for f in state.findings}
    assert by_id["FA"]["exposure_amount"] == 300.0  # r1+r2
    assert by_id["FB"]["exposure_amount"] == 500.0  # r2+r3
    naive_sum = by_id["FA"]["exposure_amount"] + by_id["FB"]["exposure_amount"]
    assert naive_sum == 800.0

    headline = h.persistence.get_run_metrics(state.run_id)["run_exposure_headline"]
    assert headline["value"] == 600.0  # union {r1,r2,r3} = 100+200+300
    assert headline["value"] < naive_sum


def test_null_amount_raises_contract_violation_never_defaults_to_zero(local_persistence, tmp_path):
    def read_population(source, *, version=None):
        return pd.DataFrame({"__row_key": ["r1"], "Amount": [None]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance"}],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [_flagged_row("claims", "r1", "RF_A")])
    h.persistence.write_findings(
        h.state.run_id, [_finding("FA", test_id="TA")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    with pytest.raises(ContractViolation):
        prioritise(h.ctx, h.state)


def test_unparseable_amount_raises_contract_violation(local_persistence, tmp_path):
    def read_population(source, *, version=None):
        return pd.DataFrame({"__row_key": ["r1"], "Amount": ["not-a-number"]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance"}],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [_flagged_row("claims", "r1", "RF_A")])
    h.persistence.write_findings(
        h.state.run_id, [_finding("FA", test_id="TA")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    with pytest.raises(ContractViolation):
        prioritise(h.ctx, h.state)
