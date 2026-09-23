"""CLAUDE.md P2/P3 gate review item 1: orchestrator.nodes.fieldwork.prioritise's
exposure computation, exercised directly against contrived flagged_rows/
population data (never the shared tests/fixtures/skills/mini fixture's own
plan.yaml for the scenario data itself, which this session does not own -- the
mini skill's harness is reused only for its Skill/persistence/NodeContext
plumbing, with ctx.skill.plan and ctx.data_source.read_population monkeypatched
per test. The mini fixture's own plan.yaml/findings.yaml gained a
`missing_amount` metric on T2 in this change, so T2 remains a monetary finding
under the new rule below -- see test_p3_nodes.py's own exposure tests).

This module previously (P2/P3 gate review item 5) collapsed a group of flagged
rows into one monetary entry whenever every member happened to share the same
amount. That heuristic silently misfired on duplicate_detection: T5.2's
grouping key deliberately INCLUDES the amount column ("(Employee ID,
Transaction Date, Vendor, amount) exact"), so every duplicate group's members
always agree on amount by construction -- the heuristic collapsed every real
duplicate group to a single entry, discarding every line but one from the
run's headline exposure and producing an exposure_amount that disagreed with
the finding's own cited `duplicate_amount` metric (RUN-AE7BB758A9B9: observation
"$1,994.79 at risk" vs `exposure_amount` 1968.09).

Fixed two ways, tested below:
  * a finding's `exposure_amount` is now always the sum of its own cited
    amount metric(s) (`metrics_cited` intersected with the test's declared
    additive-AUD metrics) -- the same figure(s) already rendered into its
    observation text, never re-derived from raw rows;
  * the run-level headline's entry-grain collapse is driven by which
    PRIMITIVE a group's flag belongs to (`_GROUP_COLLAPSE_PRIMITIVES` --
    only ratio_per_group's attendee-grain shape), never inferred from
    whether a group's members happen to agree on amount.

Covers:
  * ratio_per_group's shared-amount group collapses to one entry in the
    headline (T3.3b's shape);
  * duplicate_detection's group, whose members share ONE amount BY
    CONSTRUCTION, is NOT collapsed in the headline -- this is the exact
    regression this fix targets;
  * split_detection's group of different amounts is not collapsed either;
  * a finding whose test declares no additive AUD metric gets
    exposure_amount=None / exposure_basis="non-monetary finding", never 0.0;
  * two findings citing overlapping flagged rows: the headline is the union
    of distinct entries, never the naive sum of per-finding totals (a second,
    more elaborate case than
    test_p3_nodes.py::test_prioritise_dedupes_exposure_across_findings);
  * a null or unparseable amount in a source a population declares
    `amount_column` for raises ContractViolation, never a fabricated 0.0.
"""

from __future__ import annotations

import pandas as pd
import pytest

from orchestrator.contract import ContractViolation
from orchestrator.nodes.fieldwork import prioritise
from tests.test_p3_nodes import _make_harness


def _flagged_row(source, row_key, flag, group_id=None):
    return {"source": source, "row_key": row_key, "flag": flag, "group_id": group_id}


def _finding(finding_id, *, test_id, title="A finding", severity="High", metrics_cited=None):
    return dict(
        finding_id=finding_id, rule_id=f"RULE.{test_id}", test_id=test_id, title=title,
        severity=severity, severity_rule=None, threshold_refs=[], proposed_severity=None,
        proposed_severity_reason=None, metrics_cited=metrics_cited or {}, observation="x",
        recommendation="x", management_questions=[], control_id=None, risk_id=None,
        assertion="operating", exposure_amount=None, exposure_basis=None, review_state="draft",
        analyst_set_severity=True, severity_basis="fixed",
    )


def _metric(value, unit="AUD"):
    return {"value": value, "unit": unit, "source_ref": {}}


def _rig(local_persistence, tmp_path, *, populations, tests, read_population):
    """Real mini-skill harness for its plumbing; plan/population/reader are
    replaced with the scenario under test."""
    h = _make_harness(local_persistence, tmp_path)
    h.ctx.skill.plan = {"populations": populations, "tests": tests}
    h.ctx.data_source.read_population = read_population
    return h


