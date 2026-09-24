"""T-X1/T-X2 (docs/specs/P6_narration_design.md §11, WP N4): the exposure
computation `orchestrator.nodes.fieldwork.prioritise` extracted into
`orchestrator.exposure` (§5.3, item 6 of "Design in one page").

  T-X1  refactored `prioritise` gives byte-identical findings exposure and
        `run_metrics` to a golden snapshot taken BEFORE the refactor
        (tests/fixtures/exposure_golden/*.json, captured against the
        pre-refactor code from this WP's own commit history) -- on both the
        fast planted fixture (exact run_id/clock pinned, so the comparison
        is truly byte-identical including finding_id/created_at) and the
        real SKILL-001 run against synthetic_data/ (run-identity/timestamp
        fields normalized away, since `service.start_audit_run` assigns
        both -- see `_normalize_findings`).
  T-X2  `exposure.headline` is a pure reduction over per-finding line maps:
        no acceptances (an empty additional map) leaves the headline
        unchanged; an additional accepted map's lines are deduplicated by
        MAX against the existing maps, never summed. `finalise` (P6 WP N10,
        not yet built) will call exactly this function over rule findings
        plus accepted AI-proposed candidates' own `exposure.
        finding_line_values` maps -- this is the unit-level test of that
        building block, since the node itself does not exist yet.

Also covers `orchestrator.exposure`'s own dedup rules directly (not only via
the full `prioritise` pipeline test_exposure_dedup.py already exercises):
max per distinct line, spend vs excess basis, a non-unique entry_key
raising loudly, `build_test_line_values` covering a test NO finding cites
(the AI-proposed-candidate anchor-metric case, §5.1 C-2),
`put_test_line_values`'s idempotent replace-per-run semantics (CLAUDE.md
§2.3 rule 1), and the coordinator fix (2026-09-24, NN14): a flagged row on
an amount-bearing test that matches none of its sibling sub-tests'
re-derived groups (T6.1d_dom/_int's shape) raises `ContractViolation` at
build time instead of being silently left out of `test_line_values`."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from orchestrator import exposure
from orchestrator.contract import ContractViolation
from orchestrator.nodes.fieldwork import classify, discover, execute, find, prioritise
from tests.test_exposure_dedup import _finding, _flagged_row, _metric, _rig
from tests.test_p3_nodes import _make_harness
from tests.test_p3_tne_gates import DATA_DIR as PLANTED_DATA_DIR
from tests.test_p3_tne_gates import _make_ctx_and_state

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "exposure_golden"
SYNTHETIC_DATA_DIR = Path(__file__).parent.parent / "synthetic_data"

pytestmark = pytest.mark.skipif(
    not (GOLDEN_DIR / "planted_fixture_prioritise_snapshot.json").is_file(),
    reason="golden snapshot missing -- see this file's own module docstring",
)

# run_id-derived / real-clock fields a `service.start_audit_run` run cannot
# pin, so the synthetic-run comparison strips them; the planted-fixture
# comparison pins run_id/clock exactly instead and needs none of this.
_VOLATILE_FINDING_FIELDS = {"run_id", "finding_id", "created_at", "updated_at"}


def _normalize_findings(findings: list[dict]) -> list[dict]:
    return [{k: v for k, v in f.items() if k not in _VOLATILE_FINDING_FIELDS} for f in findings]


def _load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text())


# ── T-X1: golden snapshot, planted fixture (exact run_id/clock -> literal byte-identical) ──


def test_x1_planted_fixture_byte_identical_to_pre_refactor_snapshot(local_persistence):
    golden = _load_golden("planted_fixture_prioritise_snapshot.json")

    ctx, state = _make_ctx_and_state(local_persistence, PLANTED_DATA_DIR, run_id="RUN-GOLDEN-PLANTED")
    state = discover(ctx, state)
    state = execute(ctx, state)
    state = classify(ctx, state)
    state = find(ctx, state)
    state = prioritise(ctx, state)

    findings = local_persistence.list_findings(state.run_id)
    metrics = local_persistence.get_run_metrics(state.run_id)

    assert findings == golden["findings"], "findings must be byte-identical to the pre-refactor snapshot"
    assert metrics["run_exposure_headline"] == golden["run_exposure_headline"]
    assert metrics["run_approved_not_spent_total"] == golden["run_approved_not_spent_total"]


# ── T-X1: golden snapshot, real SKILL-001 run against synthetic_data/ ──


@pytest.mark.skipif(not SYNTHETIC_DATA_DIR.is_dir(), reason="synthetic_data/ not present in this checkout")
@pytest.mark.skipif(
    not (GOLDEN_DIR / "skill001_synthetic_prioritise_snapshot.json").is_file(),
    reason="SKILL-001 synthetic golden snapshot missing",
)
def test_x1_skill001_synthetic_run_matches_pre_refactor_snapshot(tmp_path):
    # Slow (a full run against the real ~30MB synthetic_data/ files -- see
    # tests/test_p3_synthetic_full_run.py's own docstring for why that file
    # is kept separate from the fast suite; this test is the same shape).
    # `service.start_audit_run` assigns its own run_id and real-clock
    # timestamps, so the comparison is normalized (_normalize_findings)
    # rather than a literal full-row diff -- everything exposure-relevant
    # (ordering, severity, exposure_amount, exposure_basis, monetary_basis,
    # metrics_cited) still is compared exactly.
    import time

    from orchestrator import service

    golden = _load_golden("skill001_synthetic_prioritise_snapshot.json")

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(SYNTHETIC_DATA_DIR),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "ORCH_WORKER_ID": "test-exposure-lines-x1",
        "CODE_REVISION": "test-exposure-lines-x1",
    }
    ctx = service.build_app_context(env)
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-001")
        assert all(bindings.values()), bindings
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-001", bindings=bindings,
            audit_period=("2025-01-01", "2026-04-30"),
            objective="T-X1 golden regression", run_owner="tester",
        )
        deadline = time.time() + 600
        status = None
        while time.time() < deadline:
            status = service.get_run(ctx, run_id)["status"]
            if status in ("awaiting_signoff", "failed"):
                break
            time.sleep(1)
        assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")

        findings = ctx.persistence.list_findings(run_id)
        metrics = ctx.persistence.get_run_metrics(run_id)
    finally:
        ctx.executor.stop()

    assert _normalize_findings(findings) == _normalize_findings(golden["findings"])
    assert metrics["run_exposure_headline"] == golden["run_exposure_headline"]
    assert metrics["run_approved_not_spent_total"] == golden["run_approved_not_spent_total"]


# ── T-X2: exposure.headline is a pure reduction ──────────────────────────


def test_x2_headline_with_no_additional_maps_is_unchanged():
    existing = [{"a": 100.0, "b": 50.0}]
    assert exposure.headline(existing) == 150.0
    # An "accepted" candidate that contributes an EMPTY map (the no-
    # acceptance case `finalise` hits when sign-off decides nothing) must
    # leave the headline exactly as `prioritise` already computed it.
    assert exposure.headline(existing + [{}]) == 150.0


def test_x2_headline_max_per_line_never_a_sum_across_maps():
    # Two "findings" (one already a rule finding, one an accepted AI-
    # proposed candidate, from `finalise`'s point of view) both attribute a
    # value to the SAME line -- the headline takes the larger, never the sum.
    rule_finding_map = {"lineA": 100.0, "lineB": 30.0}
    accepted_candidate_map = {"lineA": 40.0, "lineC": 20.0}
    assert exposure.headline([rule_finding_map, accepted_candidate_map]) == 150.0, (
        "lineA must count once at its larger value (100), never 100+40"
    )


def test_x2_headline_empty_input_is_zero():
    assert exposure.headline([]) == 0.0


# ── dedup rules, direct against exposure.py's own functions ─────────────


def test_finding_line_values_max_per_distinct_line_within_one_finding():
    # A finding whose own flagged rows include the SAME line twice (two
    # different flags on one physical row, both belonging to its producing
    # tests) must not double-count that line within its own map.
    rows_by_test_id = {
        "T1": [
            {"test_id": "T1", "source": "claims", "row_key": "r1", "line_key": "L1",
             "spend_amount": 100.0, "excess_amount": None},
        ],
        "T2": [
            {"test_id": "T2", "source": "claims", "row_key": "r1", "line_key": "L1",
             "spend_amount": 100.0, "excess_amount": None},
        ],
    }
    flagged_rows = [
        {"source": "claims", "row_key": "r1", "flag": "RF_A"},
        {"source": "claims", "row_key": "r1", "flag": "RF_B"},
    ]
    line_map = exposure.finding_line_values(
        flagged_rows, "spend", {"T1", "T2"}, rows_by_test_id,
        entry_key_cols_by_source={}, entry_key_by_row={}, row_amount={("claims", "r1"): 100.0},
    )
    assert line_map == {json.dumps(["claims", "r1"]): 100.0}


def test_finding_line_values_spend_basis_uses_row_amount_directly():
    line_map = exposure.finding_line_values(
        [{"source": "claims", "row_key": "r1", "flag": "RF_A"}],
        "spend", {"T1"}, rows_by_test_id={},
        entry_key_cols_by_source={}, entry_key_by_row={}, row_amount={("claims", "r1"): 250.0},
    )
    assert line_map == {json.dumps(["claims", "r1"]): 250.0}


def test_finding_line_values_excess_basis_reads_from_test_line_values_not_spend_amount():
    rows_by_test_id = {
        "T1": [
            {"test_id": "T1", "source": "claims", "row_key": "r1", "line_key": "L1",
             "spend_amount": 500.0, "excess_amount": 120.0},
        ],
    }
    line_map = exposure.finding_line_values(
        [{"source": "claims", "row_key": "r1", "flag": "RF_DUP"}],
        "excess", {"T1"}, rows_by_test_id,
        entry_key_cols_by_source={}, entry_key_by_row={}, row_amount={("claims", "r1"): 500.0},
    )
    assert line_map == {json.dumps(["claims", "r1"]): 120.0}, "excess basis must use excess_amount, never the full spend_amount"


def test_finding_line_values_excess_basis_raises_when_no_producing_test_resolved_it():
    line_map_input = [{"source": "claims", "row_key": "r1", "flag": "RF_DUP"}]
    with pytest.raises(ContractViolation, match="not in an exceeding group"):
        exposure.finding_line_values(
            line_map_input, "excess", {"T1"}, rows_by_test_id={},
            entry_key_cols_by_source={}, entry_key_by_row={}, row_amount={("claims", "r1"): 500.0},
        )


def test_approved_not_spent_never_enters_line_maps(local_persistence, tmp_path):
    # Direct exposure.compute_run_exposure call (not through prioritise) --
    # an 'approved_not_spent' finding must never contribute to headline_exposure.
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

    rows_by_flag = {"RF_A": h.persistence.list_flagged_rows(h.state.run_id)}
    existing_metrics = h.persistence.get_run_metrics(h.state.run_id)
    outcome = exposure.compute_run_exposure(h.ctx, h.state, h.ctx.skill, [finding], existing_metrics, rows_by_flag)

    assert outcome["headline_exposure"] == 0.0
    assert outcome["approved_not_spent_total"] == 2500.0
    assert outcome["updated_findings"][0]["exposure_amount"] == 2500.0


def test_non_unique_entry_key_without_repeats_fails_loudly():
    # Directly against exposure.read_line_amounts -- the same check
    # test_exposure_dedup.py exercises through the full prioritise pipeline,
    # here isolated to the extracted function itself.
    class _FakeDataSource:
        def read_population(self, source, *, version=None, **_):
            assert source == "claims"
            return pd.DataFrame({
                "__row_key": ["a", "b"],
                "Employee ID": [1, 1],
                "Transaction Date": ["2026-01-05", "2026-01-06"],  # differs -- key not actually unique-per-row
                "Vendor": ["V", "V"],
                "Amount": [100.0, 100.0],
            })

    class _FakeSkill:
        contract = {"sources": {"claims": {"entry_key": ["Employee ID"], "entry_key_repeats": False}}}

    class _FakeCtx:
        data_source = _FakeDataSource()

    with pytest.raises(ContractViolation, match="is not unique"):
        exposure.read_line_amounts(
            _FakeCtx(), _FakeSkill(), bindings={"claims": "v1"},
            amount_col_by_source={"claims": "Amount"},
            entry_key_cols_by_source={"claims": ["Employee ID"]},
        )


def test_build_test_line_values_covers_a_test_no_finding_cites(local_persistence, tmp_path):
    # §5.1 C-2 (AI-proposed candidates' anchor metric): a test's flagged
    # rows must land in test_line_values even when the current finding set
    # (rule findings only, in this WP) never cites it -- build_test_line_values
    # iterates every TESTABLE plan test, not only the ones `prioritise`'s
    # finding loop happens to touch.
    def read_population(source, *, version=None):
        return pd.DataFrame({
            "__row_key": ["r1", "r2"],
            "Employee ID": [1, 2],
            "Transaction Date": ["2026-01-05", "2026-01-06"],
            "Vendor": ["V1", "V2"],
            "Amount": [100.0, 200.0],
        })

    h = _rig(
        local_persistence, tmp_path,
        populations={"pop": {"source": "claims", "amount_column": "Amount"}},
        tests=[
            {"test_id": "TCITED", "flag": "RF_CITED", "primitive": "threshold_exceedance",
             "params": {"flag": "RF_CITED", "metrics": {"cited_amount": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}},
            {"test_id": "TUNCITED", "flag": "RF_UNCITED", "primitive": "threshold_exceedance",
             "params": {"flag": "RF_UNCITED", "metrics": {"uncited_amount": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}},
        ],
        read_population=read_population,
    )
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "r1", "RF_CITED"),
        _flagged_row("claims", "r2", "RF_UNCITED"),
    ])
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "cited_amount", "value": 100.0, "unit": "AUD", "source_ref": {}, "test_id": "TCITED"},
        {"metric_name": "uncited_amount", "value": 200.0, "unit": "AUD", "source_ref": {}, "test_id": "TUNCITED"},
    ])
    # Only TCITED gets a rule finding -- TUNCITED's flagged row is never
    # referenced by any persisted finding.
    finding = _finding("FC", test_id="TCITED", metrics_cited={"cited_amount": _metric(100.0)}, monetary_basis="spend")

    rows_by_flag = {
        "RF_CITED": [h.persistence.list_flagged_rows(h.state.run_id, "RF_CITED")[0]],
        "RF_UNCITED": [h.persistence.list_flagged_rows(h.state.run_id, "RF_UNCITED")[0]],
    }
    existing_metrics = h.persistence.get_run_metrics(h.state.run_id)
    outcome = exposure.compute_run_exposure(h.ctx, h.state, h.ctx.skill, [finding], existing_metrics, rows_by_flag)

    test_ids_in_output = {row["test_id"] for row in outcome["test_line_values"]}
    assert test_ids_in_output == {"TCITED", "TUNCITED"}, (
        "TUNCITED has no rule finding at all, but its flagged row must still land in "
        "test_line_values for a future AI-proposed candidate to use"
    )
    uncited_row = next(r for r in outcome["test_line_values"] if r["test_id"] == "TUNCITED")
    assert uncited_row["spend_amount"] == 200.0


def test_build_test_line_values_raises_when_a_row_matches_no_sibling_sub_tests_group(local_persistence, tmp_path):
    # Coordinator fix (2026-09-24, NN14): two sibling sub-tests sharing one
    # flag NAME (T6.1d_dom/_int's shape -- both would write
    # RF_CS_DailySpendOverLimit) each re-derive their OWN group_by
    # threshold_exceedance population. A row legitimately absent from ONE
    # sibling's own re-derived group (because it belongs to the other) must
    # stay silent -- r1 (TDOM's own group) and rX (TINT's own group) below
    # are each claimed by exactly one sibling and must NOT raise. But r3 is
    # flagged under the shared flag, prices on an amount-bearing source, and
    # is filtered out of BOTH siblings' own re-derived populations -- it
    # must raise ContractViolation at build time, never be silently dropped
    # (which would silently under-count the headline with no signal).
    def read_population(source, *, version=None):
        if source == "claims":
            # pop_dom filters to Employee ID == 1 below, so r3 (Employee ID
            # 99) is present in the raw source (row_amount sees it) but
            # never enters TDOM's own re-derived group_by population.
            return pd.DataFrame({
                "__row_key": ["r1", "r3"],
                "Employee ID": [1, 99],
                "Transaction Date": ["2026-01-05", "2026-01-07"],
                "Vendor": ["V1", "V3"],
                "Amount": [600.0, 50.0],
            })
        assert source == "register"
        return pd.DataFrame({
            "__row_key": ["rX"], "Employee ID": [2], "Transaction Date": ["2026-01-06"],
            "Vendor": ["V2"], "Amount": [700.0],
        })

    h = _rig(
        local_persistence, tmp_path,
        populations={
            "pop_dom": {
                "source": "claims", "amount_column": "Amount",
                "filters": [{"column": "Employee ID", "op": "eq", "value": 1}],
            },
            "pop_int": {"source": "register", "amount_column": "Amount"},
        },
        tests=[
            {"test_id": "TDOM", "flag": "RF_OVERLIMIT", "primitive": "threshold_exceedance",
             "params": {"flag": "RF_OVERLIMIT", "group_by": ["Employee ID", "Transaction Date"],
                        "column": "Amount", "limit": {"threshold": "daily_limit"}, "population": "pop_dom",
                        "metrics": {"dom_over_amount": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}},
            {"test_id": "TINT", "flag": "RF_OVERLIMIT", "primitive": "threshold_exceedance",
             "params": {"flag": "RF_OVERLIMIT", "group_by": ["Employee ID", "Transaction Date"],
                        "column": "Amount", "limit": {"threshold": "daily_limit"}, "population": "pop_int",
                        "metrics": {"int_over_amount": {"kind": "sum", "column": "Amount", "unit": "AUD"}}}},
        ],
        read_population=read_population,
    )
    h.ctx.skill.thresholds["daily_limit"] = {"value": 500}
    h.persistence.write_flagged_rows(h.state.run_id, [
        _flagged_row("claims", "r1", "RF_OVERLIMIT"),
        _flagged_row("register", "rX", "RF_OVERLIMIT"),
        _flagged_row("claims", "r3", "RF_OVERLIMIT"),
    ])
    h.persistence.write_run_metrics(h.state.run_id, [
        {"metric_name": "dom_over_amount", "value": 100.0, "unit": "AUD", "source_ref": {}, "test_id": "TDOM"},
        {"metric_name": "int_over_amount", "value": 200.0, "unit": "AUD", "source_ref": {}, "test_id": "TINT"},
    ])
    finding = _finding("FD", test_id="TDOM", metrics_cited={"dom_over_amount": _metric(100.0)}, monetary_basis="excess")

    rows_by_flag = {"RF_OVERLIMIT": h.persistence.list_flagged_rows(h.state.run_id)}
    existing_metrics = h.persistence.get_run_metrics(h.state.run_id)

    with pytest.raises(ContractViolation, match="r3"):
        exposure.compute_run_exposure(h.ctx, h.state, h.ctx.skill, [finding], existing_metrics, rows_by_flag)


# ── idempotent write (node rule 1: overwrite, never duplicate) ──────────


def test_put_test_line_values_replaces_never_duplicates(local_persistence):
    run_id = "RUN-TLV-IDEMPOTENT"
    first = [
        {"test_id": "T1", "source": "claims", "row_key": "r1", "line_key": json.dumps(["claims", "r1"]),
         "spend_amount": 100.0, "excess_amount": None},
        {"test_id": "T1", "source": "claims", "row_key": "r2", "line_key": json.dumps(["claims", "r2"]),
         "spend_amount": 200.0, "excess_amount": None},
    ]
    local_persistence.put_test_line_values(run_id, first)

    conn = local_persistence._connect()
    rows = conn.execute(
        "SELECT test_id, source, row_key, spend_amount FROM test_line_values WHERE run_id = ? "
        "ORDER BY row_key", (run_id,),
    ).fetchall()
    local_persistence._release(conn)
    assert [(r["test_id"], r["source"], r["row_key"], r["spend_amount"]) for r in rows] == [
        ("T1", "claims", "r1", 100.0), ("T1", "claims", "r2", 200.0),
    ]

    # A re-run (e.g. `prioritise` re-executed for the same run_id) with a
    # DIFFERENT row set must REPLACE the prior set, never accumulate.
    second = [
        {"test_id": "T2", "source": "claims", "row_key": "r3", "line_key": json.dumps(["claims", "r3"]),
         "spend_amount": 300.0, "excess_amount": 50.0},
    ]
    local_persistence.put_test_line_values(run_id, second)

    conn = local_persistence._connect()
    rows = conn.execute(
        "SELECT test_id, source, row_key, spend_amount, excess_amount FROM test_line_values "
        "WHERE run_id = ? ORDER BY row_key", (run_id,),
    ).fetchall()
    local_persistence._release(conn)
    assert [(r["test_id"], r["source"], r["row_key"], r["spend_amount"], r["excess_amount"]) for r in rows] == [
        ("T2", "claims", "r3", 300.0, 50.0),
    ], "the first write's rows must be gone -- put_test_line_values replaces this run's whole set"

    # A second identical write is a true no-op (same rows, not duplicated).
    local_persistence.put_test_line_values(run_id, second)
    conn = local_persistence._connect()
    count = conn.execute("SELECT COUNT(*) AS n FROM test_line_values WHERE run_id = ?", (run_id,)).fetchone()["n"]
    local_persistence._release(conn)
    assert count == 1


def test_prioritise_persists_test_line_values(local_persistence):
    # End-to-end confirmation that `prioritise` itself now writes through
    # `put_test_line_values` (rather than only the direct-call tests above).
    ctx, state = _make_ctx_and_state(local_persistence, PLANTED_DATA_DIR, run_id="RUN-TLV-E2E")
    state = discover(ctx, state)
    state = execute(ctx, state)
    state = classify(ctx, state)
    state = find(ctx, state)
    state = prioritise(ctx, state)

    conn = local_persistence._connect()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM test_line_values WHERE run_id = ?", (state.run_id,),
    ).fetchone()["n"]
    local_persistence._release(conn)
    assert count > 0

    # Re-running prioritise for the SAME run_id must not duplicate rows.
    state = prioritise(ctx, state)
    conn = local_persistence._connect()
    count_again = conn.execute(
        "SELECT COUNT(*) AS n FROM test_line_values WHERE run_id = ?", (state.run_id,),
    ).fetchone()["n"]
    local_persistence._release(conn)
    assert count_again == count
