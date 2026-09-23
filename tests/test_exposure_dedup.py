"""orchestrator.nodes.fieldwork.prioritise's exposure computation, exercised
directly against contrived flagged_rows/population data (never the shared
tests/fixtures/skills/mini fixture's own plan.yaml for the scenario data
itself, which this session does not own -- the mini skill's harness is
reused only for its Skill/persistence/NodeContext plumbing, with
ctx.skill.plan, ctx.skill.contract's sources' entry_key and
ctx.data_source.read_population monkeypatched per test).

Two separate mechanisms, both covered here:

  * a finding's `exposure_amount` is always the sum of its own cited amount
    metric(s) (`metrics_cited` intersected with the additive-AUD metrics ANY
    plan test declares, CLAUDE.md P2/P3 gate review item B1) -- the same
    figure(s) already rendered into its observation text, never re-derived
    from raw rows;
  * the run headline ("Gross value of flagged spend (de-duplicated)", B2) is
    the sum, over 'spend'/'excess' findings only, of each source's own
    declared entry_key (contract.yaml, real columns) identifying one
    distinct transaction entry -- never a positional row index, never
    inferred from a primitive's group_id or from whether a group's members
    happen to agree on amount. This replaced an earlier, narrower mechanism
    (_GROUP_COLLAPSE_PRIMITIVES, keyed on group_id and which primitive
    produced it) that could not de-duplicate the same entry across two
    DIFFERENT findings/sources and had no real column identity at all.

Covers:
  * an attendee-grain population (T3.3b's shape: several rows sharing one
    entry_key) collapses to one entry in the headline;
  * two rows that a primitive's group_id ties together but whose entry_key
    genuinely differs (T5.2's shape: duplicate_detection's grouping key
    includes amount, so members always agree on amount by construction, but
    are still distinct transaction entries) are NOT collapsed;
  * a group of different-amount rows (T5.1's shape) is summed, not
    collapsed;
  * a finding whose test declares no additive AUD metric gets
    exposure_amount=None / exposure_basis="non-monetary finding", never 0.0;
  * a finding with monetary_basis='none' never contributes to the headline
    even if its rows share a source with a declared entry_key;
  * a finding with monetary_basis='approved_not_spent' contributes to
    run_approved_not_spent_total, never to the headline;
  * two 'spend'/'excess' findings citing overlapping flagged rows: the
    headline is the union of distinct entries, never the naive sum of
    per-finding totals;
  * a null or unparseable amount, or a missing entry_key column, in a source
    a population declares `amount_column`/contract declares `entry_key` for
    raises ContractViolation, never a fabricated 0.0 or a silent skip;
  * a 'spend'/'excess' finding whose source declares NO entry_key raises,
    rather than silently entering an undeduplicated headline;
  * two rows sharing one entry_key that disagree on amount raises (a
    genuine data-integrity problem, never guessed).
"""

from __future__ import annotations

import pandas as pd
import pytest

from orchestrator.contract import ContractViolation
from orchestrator.nodes.fieldwork import prioritise
from tests.test_p3_nodes import _make_harness


def _flagged_row(source, row_key, flag, group_id=None):
    return {"source": source, "row_key": row_key, "flag": flag, "group_id": group_id}


def _finding(finding_id, *, test_id, title="A finding", severity="High", metrics_cited=None, monetary_basis="spend"):
    return dict(
        finding_id=finding_id, rule_id=f"RULE.{test_id}", test_id=test_id, title=title,
        severity=severity, severity_rule=None, threshold_refs=[], proposed_severity=None,
        proposed_severity_reason=None, metrics_cited=metrics_cited or {}, observation="x",
        recommendation="x", management_questions=[], control_id=None, risk_id=None,
        assertion="operating", exposure_amount=None, exposure_basis=None, review_state="draft",
        analyst_set_severity=True, severity_basis="fixed", monetary_basis=monetary_basis,
    )


def _metric(value, unit="AUD"):
    return {"value": value, "unit": unit, "source_ref": {}}


