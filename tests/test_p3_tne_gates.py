"""Tier A gates against the real SKILL-001 T&E Skill (CLAUDE.md §5), run
through the `execute` node rather than orchestrator.engine.execute_skill
directly, using the small planted-exception fixture
(tests/fixtures/tne_planted/data/, owned by the P2 primitives work -- read
only, never modified here) so this runs in seconds rather than the minutes a
full synthetic_data/ pass takes (that pass lives in
tests/test_p3_synthetic_full_run.py).

  G6  Reconciliation      -- every source's row count as the engine saw it
                             equals an independently obtained count at the
                             same pinned version; a real, injected variance
                             fails the run.
  G7  Contract conformance -- a bound source missing a declared contract
                             column fails the run with a contract message,
                             never a silent default.
  G9  Cross-process determinism -- two independent executions (two separate
                             persistence instances, two fresh NodeContexts)
                             over the same config + data produce byte-
                             identical non-narrative output: run_metrics,
                             flagged_rows, findings.
  G10 Negative control     -- a clean population (every plant removed) with
                             the SAME contract shape produces zero findings.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from orchestrator import runs as runs_module
from orchestrator.adapters.export_storage import LocalExportStorage
from orchestrator.config import DEFAULT_PPTX_TEMPLATE_PATH
from orchestrator.contract import ContractViolation, LocalFileDataSource
from orchestrator.errors import ReconciliationError
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import discover, execute, find
from orchestrator.skills import load_skill
from tests.conftest import canonical_ts

REPO_ROOT = Path(__file__).parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
DATA_DIR = REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "data"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

pytestmark = pytest.mark.skipif(
    not DATA_DIR.is_dir(), reason="tests/fixtures/tne_planted/data/ not present -- run generate.py first"
)


def _fingerprint(fp_id: str, skill_content_hash: str | None) -> dict:
    return dict(
        fingerprint_id=fp_id,
        source_table_versions="{}",
        uploaded_file_hashes="{}",
        reference_data_hashes="{}",
        skill_content_hash=skill_content_hash,
        code_revision="rev1",
        dependency_lock_hash="dep1",
        runtime_config_hash="rc1",
        endpoint_config="{}",
        prompt_template_version="none",
        created_at=canonical_ts(0),
    )


class _Settings:
    catalog = None
    schema = None
    pptx_template_path = DEFAULT_PPTX_TEMPLATE_PATH


def _make_ctx_and_state(persistence, data_dir: Path, *, run_id: str):
    skill = load_skill(SKILL_DIR)
    skill.validate()
    data_source = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}

    now = canonical_ts(1)
    fp = _fingerprint(f"FP-{run_id}", skill.content_hash)
    state = runs_module.create_run(
        persistence, run_id=run_id, run_kind="fieldwork", engagement_id="ENG-DEFAULT",
        skill_id=skill.skill_id, skill_version=skill.version, mode="playbook",
        audit_period=AUDIT_PERIOD, objective="gate test", run_owner="gatebot",
        options={"auto_confirm_plan": True}, fingerprint=fp, now=now,
    )
    data_assets = [
        {"source": name, "table_fqn": name, "version": v} for name, v in source_versions.items()
    ]
    state = persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    export_dir = Path(tempfile.mkdtemp(prefix="tne-gates-exports-"))
    ctx = NodeContext(
        settings=_Settings(), persistence=persistence, data_source=data_source,
        skill=skill, clock=lambda: canonical_ts(2),
        export_storage=LocalExportStorage(root_dir=export_dir),
    )
    return ctx, state


# ── G6 ────────────────────────────────────────────────────────────────────────


def test_g6_reconciliation_passes_with_zero_variance(local_persistence, uid):
    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-G6-OK-{uid}")
    state = discover(ctx, state)
    state = execute(ctx, state)
    assert state.reconciliation  # every one of the 8 contract sources present
    for source, rec in state.reconciliation.items():
        assert rec["variance"] == 0, f"{source}: {rec}"
        assert rec["engine_rows"] == rec["independent_rows"]

    # P2/P3 gate review item 2: G6 also reconciles Sigma(amount) and min/max
    # date, wherever the source's raw_<source> population declares one --
    # not rows alone. expense_report declares both.
    exp = state.reconciliation["expense_report"]
    assert exp["amount"] is not None
    assert exp["amount_variance"] == 0.0
    assert exp["amount"] == exp["independent_amount"]
    assert exp["min_date"] is not None and exp["max_date"] is not None
    assert exp["min_date_match"] is True
    assert exp["max_date_match"] is True
    assert exp["min_date"] == exp["independent_min_date"]
    assert exp["max_date"] == exp["independent_max_date"]

    # travel_request_segment declares neither (spec §11/§0.5: no single
    # natural amount/date column) -- reconciled on rows alone, never a
    # fabricated amount/date comparison.
    seg = state.reconciliation["travel_request_segment"]
    assert seg["amount"] is None and seg["amount_variance"] is None
    assert seg["min_date"] is None and seg["min_date_match"] is None


def test_g6_reconciliation_fails_on_injected_variance(local_persistence, uid):
    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-G6-BAD-{uid}")
    state = discover(ctx, state)

    real_row_count = ctx.data_source.row_count

    def lying_row_count(source, *, version=None):
        n = real_row_count(source, version=version)
        return n + 1 if source == "expense_report" else n

    ctx.data_source.row_count = lying_row_count
    try:
        with pytest.raises(ReconciliationError) as exc:
            execute(ctx, state)
    finally:
        ctx.data_source.row_count = real_row_count
    assert "expense_report" in str(exc.value)


def test_g6_reconciliation_fails_on_injected_amount_variance(local_persistence, uid):
    # P2/P3 gate review item 2: a Sigma(amount) mismatch fails the run exactly
    # like a row-count mismatch does -- amount is not just reported, it gates.
    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-G6-AMT-{uid}")
    state = discover(ctx, state)

    real_column_stats = ctx.data_source.column_stats

    def lying_column_stats(source, *, version=None, amount_column=None, date_column=None):
        stats = real_column_stats(
            source, version=version, amount_column=amount_column, date_column=date_column
        )
        if source == "expense_report" and stats["amount"] is not None:
            stats = {**stats, "amount": stats["amount"] + 1.0}
        return stats

    ctx.data_source.column_stats = lying_column_stats
    try:
        with pytest.raises(ReconciliationError) as exc:
            execute(ctx, state)
    finally:
        ctx.data_source.column_stats = real_column_stats
    assert "expense_report" in str(exc.value)
    assert "amount" in str(exc.value)


def test_g6_reconciliation_fails_on_injected_date_variance(local_persistence, uid):
    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-G6-DATE-{uid}")
    state = discover(ctx, state)

    real_column_stats = ctx.data_source.column_stats

    def lying_column_stats(source, *, version=None, amount_column=None, date_column=None):
        stats = real_column_stats(
            source, version=version, amount_column=amount_column, date_column=date_column
        )
        if source == "expense_report" and stats["max_date"] is not None:
            stats = {**stats, "max_date": "2099-12-31"}
        return stats

    ctx.data_source.column_stats = lying_column_stats
    try:
        with pytest.raises(ReconciliationError) as exc:
            execute(ctx, state)
    finally:
        ctx.data_source.column_stats = real_column_stats
    assert "expense_report" in str(exc.value)
    assert "max_date" in str(exc.value)


def test_g6_amount_variance_is_decimal_safe_not_float_epsilon(local_persistence, uid):
    # A sub-cent float artefact (the kind repeated pandas summation can
    # produce) must NOT register as a G6 variance; a genuine cent-level
    # mismatch always must. Exercised directly against _decimal_variance
    # rather than a full run, since reproducing a genuine float summation-
    # order difference between two independent reads is not reliable to
    # construct as a fixture.
    from orchestrator.nodes.fieldwork import _decimal_variance

    assert _decimal_variance(1960.0, 1959.9999999999998) == 0.0
    assert _decimal_variance(1960.00, 1960.01) == pytest.approx(-0.01)
    assert _decimal_variance(None, 1.0) is None
    assert _decimal_variance(1.0, None) is None


def test_g6_caveat_fails_when_a_tested_populations_own_row_accounting_does_not_add_up(local_persistence, uid):
    """Item 8 (CLAUDE.md §5 G6 caveat, P2/P3 gate review): the source-level
    check above only catches a whole SOURCE's row count drifting from an
    independent count -- it says nothing about a single TESTED population
    (e.g. p_exp) silently losing a row a filter never declared in its own
    excluded_counts. Isolated from the source-level check: `execute_skill`'s
    REAL result is used untouched except for one population's own `rows`
    figure, decremented by one with excluded_counts left as-is -- the raw
    source check (which reads a DIFFERENT population, raw_expense_report,
    never touched here) still passes; only the new per-population check
    catches this."""
    import orchestrator.nodes.fieldwork as fieldwork_module

    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-G6-POPROWS-{uid}")
    state = discover(ctx, state)

    real_execute_skill = fieldwork_module.execute_skill

    def lying_execute_skill(*args, **kwargs):
        result = real_execute_skill(*args, **kwargs)
        p_exp = result.populations["p_exp"]
        assert p_exp["rows"] > 0, "fixture must produce a non-empty p_exp population"
        corrupted = {**p_exp, "rows": p_exp["rows"] - 1}  # one row silently unaccounted
        return dataclasses.replace(result, populations={**result.populations, "p_exp": corrupted})

    fieldwork_module.execute_skill = lying_execute_skill
    try:
        with pytest.raises(ReconciliationError) as exc:
            execute(ctx, state)
    finally:
        fieldwork_module.execute_skill = real_execute_skill

    message = str(exc.value)
    assert "p_exp" in message
    assert "expense_report" in message
    # The raw source's OWN population is untouched, so nothing about
    # raw_expense_report's row accounting itself should be implicated.
    assert "raw_expense_report" not in message


# ── G7 ────────────────────────────────────────────────────────────────────────


def test_g7_contract_violation_fails_with_named_column(local_persistence, tmp_path, uid):
    corrupted_dir = tmp_path / "corrupted"
    corrupted_dir.mkdir()
    for p in DATA_DIR.iterdir():
        (corrupted_dir / p.name).write_bytes(p.read_bytes())

    df = pd.read_csv(corrupted_dir / "Complete_Per_Diem_Rates_by_Country_Region.csv")
    df = df.drop(columns=["Rate Per Day (S$)"])
    df.to_csv(corrupted_dir / "Complete_Per_Diem_Rates_by_Country_Region.csv", index=False)

    ctx, state = _make_ctx_and_state(local_persistence, corrupted_dir, run_id=f"RUN-G7-{uid}")
    state = discover(ctx, state)
    with pytest.raises(ContractViolation) as exc:
        execute(ctx, state)
    assert "Rate Per Day (S$)" in str(exc.value)


# ── G9 ────────────────────────────────────────────────────────────────────────


def test_g9_two_independent_executions_are_byte_identical(local_persistence_db_path, tmp_path, uid):
    from orchestrator.adapters.persistence_local import LocalPersistence

    results = []
    for i in (1, 2):
        db_path = str(tmp_path / f"g9_{i}.db")
        persistence = LocalPersistence(db_path)
        persistence.migrate()
        ctx, state = _make_ctx_and_state(persistence, DATA_DIR, run_id=f"RUN-G9-{uid}")
        state = discover(ctx, state)
        state = execute(ctx, state)
        state = find(ctx, state)

        metrics = persistence.get_run_metrics(state.run_id)
        flagged = sorted(
            persistence.list_flagged_rows(state.run_id),
            key=lambda r: (r["source"], r["row_key"], r["flag"]),
        )
        findings = persistence.list_findings(state.run_id)
        # Narrative fields are excluded by construction -- P3 never populates
        # them (no LLM calls, CLAUDE.md §3 NN2), so nothing to strip here, but
        # observation/recommendation ARE deterministic template text over
        # metrics_cited and legitimately part of the non-narrative surface.
        #
        # Frame snapshots (orchestrator/frames.py, CLAUDE.md build brief P4
        # perf fix) are non-narrative run output too: the `execute` node just
        # wrote one Parquet file per contract source into this iteration's own
        # export_storage. Read every one of THOSE bytes back (never re-derive
        # them) so a divergence in the snapshot-writing path itself -- not
        # just in the numbers that feed it -- would fail this gate.
        frame_exports = state.exports["frames"]
        frame_bytes = {
            source: ctx.export_storage.read(meta["path"]) for source, meta in sorted(frame_exports.items())
        }
        results.append(
            {
                "metrics": json.dumps(metrics, sort_keys=True),
                "flagged": json.dumps(flagged, sort_keys=True),
                "findings": json.dumps(
                    [{k: v for k, v in f.items() if k not in ("created_at", "updated_at")} for f in findings],
                    sort_keys=True,
                ),
                "frame_sources": sorted(frame_exports),
                "frame_sha256": {source: meta["sha256"] for source, meta in sorted(frame_exports.items())},
                "frame_bytes": frame_bytes,
            }
        )

    assert results[0]["metrics"] == results[1]["metrics"]
    assert results[0]["flagged"] == results[1]["flagged"]
    assert results[0]["findings"] == results[1]["findings"]
    assert results[0]["frame_sources"] == results[1]["frame_sources"]
    assert len(results[0]["frame_sources"]) == 8  # one snapshot per SKILL-001 contract source
    assert results[0]["frame_sha256"] == results[1]["frame_sha256"]
    assert results[0]["frame_bytes"] == results[1]["frame_bytes"]


def test_g9_two_subprocesses_with_different_hash_seeds_are_byte_identical(tmp_path, uid):
    # CLAUDE.md P2/P3 gate review item 3: the in-process gate above shares one
    # interpreter's PYTHONHASHSEED across both iterations, so it cannot catch
    # set/dict iteration order or float-summation-order nondeterminism -- only
    # genuinely separate PROCESSES with DIFFERENT hash seeds can. Runs
    # tests/g9_subprocess_worker.py (never imported -- a real subprocess) twice
    # end to end through discover/execute/find/prioritise/act/export, and
    # compares metrics, flagged_rows, findings (incl. rendered text/severity/
    # exposure), management actions, reconciliation and the XLSX workpaper's
    # own cell values.
    import os
    import subprocess

    worker = REPO_ROOT / "tests" / "g9_subprocess_worker.py"
    results = []
    for i, hash_seed in enumerate((["1"], ["2"]), start=1):
        db_path = tmp_path / f"g9-sub-{i}.db"
        out_path = tmp_path / f"g9-sub-{i}.json"
        env = {**os.environ, "PYTHONHASHSEED": hash_seed[0]}
        proc = subprocess.run(
            [sys.executable, str(worker), str(db_path), f"RUN-G9SUB-{uid}", str(out_path)],
            env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, f"worker {i} (PYTHONHASHSEED={hash_seed[0]}) failed:\n{proc.stdout}\n{proc.stderr}"
        results.append(json.loads(out_path.read_text()))

    assert results[0]["metrics"] == results[1]["metrics"]
    assert results[0]["flagged_rows"] == results[1]["flagged_rows"]
    assert results[0]["findings"] == results[1]["findings"]
    assert results[0]["management_actions"] == results[1]["management_actions"]
    assert results[0]["reconciliation"] == results[1]["reconciliation"]
    assert results[0]["xlsx_cells"] == results[1]["xlsx_cells"]
    # A monetary finding really did fire on this fixture, and every sheet was
    # actually compared -- an early-exit-to-empty on both sides would pass
    # every assertion above without proving anything.
    assert any(f.get("exposure_amount") for f in results[0]["findings"])
    assert set(results[0]["xlsx_cells"]) >= {"Findings", "Reconciliation", "Metrics"}


# ── G10 ───────────────────────────────────────────────────────────────────────


# G10 is deliberately run against the "mini" Skill fixture rather than
# tne_exco: plants.yaml's natural_id -> row mapping is internal to
# generate.py (owned by the P2 primitives work) and re-deriving "which rows
# are the plants" here to strip them would risk re-implementing that
# generator's own logic rather than testing independently of it (exactly
# what CLAUDE.md §9 warns against: "never use a generator as its own test
# oracle"). The mini Skill's clean-by-construction dataset (every claim
# under the high-value limit, every claim matched in the register) is a
# clean population this file fully controls and can assert on plainly.


def test_g10_negative_control_zero_findings(local_persistence, tmp_path):
    from tests.test_p3_nodes import MINI_SKILL_DIR, _fingerprint as _mini_fingerprint

    data_dir = tmp_path / "clean"
    data_dir.mkdir()
    # T2's primitive config (mini/plan.yaml) is mode: semi -- it flags LEFT
    # rows that DO have a matching right-side row (a SQL semi-join), so a
    # clean population for T2's "missing_count > 0" trigger to stay false is
    # one where NO claim matches any register row on keys, not the reverse.
    claims = pd.DataFrame(
        [
            {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Vendor": "VendorA"},
            {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 200, "Vendor": "VendorB"},
        ]
    )
    register = pd.DataFrame(
        [
            {"Employee ID": 9, "Transaction Date": "2026-01-09", "Vendor": "VendorZ"},
        ]
    )
    claims.to_csv(data_dir / "claims.csv", index=False)
    register.to_csv(data_dir / "register.csv", index=False)

    skill = load_skill(MINI_SKILL_DIR)
    skill.validate()
    data_source = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}

    now = canonical_ts(1)
    fp = _mini_fingerprint("FP-G10-CLEAN", skill.content_hash)
    state = runs_module.create_run(
        local_persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=skill.skill_id,
        skill_version=skill.version, mode="playbook", audit_period=("2026-01-01", "2026-02-28"),
        objective="G10", run_owner="gatebot", options={"auto_confirm_plan": True}, fingerprint=fp, now=now,
    )
    data_assets = [{"source": n, "table_fqn": n, "version": v} for n, v in source_versions.items()]
    state = local_persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    export_dir = Path(tempfile.mkdtemp(prefix="tne-gates-g10-exports-"))
    ctx = NodeContext(
        settings=_Settings(), persistence=local_persistence, data_source=data_source,
        skill=skill, clock=lambda: canonical_ts(2),
        export_storage=LocalExportStorage(root_dir=export_dir),
    )
    state = discover(ctx, state)
    state = execute(ctx, state)
    assert local_persistence.get_run_metrics(state.run_id)["hv_count"]["value"] == 0
    assert local_persistence.get_run_metrics(state.run_id)["missing_count"]["value"] == 0

    state = find(ctx, state)
    findings = local_persistence.list_findings(state.run_id)
    assert findings == []
    assert state.findings == []


# ── G13 ───────────────────────────────────────────────────────────────────────


def test_g13_every_xlsx_number_equals_the_persisted_value(local_persistence, uid):
    """G13 (CLAUDE.md §5): every number in the exported XLSX workpaper equals
    the run's own persisted value, with the same rounding -- the exporter
    reads state/persistence, never recomputes (orchestrator.nodes.fieldwork.
    export's own docstring). Runs the real SKILL-001 Skill end to end
    (execute -> classify -> find -> prioritise -> act -> export) against the
    fast planted fixture and diffs every numeric cell in the Findings,
    Metrics and Test Results sheets against orchestrator.persistence's own
    rows for that run -- not a re-derivation of what the exporter should
    have written, an independent read of the same source it read."""
    import hashlib
    import io

    import openpyxl

    from orchestrator.nodes.fieldwork import act, classify, export, prioritise

    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-G13-{uid}")
    state = discover(ctx, state)
    state = execute(ctx, state)
    state = classify(ctx, state)
    state = find(ctx, state)
    state = prioritise(ctx, state)
    state = act(ctx, state)
    state = export(ctx, state)

    findings_by_id = {f["finding_id"]: f for f in local_persistence.list_findings(state.run_id)}
    assert findings_by_id, "no findings fired on this fixture -- test is not exercising anything"
    metrics = local_persistence.get_run_metrics(state.run_id)
    test_results_by_id = {t["test_id"]: t for t in state.test_results}

    xlsx_meta = state.exports["xlsx"]
    content = ctx.export_storage.read(xlsx_meta["path"])
    assert hashlib.sha256(content).hexdigest() == xlsx_meta["sha256"]
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)

    findings_sheet = wb["Findings"]
    header = [c.value for c in findings_sheet[1]]
    exposure_col = header.index("exposure_amount") + 1
    id_col = header.index("finding_id") + 1
    checked_findings = 0
    for row in findings_sheet.iter_rows(min_row=2):
        finding_id = row[id_col - 1].value
        if finding_id not in findings_by_id:
            continue  # the footer row, or past the data rows
        expected = findings_by_id[finding_id]["exposure_amount"]
        actual = row[exposure_col - 1].value
        if expected is None:
            assert actual is None, f"{finding_id}: expected a blank cell (non-monetary), got {actual!r}"
        else:
            assert actual == pytest.approx(round(expected, 2)), f"{finding_id}: {actual!r} != {expected!r}"
        checked_findings += 1
    assert checked_findings == len(findings_by_id)

    metrics_sheet = wb["Metrics"]
    checked_metrics = 0
    for row in metrics_sheet.iter_rows(min_row=2):
        name = row[0].value
        if name not in metrics:
            continue  # the footer row
        value = metrics[name]["value"]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            assert row[1].value == pytest.approx(value), f"{name}: {row[1].value!r} != {value!r}"
        checked_metrics += 1
    assert checked_metrics == len(metrics)

    test_results_sheet = wb["Test Results"]
    checked_tests = 0
    for row in test_results_sheet.iter_rows(min_row=2):
        test_id = row[0].value
        if test_id not in test_results_by_id:
            continue  # the footer row
        assert row[2].value == test_results_by_id[test_id]["exception_units"]
        checked_tests += 1
    assert checked_tests == len(test_results_by_id)

    # Independent review 2026-09-24 item 7: G6/G13 covered the Findings/
    # Metrics/Test Results sheets above, but not Reconciliation or Flagged
    # Row Counts -- extended here, against this same run's own state.
    reconciliation_sheet = wb["Reconciliation"]
    recon_header = [c.value for c in reconciliation_sheet[1]]
    recon_col = {name: i for i, name in enumerate(recon_header)}
    checked_sources = 0
    for row in reconciliation_sheet.iter_rows(min_row=2):
        source = row[recon_col["source"]].value
        if source not in state.reconciliation:
            continue  # the footer row
        rec = state.reconciliation[source]
        for col_name, key in (
            ("engine_rows", "engine_rows"), ("independent_rows", "independent_rows"),
            ("row_variance", "variance"),
        ):
            expected = rec.get(key)
            actual = row[recon_col[col_name]].value
            # NN14 / the "—" decision (§11): a value this run never
            # measured writes as "—", never a fabricated 0 -- see also
            # test_xlsx_reconciliation_writes_dash_not_zero_for_unmeasured_values
            # below, which exercises the None case directly (this
            # fixture's own 8 sources are all bound, so None never occurs
            # naturally here).
            if expected is None:
                assert actual == "—", f"{source}.{col_name}: expected '—', got {actual!r}"
            else:
                assert actual == expected, f"{source}.{col_name}: {actual!r} != {expected!r}"
        if rec.get("amount") is None:
            assert row[recon_col["amount"]].value == "n/a — no amount column declared"
        else:
            assert row[recon_col["amount"]].value == pytest.approx(round(rec["amount"], 2))
            for col_name, key in (
                ("independent_amount", "independent_amount"), ("amount_variance", "amount_variance"),
            ):
                expected = rec.get(key)
                actual = row[recon_col[col_name]].value
                if expected is None:
                    assert actual == "—", f"{source}.{col_name}: expected '—', got {actual!r}"
                else:
                    assert actual == pytest.approx(round(expected, 2)), f"{source}.{col_name}: {actual!r} != {expected!r}"
        checked_sources += 1
    assert checked_sources == len(state.reconciliation)

    flagged_rows = ctx.persistence.list_flagged_rows(state.run_id)
    expected_flag_counts: dict[str, int] = {}
    for row in flagged_rows:
        expected_flag_counts[row["flag"]] = expected_flag_counts.get(row["flag"], 0) + 1
    frc_sheet = wb["Flagged Row Counts"]
    checked_flags = 0
    for row in frc_sheet.iter_rows(min_row=2):
        flag = row[0].value
        if flag not in expected_flag_counts:
            continue  # the footer row
        assert row[1].value == expected_flag_counts[flag], f"{flag}: {row[1].value!r} != {expected_flag_counts[flag]!r}"
        checked_flags += 1
    assert checked_flags == len(expected_flag_counts)
    assert checked_flags > 0, "no flagged rows on this fixture -- test is not exercising anything"

    # Every sheet's own footer (§9A.2): run_id + generation timestamp, so an
    # exported workpaper is traceable to the run that produced it.
    expected_footer_prefix = f"run_id={state.run_id} | generated_at="
    for sheet_name in wb.sheetnames:
        sheet = wb[sheet_name]
        footer_cell = sheet.cell(row=sheet.max_row, column=1).value
        assert isinstance(footer_cell, str) and footer_cell.startswith(expected_footer_prefix), (
            f"{sheet_name}: missing/incorrect footer row, got {footer_cell!r}"
        )


def test_xlsx_reconciliation_writes_dash_not_zero_for_unmeasured_values(local_persistence, uid):
    """Independent review 2026-09-24 item 4 (CLAUDE.md NN14 / the "—"
    decision, §11): a Reconciliation-sheet value this run never MEASURED --
    a source's raw_<source> population was never bound, so `engine_rows`/
    `variance` are None; an amount column IS declared but the independent
    side individually came back empty, so `independent_amount`/
    `amount_variance` are None -- must write as "—", never a fabricated
    0/0.0. Writing 0 there reads as "reconciled with zero variance", a
    false claim of a clean reconciliation, not an absence. A genuinely
    computed 0 still writes as the number 0. Calls
    orchestrator.nodes.fieldwork._write_xlsx_workpaper directly with a
    hand-built reconciliation dict (this fixture's real 8 sources are all
    bound, so the None case never occurs naturally in the G13 test above)."""
    import io

    import openpyxl

    from orchestrator.nodes.fieldwork import _write_xlsx_workpaper

    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-RECON-DASH-{uid}")
    reconciliation = {
        "unmeasured_source": {
            "engine_rows": None, "independent_rows": 40, "variance": None,
            "amount": None, "independent_amount": None, "amount_variance": None,
            "min_date": None, "independent_min_date": None, "min_date_match": None,
            "max_date": None, "independent_max_date": None, "max_date_match": None,
        },
        "amount_declared_but_independent_missing": {
            "engine_rows": 12, "independent_rows": 12, "variance": 0,
            "amount": 500.0, "independent_amount": None, "amount_variance": None,
            "min_date": "2025-01-01", "independent_min_date": "2025-01-01", "min_date_match": True,
            "max_date": "2025-01-31", "independent_max_date": "2025-01-31", "max_date_match": True,
        },
        "clean_zero_variance": {
            "engine_rows": 0, "independent_rows": 0, "variance": 0,
            "amount": 0.0, "independent_amount": 0.0, "amount_variance": 0.0,
            "min_date": None, "independent_min_date": None, "min_date_match": None,
            "max_date": None, "independent_max_date": None, "max_date_match": None,
        },
    }
    state = dataclasses.replace(state, reconciliation=reconciliation)
    content = _write_xlsx_workpaper(state, findings=[], metrics={}, flagged_rows=[], now=canonical_ts(9))

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    sheet = wb["Reconciliation"]
    header = [c.value for c in sheet[1]]
    col = {name: i for i, name in enumerate(header)}
    rows_by_source = {}
    for row in sheet.iter_rows(min_row=2):
        source = row[col["source"]].value
        if source in reconciliation:
            rows_by_source[source] = row

    unmeasured = rows_by_source["unmeasured_source"]
    assert unmeasured[col["engine_rows"]].value == "—"
    assert unmeasured[col["row_variance"]].value == "—"
    assert unmeasured[col["independent_rows"]].value == 40
    assert unmeasured[col["amount"]].value == "n/a — no amount column declared"

    partial = rows_by_source["amount_declared_but_independent_missing"]
    assert partial[col["amount"]].value == pytest.approx(500.0)
    assert partial[col["independent_amount"]].value == "—"
    assert partial[col["amount_variance"]].value == "—"

    clean = rows_by_source["clean_zero_variance"]
    assert clean[col["engine_rows"]].value == 0
    assert clean[col["row_variance"]].value == 0
    assert clean[col["amount"]].value == pytest.approx(0.0)
    assert clean[col["independent_amount"]].value == pytest.approx(0.0)
    assert clean[col["amount_variance"]].value == pytest.approx(0.0)