def test_ratio_per_group_shared_amount_collapses_to_one_headline_entry(local_persistence, tmp_path):
    # Three attendee rows of ONE $900 entertainment entry (T3.3b's shape) --
    # the headline must count this once, not three times.
    def read_population(source, *, version=None):
        assert source == "claims"
        return pd.DataFrame({"__row_key": ["a1", "a2", "a3"], "Entry Amount": [900.0, 900.0, 900.0]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"attendees_pop": {"source": "claims", "amount_column": "Entry Amount"}},
        tests=[{
            "test_id": "TG1", "flag": "RF_ENTERTAIN", "primitive": "ratio_per_group",
            "params": {
                "flag": "RF_ENTERTAIN",
                "metrics": {"ent_amount": {"kind": "sum", "column": "Entry Amount", "unit": "AUD"}},
            },
        }],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "a1", "RF_ENTERTAIN", group_id="G1"),
        _flagged_row("claims", "a2", "RF_ENTERTAIN", group_id="G1"),
        _flagged_row("claims", "a3", "RF_ENTERTAIN", group_id="G1"),
    ])
    finding = _finding("F1", test_id="TG1", metrics_cited={"ent_amount": _metric(900.0)})
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    f = state.findings[0]
    assert f["exposure_amount"] == 900.0, "exposure_amount is the finding's own cited metric"

    headline = h.persistence.get_run_metrics(state.run_id)["run_exposure_headline"]
    assert headline["value"] == 900.0, "three attendee rows of one $900 entry must collapse in the headline, not sum to $2700"


def test_duplicate_detection_group_with_equal_amounts_is_not_collapsed(local_persistence, tmp_path):
    # THE REGRESSION THIS FIX TARGETS (RUN-AE7BB758A9B9): duplicate_detection's
    # grouping key deliberately includes the amount column, so a duplicate
    # group's members ALWAYS share one amount by construction -- the old
    # "members agree on amount => collapse" heuristic silently discarded
    # every line but one from the headline for every real duplicate group.
    # Two duplicate $500 lines: the headline must count both.
    def read_population(source, *, version=None):
        assert source == "claims"
        return pd.DataFrame({"__row_key": ["d1", "d2"], "Amount": [500.0, 500.0]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"claims_pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{
            "test_id": "TG4", "flag": "RF_DUP", "primitive": "duplicate_detection",
            "params": {
                "flag": "RF_DUP",
                "metrics": {"dup_amount": {"kind": "value", "key": "dup_amount", "unit": "AUD"}},
            },
        }],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "d1", "RF_DUP", group_id="D1"),
        _flagged_row("claims", "d2", "RF_DUP", group_id="D1"),
    ])
    # duplicate_amount's real shape (spec T5.2) is "sum of extra lines beyond
    # the first in each group" -- one $500 line here, not both.
    finding = _finding("F4", test_id="TG4", metrics_cited={"dup_amount": _metric(500.0)})
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    f = state.findings[0]
    assert f["exposure_amount"] == 500.0, "exposure_amount must equal the finding's own cited metric, never a re-derived row/group sum"

    headline = h.persistence.get_run_metrics(state.run_id)["run_exposure_headline"]
    assert headline["value"] == 1000.0, "both $500 lines are distinct entries in the headline even though duplicate_detection made them share one amount"