def _rig(local_persistence, tmp_path, *, populations, tests, read_population, entry_keys=None):
    """Real mini-skill harness for its plumbing; plan/population/reader are
    replaced with the scenario under test. `entry_keys` (optional,
    {source: [columns]}) overrides/adds to the mini contract's own declared
    source entry_key -- a scenario that doesn't touch a source at all (e.g.
    the non-monetary-finding cases) leaves the mini contract's real "claims"
    entry_key in place but never has to read it (B2's scoping: entry_key
    sources are read only where plan.yaml's populations actually reference
    that source)."""
    h = _make_harness(local_persistence, tmp_path)
    h.ctx.skill.plan = {"populations": populations, "tests": tests}
    h.ctx.data_source.read_population = read_population
    for source, cols in (entry_keys or {}).items():
        h.ctx.skill.contract["sources"].setdefault(source, {})["entry_key"] = cols
    return h


def test_attendee_grain_population_collapses_to_one_headline_entry(local_persistence, tmp_path):
    # Three attendee rows of ONE entertainment entry (T3.3b's shape) --
    # all share the same entry_key (Employee ID, Transaction Date, Vendor,
    # Entry Amount), only __row_key (one per attendee) differs. The headline
    # must count this once, not three times.
    def read_population(source, *, version=None):
        assert source == "claims"
        return pd.DataFrame({
            "__row_key": ["a1", "a2", "a3"],
            "Employee ID": [1, 1, 1],
            "Transaction Date": ["2026-01-05"] * 3,
            "Vendor": ["VendorX"] * 3,
            "Entry Amount": [900.0, 900.0, 900.0],
        })

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
        entry_keys={"claims": ["Employee ID", "Transaction Date", "Vendor", "Entry Amount"]},
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "a1", "RF_ENTERTAIN", group_id="G1"),
        _flagged_row("claims", "a2", "RF_ENTERTAIN", group_id="G1"),
        _flagged_row("claims", "a3", "RF_ENTERTAIN", group_id="G1"),
    ])
    # B1: flags are now looked up via the RECORDED metric->test mapping
    # (write_run_metrics' own test_id column), not the finding's test_id.
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "ent_amount", "value": 900.0, "unit": "AUD", "source_ref": {}, "test_id": "TG1"},
    ])
    finding = _finding("F1", test_id="TG1", metrics_cited={"ent_amount": _metric(900.0)}, monetary_basis="spend")
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
    assert headline["source_ref"]["label"] == "Gross value of flagged spend (de-duplicated)"


def test_duplicate_lines_sharing_amount_by_construction_are_not_collapsed(local_persistence, tmp_path):
    # THE REGRESSION THIS FIX TARGETS (RUN-AE7BB758A9B9): duplicate_detection's
    # grouping key deliberately includes the amount column, so a duplicate
    # group's members ALWAYS share one amount by construction -- but they are
    # still two DISTINCT transaction entries (different Report Legacy Key in
    # the real Skill; here, a different Vendor spelling simulates two
    # separate submissions). entry_key-based de-duplication must not collapse
    # them just because a primitive's group_id ties them together.
    def read_population(source, *, version=None):
        assert source == "claims"
        return pd.DataFrame({
            "__row_key": ["d1", "d2"],
            "Employee ID": [1, 1],
            "Transaction Date": ["2026-01-05", "2026-01-05"],
            "Vendor": ["VendorX", "VendorX Pty Ltd"],  # distinct entry_key values
            "Amount": [500.0, 500.0],
        })

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
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "dup_amount", "value": 500.0, "unit": "AUD", "source_ref": {}, "test_id": "TG4"},
    ])
    # duplicate_amount's real shape (spec T5.2) is "sum of extra lines beyond
    # the first in each group" -- one $500 line here, not both; monetary
    # basis is 'excess' (B2 -- the double-payment excess at risk).
    finding = _finding("F4", test_id="TG4", metrics_cited={"dup_amount": _metric(500.0)}, monetary_basis="excess")
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
    # (split_detection's group_id shape) -- each has a distinct entry_key
    # (Amount differs) and is its own headline entry.
    def read_population(source, *, version=None):
        assert source == "claims"
        return pd.DataFrame({
            "__row_key": ["c1", "c2", "c3"],
            "Employee ID": [2, 2, 2],
            "Transaction Date": ["2026-01-10", "2026-01-11", "2026-01-12"],
            "Vendor": ["VendorY", "VendorY", "VendorY"],
            "Amount": [1000.0, 1500.0, 2000.0],
        })

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
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "split_amount", "value": 4500.0, "unit": "AUD", "source_ref": {}, "test_id": "TG2"},
    ])
    finding = _finding("F2", test_id="TG2", metrics_cited={"split_amount": _metric(4500.0)}, monetary_basis="spend")
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
        h.state.run_id, [_finding("F3", test_id="TG3", monetary_basis="none")],
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

    headline = h.persistence.get_run_metrics(state.run_id)["run_exposure_headline"]
    assert headline["value"] == 0.0


def test_finding_citing_a_metric_its_test_never_declared_stays_non_monetary(local_persistence, tmp_path):
    # A finding's OWN metrics_cited is not enough on its own -- the metric
    # must also be a real additive-AUD metric the test's plan.yaml params
    # declared. Guards against a stray/renamed metric silently entering
    # exposure.
    h = _rig(
        local_persistence, tmp_path,
        populations={"claims_pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{"test_id": "TG5", "flag": "RF_X", "primitive": "threshold_exceedance", "params": {"flag": "RF_X"}}],
        read_population=lambda source, **k: pd.DataFrame({
            "__row_key": ["z1"], "Employee ID": [9], "Transaction Date": ["2026-01-01"],
            "Vendor": ["V"], "Amount": [10.0],
        }),
    )
    h.persistence.write_flagged_rows(h.state.run_id, [_flagged_row("claims", "z1", "RF_X")])
    finding = _finding("F5", test_id="TG5", metrics_cited={"some_other_amount": _metric(999.0)}, monetary_basis="none")
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    assert state.findings[0]["exposure_amount"] is None


def test_approved_not_spent_finding_never_enters_the_headline(local_persistence, tmp_path):
    # B2: T3.1a's shape -- money approved but never spent is real, but it is
    # not "flagged spend". Reported separately (run_approved_not_spent_total),
    # never summed into the headline even though it has a real cited amount
    # metric and real flagged rows.
    def read_population(source, *, version=None):
        return pd.DataFrame({
            "__row_key": ["r1"], "Employee ID": [3], "Transaction Date": ["2026-01-20"],
            "Vendor": ["VendorZ"], "Amount": [2500.0],
        })

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance",
                 "params": {"flag": "RF_A", "metrics": {"a_amount": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}}],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [_flagged_row("claims", "r1", "RF_A")])
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "a_amount", "value": 2500.0, "unit": "AUD", "source_ref": {}, "test_id": "TA"},
    ])
    finding = _finding("FA", test_id="TA", metrics_cited={"a_amount": _metric(2500.0)}, monetary_basis="approved_not_spent")
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    assert state.findings[0]["exposure_amount"] == 2500.0

    metrics = h.persistence.get_run_metrics(state.run_id)
    assert metrics["run_exposure_headline"]["value"] == 0.0, "approved-but-unspent money must never enter the flagged-spend headline"
    assert metrics["run_approved_not_spent_total"]["value"] == 2500.0