def test_split_detection_group_of_different_amounts_is_not_collapsed(local_persistence, tmp_path):
    # Three DIFFERENT-amount lines clustered into one detected split group
    # (split_detection's group_id shape) -- each is its own headline entry.
    def read_population(source, *, version=None):
        assert source == "claims"
        return pd.DataFrame({"__row_key": ["c1", "c2", "c3"], "Amount": [1000.0, 1500.0, 2000.0]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"claims_pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{
            "test_id": "TG2", "primitive": "split_detection",
            "params": {
                "flag_same_day": "RF_SPLIT_SAMEDAY", "flag_window": "RF_SPLIT_WINDOW",
                "metrics": {"split_amount": {"kind": "value", "key": "split_amount", "unit": "AUD"}},
            },
        }],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "c1", "RF_SPLIT_SAMEDAY", group_id="SPLIT1"),
        _flagged_row("claims", "c2", "RF_SPLIT_SAMEDAY", group_id="SPLIT1"),
        _flagged_row("claims", "c3", "RF_SPLIT_SAMEDAY", group_id="SPLIT1"),
    ])
    finding = _finding("F2", test_id="TG2", metrics_cited={"split_amount": _metric(4500.0)})
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    f = state.findings[0]
    assert f["exposure_amount"] == 4500.0

    headline = h.persistence.get_run_metrics(state.run_id)["run_exposure_headline"]
    assert headline["value"] == 4500.0, "a split-claim group's three DIFFERENT amounts must all be summed, never collapsed"


def test_finding_with_no_declared_amount_metric_gets_none_never_zero(local_persistence, tmp_path):
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


def test_finding_citing_a_metric_its_test_never_declared_stays_non_monetary(local_persistence, tmp_path):
    # A finding's OWN metrics_cited is not enough on its own -- the metric
    # must also be a real additive-AUD metric the test's plan.yaml params
    # declared. Guards against a stray/renamed metric silently entering
    # exposure.
    h = _rig(
        local_persistence, tmp_path,
        populations={"claims_pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{"test_id": "TG5", "flag": "RF_X", "primitive": "threshold_exceedance", "params": {"flag": "RF_X"}}],
        read_population=lambda source, **k: pd.DataFrame({"__row_key": ["z1"], "Amount": [10.0]}),
    )
    h.persistence.write_flagged_rows(h.state.run_id, [_flagged_row("claims", "z1", "RF_X")])
    finding = _finding("F5", test_id="TG5", metrics_cited={"some_other_amount": _metric(999.0)})
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    assert state.findings[0]["exposure_amount"] is None


def test_overlapping_findings_never_double_count_the_headline(local_persistence, tmp_path):
    def read_population(source, *, version=None):
        return pd.DataFrame({"__row_key": ["r1", "r2", "r3"], "Amount": [100.0, 200.0, 300.0]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[
            {"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance",
             "params": {"flag": "RF_A", "metrics": {"a_amount": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}},
            {"test_id": "TB", "flag": "RF_B", "primitive": "threshold_exceedance",
             "params": {"flag": "RF_B", "metrics": {"b_amount": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}},
        ],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "r1", "RF_A"),
        _flagged_row("claims", "r2", "RF_A"),
        _flagged_row("claims", "r2", "RF_B"),  # shared row between TA and TB
        _flagged_row("claims", "r3", "RF_B"),
    ])
    fa = _finding("FA", test_id="TA", metrics_cited={"a_amount": _metric(300.0)})
    fb = _finding("FB", test_id="TB", metrics_cited={"b_amount": _metric(500.0)})
    h.persistence.write_findings(
        h.state.run_id, [fa, fb],
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


def test_group_collapse_primitive_with_disagreeing_amounts_raises(local_persistence, tmp_path):
    # A ratio_per_group-owned group_id is declared to share one amount --
    # if this run's data violates that assumption, fail loudly rather than
    # silently pick one member's amount as "the" entry amount.
    def read_population(source, *, version=None):
        return pd.DataFrame({"__row_key": ["a1", "a2"], "Entry Amount": [900.0, 950.0]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"attendees_pop": {"source": "claims", "amount_column": "Entry Amount"}},
        tests=[{
            "test_id": "TG1", "flag": "RF_ENTERTAIN", "primitive": "ratio_per_group",
            "params": {
                "flag": "RF_ENTERTAIN",
                "metrics": {"ent_amount": {"kind": "sum", "column": "Entry Amount", "unit": "AUD"}},
            },
        }],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "a1", "RF_ENTERTAIN", group_id="G1"),
        _flagged_row("claims", "a2", "RF_ENTERTAIN", group_id="G1"),
    ])
    finding = _finding("F1", test_id="TG1", metrics_cited={"ent_amount": _metric(1850.0)})
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    with pytest.raises(ContractViolation):
        prioritise(h.ctx, h.state)