def test_overlapping_findings_never_double_count_the_headline(local_persistence, tmp_path):
    def read_population(source, *, version=None):
        return pd.DataFrame({
            "__row_key": ["r1", "r2", "r3"],
            "Employee ID": [4, 4, 4],
            "Transaction Date": ["2026-02-01", "2026-02-02", "2026-02-03"],
            "Vendor": ["VendorW", "VendorW", "VendorW"],
            "Amount": [100.0, 200.0, 300.0],
        })

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
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "a_amount", "value": 300.0, "unit": "AUD", "source_ref": {}, "test_id": "TA"},
        {"metric_name": "b_amount", "value": 500.0, "unit": "AUD", "source_ref": {}, "test_id": "TB"},
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
        return pd.DataFrame({
            "__row_key": ["r1"], "Employee ID": [5], "Transaction Date": ["2026-01-01"],
            "Vendor": ["V"], "Amount": [None],
        })

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance"}],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [_flagged_row("claims", "r1", "RF_A")])
    h.persistence.write_findings(
        h.state.run_id, [_finding("FA", test_id="TA", monetary_basis="none")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    with pytest.raises(ContractViolation):
        prioritise(h.ctx, h.state)


def test_unparseable_amount_raises_contract_violation(local_persistence, tmp_path):
    def read_population(source, *, version=None):
        return pd.DataFrame({
            "__row_key": ["r1"], "Employee ID": [5], "Transaction Date": ["2026-01-01"],
            "Vendor": ["V"], "Amount": ["not-a-number"],
        })

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance"}],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [_flagged_row("claims", "r1", "RF_A")])
    h.persistence.write_findings(
        h.state.run_id, [_finding("FA", test_id="TA", monetary_basis="none")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    with pytest.raises(ContractViolation):
        prioritise(h.ctx, h.state)


def test_entries_sharing_a_key_but_disagreeing_on_amount_raises(local_persistence, tmp_path):
    # Two rows that resolve to the SAME entry_key (e.g. an attendee-grain
    # population) but carry different amounts is a genuine data-integrity
    # problem -- the same transaction entry cannot have two different
    # amounts. Fail loudly rather than silently pick one.
    def read_population(source, *, version=None):
        return pd.DataFrame({
            "__row_key": ["a1", "a2"],
            "Employee ID": [1, 1], "Transaction Date": ["2026-01-05", "2026-01-05"],
            "Vendor": ["VendorX", "VendorX"], "Entry Amount": [900.0, 950.0],
        })

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
        entry_keys={"claims": ["Employee ID", "Transaction Date", "Vendor"]},  # deliberately excludes Entry Amount
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "a1", "RF_ENTERTAIN", group_id="G1"),
        _flagged_row("claims", "a2", "RF_ENTERTAIN", group_id="G1"),
    ])
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "ent_amount", "value": 1850.0, "unit": "AUD", "source_ref": {}, "test_id": "TG1"},
    ])
    finding = _finding("F1", test_id="TG1", metrics_cited={"ent_amount": _metric(1850.0)}, monetary_basis="spend")
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    with pytest.raises(ContractViolation):
        prioritise(h.ctx, h.state)


def test_finding_citing_a_sibling_sub_tests_metrics_is_not_dropped(local_persistence, tmp_path):
    """B1 (CLAUDE.md P2/P3 gate review, RUN-5C6A997EC940): the T&E Skill
    splits one catalogue test into plan.yaml sub-tests that do NOT share a
    prefix with each other -- T6.1d_dom / T6.1d_int is exactly this shape,
    neither is a prefix of the other. A finding names only T6.1d_dom as its
    own test_id but cites metrics from BOTH sub-tests. The old
    `tid.startswith(f"{test_id}_")` matching silently dropped T6.1d_int's
    metric and flagged rows (exposure 446.40 when the observation text reads
    $446.40 AND $998.53). Fixed: a finding's amount metrics/flags are drawn
    from whichever test produced each cited metric (the run's own recorded
    metric->test mapping), never from string-matching the finding's test_id."""
    def read_population(source, *, version=None):
        return pd.DataFrame({
            "__row_key": ["d1", "i1"],
            "Employee ID": [6, 6], "Transaction Date": ["2026-03-01", "2026-03-02"],
            "Vendor": ["V1", "V2"], "Amount": [446.40, 998.53],
        })

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[
            {"test_id": "T6.1d_dom", "flag": "RF_DAILY_DOM", "primitive": "threshold_exceedance",
             "params": {"flag": "RF_DAILY_DOM",
                        "metrics": {"daily_over_amount_dom": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}},
            {"test_id": "T6.1d_int", "flag": "RF_DAILY_INT", "primitive": "threshold_exceedance",
             "params": {"flag": "RF_DAILY_INT",
                        "metrics": {"daily_over_amount_int": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}},
        ],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "d1", "RF_DAILY_DOM"),
        _flagged_row("claims", "i1", "RF_DAILY_INT"),
    ])
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "daily_over_amount_dom", "value": 446.40, "unit": "AUD", "source_ref": {}, "test_id": "T6.1d_dom"},
        {"metric_name": "daily_over_amount_int", "value": 998.53, "unit": "AUD", "source_ref": {}, "test_id": "T6.1d_int"},
    ])
    finding = _finding(
        "F6", test_id="T6.1d_dom",
        metrics_cited={"daily_over_amount_dom": _metric(446.40), "daily_over_amount_int": _metric(998.53)},
        monetary_basis="excess",
    )
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    state = prioritise(h.ctx, h.state)
    f = state.findings[0]
    assert f["exposure_amount"] == 1444.93, "must sum BOTH sub-tests' cited amount metrics, not just T6.1d_dom's"

    headline = h.persistence.get_run_metrics(state.run_id)["run_exposure_headline"]
    assert headline["value"] == 1444.93, "the headline must include the sibling sub-test's flagged row too"


def test_amount_column_population_with_no_binding_raises(local_persistence, tmp_path):
    """Item 2 (CLAUDE.md P2/P3 gate review): a population declaring
    amount_column for a source that has no binding in state.data_assets --
    an invariant discover() should already guarantee -- must fail loudly,
    never silently skip that source's contribution to the headline."""
    h = _rig(
        local_persistence, tmp_path,
        populations={"ghost_pop": {"source": "ghost", "amount_column": "Amount"}},
        tests=[{"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance"}],
        read_population=lambda source, **k: pd.DataFrame({"__row_key": [], "Amount": []}),
    )
    h.persistence.write_flagged_rows(h.state.run_id, [])
    h.persistence.write_findings(
        h.state.run_id, [_finding("FA", test_id="TA", monetary_basis="none")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    with pytest.raises(ContractViolation):
        prioritise(h.ctx, h.state)


def test_amount_column_not_in_read_data_raises(local_persistence, tmp_path):
    """Item 2 (CLAUDE.md P2/P3 gate review): a population's declared
    amount_column absent from the data actually read must fail loudly,
    never silently skip that source's contribution to the headline."""
    def read_population(source, *, version=None):
        return pd.DataFrame({"__row_key": ["r1"]})  # no "Amount" column

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[{"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance"}],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [_flagged_row("claims", "r1", "RF_A")])
    h.persistence.write_findings(
        h.state.run_id, [_finding("FA", test_id="TA", monetary_basis="none")],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    with pytest.raises(ContractViolation):
        prioritise(h.ctx, h.state)


def test_spend_finding_whose_source_declares_no_entry_key_raises(local_persistence, tmp_path):
    """B2: a 'spend'/'excess' finding cannot be safely de-duplicated in the
    headline without a declared entry_key -- must fail loudly rather than
    silently enter an undeduplicated (or arbitrarily deduplicated) headline."""
    def read_population(source, *, version=None):
        return pd.DataFrame({"__row_key": ["r1"], "Amount": [123.0]})

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "no_entry_key_source", "amount_column": "Amount"}},
        tests=[{"test_id": "TA", "flag": "RF_A", "primitive": "threshold_exceedance",
                 "params": {"flag": "RF_A", "metrics": {"a_amount": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}}],
        read_population=read_population,
    )
    h.ctx.skill.contract["sources"]["no_entry_key_source"] = {"columns": {}}  # no entry_key declared
    h.state = h.persistence.save_state(
        __import__("dataclasses").replace(
            h.state, data_assets=h.state.data_assets + [
                {"source": "no_entry_key_source", "table_fqn": "no_entry_key_source", "version": "v1"}
            ],
        )
    )
    h.persistence.write_flagged_rows(h.state.run_id, [_flagged_row("no_entry_key_source", "r1", "RF_A")])
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "a_amount", "value": 123.0, "unit": "AUD", "source_ref": {}, "test_id": "TA"},
    ])
    finding = _finding("FA", test_id="TA", metrics_cited={"a_amount": _metric(123.0)}, monetary_basis="spend")
    h.persistence.write_findings(
        h.state.run_id, [finding],
        engagement_id=h.state.engagement_id, skill_id=h.state.skill_id,
        skill_version=h.state.skill_version, now="2026-01-01T00:00:00.000000Z",
    )

    with pytest.raises(ContractViolation):
        prioritise(h.ctx, h.state)
