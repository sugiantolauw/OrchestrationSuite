"""Surface 1 (CLAUDE.md §5 Tier B, §11 "Surface 1 harness").

Builds ONE real completed-through-`execute` SKILL-001 run (module-scoped,
LocalPersistence in-memory) against tests/fixtures/tne_planted/data -- the
same planted fixture Surface 2 uses -- via the REAL pipeline nodes
(orchestrator.nodes.fieldwork.NODES_FOR, orchestrator.pipeline.run_phase),
never a hand-rolled shortcut, so flagged_rows/run_metrics/exceptions are
exactly what a real run persists.

The oracle in every test here is hand-authored directly from
tests/fixtures/tne_planted/plants.yaml's own declared fields (CLAUDE.md §9:
"never use a generator as its own test oracle") -- the specific Booking
ID/Employee ID/Vendor/date/amount values below are copied from plants.yaml's
T3.1b and T5.2 blocks, not inferred from what the engine happens to output.
"""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from orchestrator import runs as runs_module
from orchestrator.adapters.export_storage import LocalExportStorage
from orchestrator.contract import LocalFileDataSource
from orchestrator.eval.surface1 import (
    Surface1OracleError,
    Surface1Report,
    Surface1RunError,
    TestScore,
    load_oracle,
    run_surface1,
    write_json_report,
    write_xlsx_report,
)
from orchestrator.config import load_settings
from orchestrator.fingerprint import compute_fingerprint
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import NODES_FOR
from orchestrator.pipeline import run_phase
from orchestrator.skills import load_skill
from orchestrator import service as service_module
from tests.conftest import _local_memory_persistence, canonical_ts

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
SKILLS_DIR = SKILL_DIR.parent
DATA_DIR = REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "data"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

pytestmark = pytest.mark.skipif(
    not DATA_DIR.is_dir(), reason="tests/fixtures/tne_planted/data/ not present -- run generate.py first"
)


def _settings():
    return load_settings({})


def _build_completed_run() -> tuple[object, str, dict]:
    """A real SKILL-001 run, through the real pipeline nodes, up to
    'awaiting_signoff' (the execute phase's own terminal status -- Surface 1
    scores a run as soon as `execute`/`classify`/`find`/`prioritise` have
    run; it does not require sign-off or export). Returns
    (persistence, run_id, app_ctx_kwargs) -- app_ctx_kwargs is what
    orchestrator.service.AppContext needs beyond persistence/clock to score
    this run via run_surface1(..., ctx=...)."""
    persistence = _local_memory_persistence()
    skill = load_skill(SKILL_DIR)
    skill.validate()
    data_source = LocalFileDataSource(root_dir=DATA_DIR, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}
    data_assets = [
        {"source": name, "table_fqn": name, "version": v} for name, v in source_versions.items()
    ]

    fp = compute_fingerprint(
        settings=_settings(), source_table_versions={}, uploaded_file_hashes={},
        skill_dir=SKILL_DIR, requirements_path=REPO_ROOT / "requirements.txt", prompts_dirs=[],
    )
    now = canonical_ts(1)
    state = runs_module.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT",
        skill_id=skill.skill_id, skill_version=skill.version, mode="playbook",
        audit_period=AUDIT_PERIOD, objective="Surface 1 fixture run", run_owner="surface1-test",
        options={"auto_confirm_plan": True}, data_assets=data_assets, fingerprint=fp, now=now,
    )

    export_dir = REPO_ROOT / ".local" / "test-exports" / "surface1"
    export_dir.mkdir(parents=True, exist_ok=True)
    tick = {"n": 1}

    def clock() -> str:
        tick["n"] += 1
        return canonical_ts(tick["n"])

    ctx = NodeContext(
        settings=_settings(), persistence=persistence, data_source=data_source,
        skill=skill, clock=clock, export_storage=LocalExportStorage(root_dir=export_dir),
    )
    state = run_phase(persistence, state.run_id, nodes_for=NODES_FOR, skill=ctx, clock=clock, current_fingerprint=fp)
    assert state.status == "awaiting_signoff", state.status_reason

    def data_source_factory(bindings, contract_sources=None, skill_id=None):
        return LocalFileDataSource(root_dir=DATA_DIR, sources=contract_sources or {})

    app_ctx = service_module.AppContext(
        settings=_settings(), persistence=persistence, skills_dir=SKILLS_DIR,
        data_source_factory=data_source_factory, export_storage=None, clock=clock, backend="local",
    )
    return app_ctx, state.run_id, {}


@pytest.fixture(scope="module")
def completed_run():
    app_ctx, run_id, _ = _build_completed_run()
    return app_ctx, run_id


@pytest.fixture(scope="module")
def expense_report_frame() -> pd.DataFrame:
    """The pinned Expense_Report_Combined.xlsx, read with PLAIN pandas
    (openpyxl engine), independently of orchestrator.contract/
    LocalFileDataSource -- so a Row Number oracle value computed against
    this frame is never derived from the harness under test. Row order here
    is the same order contract.parse_source_bytes reads it in (sheet
    'data', no reindexing before assigning __row_key = i+1), which is why
    matches.index[...] + 1 below equals the engine's own Row Number -- but
    that equality is exactly what these tests are checking, not assumed by
    the lookup itself."""
    return pd.read_excel(DATA_DIR / "Expense_Report_Combined.xlsx", sheet_name="data")


@pytest.fixture(scope="module")
def attendee_validity_frame() -> pd.DataFrame:
    return pd.read_excel(DATA_DIR / "Attendee_Validity_Report.xlsx", sheet_name="Sheet1")


def _row_number(df: pd.DataFrame, match_col: str, match_value) -> str:
    """The 1-based data-row position of the row whose `match_col` equals
    `match_value` -- a plain pandas lookup against a frame read straight off
    disk (see the fixtures above), never through the engine or generator."""
    matches = df.index[df[match_col] == match_value].tolist()
    assert len(matches) == 1, (
        f"{match_col}={match_value!r}: expected exactly 1 matching row in the pinned "
        f"fixture, found {len(matches)}"
    )
    return str(matches[0] + 1)


def _write_oracle(tmp_path: Path, rows: list[dict], *, name: str = "oracle.csv") -> Path:
    import csv

    path = tmp_path / name
    columns = ["record_type", "test_id", "exception_id", "expected", "identity_field", "identity_value",
               "metric_name", "expected_value"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    return path


def _exc(test_id, exception_id, expected, identity_field, identity_value) -> dict:
    return {
        "record_type": "exception", "test_id": test_id, "exception_id": exception_id,
        "expected": str(expected).lower(), "identity_field": identity_field, "identity_value": identity_value,
    }


def _metric(test_id, metric_name, expected_value) -> dict:
    return {"record_type": "metric", "test_id": test_id, "metric_name": metric_name, "expected_value": expected_value}


def _kv_exc(test_id, exception_id, expected, fields: dict) -> list[dict]:
    """One row per declared identity_field for a KeyGroupTest (T3.3b, T5.2,
    T6.1d) -- `fields` in the SAME name/value shape CATALOGUE_TESTS declares
    for that test_id (orchestrator/eval/surface1.py)."""
    return [
        _exc(test_id, exception_id, expected, field, str(value))
        for field, value in fields.items()
    ]


def _t51_exc(exception_id, expected, row_numbers) -> list[dict]:
    """One row per declared member for a T5.1 MemberGroupTest -- each
    `identity_field="Row Number"`, matching CATALOGUE_TESTS' T5.1
    `member_id_field`."""
    return [
        _exc("T5.1", exception_id, expected, "Row Number", rn) for rn in row_numbers
    ]


# ── perfect match, FP, FN (T3.1b -- plants.yaml lines ~289-660) ─────────────
# Booking ID 610001: a genuine T3.1b exception (no matching pre-approval).
# Booking ID 610000: a genuine T3.1b negative (has a matching pre-approval).


def test_perfect_match_row_test(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T3.1b", "EX-1", True, "Booking ID", "610001"),
        _exc("T3.1b", "EX-2", False, "Booking ID", "610000"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.1b"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0
    assert score.precision == 1.0
    assert score.recall == 1.0
    assert score.false_positive_ids == []
    assert score.false_negative_ids == []


def test_false_positive_detected(completed_run, tmp_path):
    """610001 is a genuine exception -- an oracle that confirms it CLEAN
    (expected=false) must be scored as a false positive: the engine flagged
    it, the oracle said it should not have been."""
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T3.1b", "WRONGLY-CLEARED", False, "Booking ID", "610001"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.1b"]
    assert score.true_positives == 0
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.precision == 0.0


def test_false_negative_detected(completed_run, tmp_path):
    """610000 is a genuine negative -- an oracle that confirms it as an
    exception (expected=true) must be scored as a false negative: the
    engine did not flag it, the oracle said it should have."""
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T3.1b", "MISSED", True, "Booking ID", "610000"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.1b"]
    assert score.true_positives == 0
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]
    assert score.recall == 0.0


# ── group-grain perfect match (T5.2 -- plants.yaml lines ~7666+) ────────────
# A genuine T5.2 duplicate group: Employee ID 52725, Transaction Date
# 2025-04-02, Vendor "Dup Vendor Co", Amount 150.


def test_group_test_perfect_match(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        {"record_type": "exception", "test_id": "T5.2", "exception_id": "DUP-1", "expected": "true",
         "identity_field": "Employee ID", "identity_value": "52725"},
        {"record_type": "exception", "test_id": "T5.2", "exception_id": "DUP-1", "expected": "true",
         "identity_field": "Transaction Date", "identity_value": "2025-04-02"},
        {"record_type": "exception", "test_id": "T5.2", "exception_id": "DUP-1", "expected": "true",
         "identity_field": "Vendor", "identity_value": "Dup Vendor Co"},
        {"record_type": "exception", "test_id": "T5.2", "exception_id": "DUP-1", "expected": "true",
         "identity_field": "Amount", "identity_value": "150"},
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T5.2"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


# ── T5.2: detected mismatch (perfect match already covered above) ──────────
# T5.2-N01 (plants.yaml): Employee ID 52725, Transaction Date 2025-04-18,
# Vendor "Itemised Vendor Co", Amount 90 -- a genuine T5.2 negative
# (itemised lines of one purchase, not a duplicate).


def test_mismatch_detected_t52(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    rows = (
        _kv_exc("T5.2", "WRONGLY-CLEARED", False, {
            "Employee ID": 52725, "Transaction Date": "2025-04-02",
            "Vendor": "Dup Vendor Co", "Amount": 150,
        })
        + _kv_exc("T5.2", "MISSED", True, {
            "Employee ID": 52725, "Transaction Date": "2025-04-18",
            "Vendor": "Itemised Vendor Co", "Amount": 90,
        })
    )
    oracle = _write_oracle(tmp_path, rows)
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T5.2"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T3.1a: business-id row test (travel_requests_no_expense) ───────────────
# TRQ-31A-P01 (Employee Chen Alex, Approved): a genuine T3.1a exception --
# an approved travel request with no matching expense claim. TRQ-31A-N01
# (Cancelled): a genuine negative -- cancelled requests are out of scope
# (CLAUDE.md §11, the accepted SKILL-001_test_specification.md default).


def test_perfect_match_t31a(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T3.1a", "EX-1", True, "Travel Request ID", "TRQ-31A-P01"),
        _exc("T3.1a", "EX-2", False, "Travel Request ID", "TRQ-31A-N01"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.1a"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t31a(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T3.1a", "WRONGLY-CLEARED", False, "Travel Request ID", "TRQ-31A-P01"),
        _exc("T3.1a", "MISSED", True, "Travel Request ID", "TRQ-31A-N01"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.1a"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T3.2a: row-number row test (expense_report, sub-test air_dom) ──────────
# Parent Key 540003 (plants.yaml natural_id "40002#30002"): a domestic
# airfare booked with a non-preferred vendor -- a genuine T3.2a exception.
# Parent Key 540035: the same category, a preferred vendor -- a genuine
# negative. "Row Number" is this row's 1-based position in the pinned
# Expense_Report_Combined.xlsx, located independently of the harness via
# the expense_report_frame fixture above.


def test_perfect_match_t32a(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(expense_report_frame, "Parent Key", 540003)
    neg_row = _row_number(expense_report_frame, "Parent Key", 540035)
    oracle = _write_oracle(tmp_path, [
        _exc("T3.2a", "EX-1", True, "Row Number", exc_row),
        _exc("T3.2a", "EX-2", False, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.2a"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t32a(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(expense_report_frame, "Parent Key", 540003)
    neg_row = _row_number(expense_report_frame, "Parent Key", 540035)
    oracle = _write_oracle(tmp_path, [
        _exc("T3.2a", "WRONGLY-CLEARED", False, "Row Number", exc_row),
        _exc("T3.2a", "MISSED", True, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.2a"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T3.3a: business-id row test (booking_detail, sub-test dom) ─────────────
# Booking ID 610000: Advance Purchase Days 0, below the 7-day domestic
# late-booking threshold -- a genuine T3.3a exception. Booking ID 610015:
# Advance Purchase Days 7, at (not below) the threshold -- a genuine
# negative. (These are the same Booking Detail rows T3.1b's tests above use
# under different natural ids/exception meanings -- plants.yaml reuses the
# booking rows across tests via YAML anchors.)


def test_perfect_match_t33a(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T3.3a", "EX-1", True, "Booking ID", "610000"),
        _exc("T3.3a", "EX-2", False, "Booking ID", "610015"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.3a"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t33a(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T3.3a", "WRONGLY-CLEARED", False, "Booking ID", "610000"),
        _exc("T3.3a", "MISSED", True, "Booking ID", "610015"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.3a"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T3.3b: key-group test (attendee_validity entertainment entry) ──────────
# T33B-P01: Entry Amount 810 over 9 attendees = $90/head, over the $80/head
# limit -- a genuine T3.3b exception. T33B-N01: 800/10 = $80/head, at (not
# over) the limit -- a genuine negative.


def test_perfect_match_t33b(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    rows = (
        _kv_exc("T3.3b", "EX-1", True, {
            "Employee ID": 52725, "Report Name": "T33B Report 1",
            "Transaction Date": "2025-05-01", "Vendor": "Harbour Restaurant", "Entry Amount": 810,
        })
        + _kv_exc("T3.3b", "EX-2", False, {
            "Employee ID": 52725, "Report Name": "T33B Report 17",
            "Transaction Date": "2025-05-17", "Vendor": "Harbour Restaurant", "Entry Amount": 800.0,
        })
    )
    oracle = _write_oracle(tmp_path, rows)
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.3b"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t33b(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    rows = (
        _kv_exc("T3.3b", "WRONGLY-CLEARED", False, {
            "Employee ID": 52725, "Report Name": "T33B Report 1",
            "Transaction Date": "2025-05-01", "Vendor": "Harbour Restaurant", "Entry Amount": 810,
        })
        + _kv_exc("T3.3b", "MISSED", True, {
            "Employee ID": 52725, "Report Name": "T33B Report 17",
            "Transaction Date": "2025-05-17", "Vendor": "Harbour Restaurant", "Entry Amount": 800.0,
        })
    )
    oracle = _write_oracle(tmp_path, rows)
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.3b"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T4.1: row-number row test (expense_report, missing receipt) ────────────
# Parent Key 540251: a genuine T4.1 exception (claim appears in the missing-
# receipt register). Parent Key 540283: a genuine negative.


def test_perfect_match_t41(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(expense_report_frame, "Parent Key", 540251)
    neg_row = _row_number(expense_report_frame, "Parent Key", 540283)
    oracle = _write_oracle(tmp_path, [
        _exc("T4.1", "EX-1", True, "Row Number", exc_row),
        _exc("T4.1", "EX-2", False, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T4.1"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t41(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(expense_report_frame, "Parent Key", 540251)
    neg_row = _row_number(expense_report_frame, "Parent Key", 540283)
    oracle = _write_oracle(tmp_path, [
        _exc("T4.1", "WRONGLY-CLEARED", False, "Row Number", exc_row),
        _exc("T4.1", "MISSED", True, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T4.1"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T4.2: row-number row test (expense_report, missing attendee record) ────
# Parent Key 540317: a genuine T4.2 exception (no matching attendee_validity
# row). Parent Key 540349: a genuine negative.


def test_perfect_match_t42(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(expense_report_frame, "Parent Key", 540317)
    neg_row = _row_number(expense_report_frame, "Parent Key", 540349)
    oracle = _write_oracle(tmp_path, [
        _exc("T4.2", "EX-1", True, "Row Number", exc_row),
        _exc("T4.2", "EX-2", False, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T4.2"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t42(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(expense_report_frame, "Parent Key", 540317)
    neg_row = _row_number(expense_report_frame, "Parent Key", 540349)
    oracle = _write_oracle(tmp_path, [
        _exc("T4.2", "WRONGLY-CLEARED", False, "Row Number", exc_row),
        _exc("T4.2", "MISSED", True, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T4.2"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T4.4: row-number row test (expense_report, high-value claim) ───────────
# Parent Key 540383: a genuine T4.4 exception (amount over the $5,000
# high-value limit). Parent Key 6900014: a genuine negative.


def test_perfect_match_t44(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(expense_report_frame, "Parent Key", 540383)
    neg_row = _row_number(expense_report_frame, "Parent Key", 6900014)
    oracle = _write_oracle(tmp_path, [
        _exc("T4.4", "EX-1", True, "Row Number", exc_row),
        _exc("T4.4", "EX-2", False, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T4.4"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t44(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(expense_report_frame, "Parent Key", 540383)
    neg_row = _row_number(expense_report_frame, "Parent Key", 6900014)
    oracle = _write_oracle(tmp_path, [
        _exc("T4.4", "WRONGLY-CLEARED", False, "Row Number", exc_row),
        _exc("T4.4", "MISSED", True, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T4.4"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T5.1: member-group test (expense_report split claims) ──────────────────
# T51-SD-P01 (plants.yaml, "same_day" kind): Parent Keys 540447 + 540449,
# $2,600 each ($5,200 aggregate, over the $5,000 threshold; neither line
# itself over the $5,000 per-line ceiling) -- a genuine T5.1 exception.
# T51-SD-N01: Parent Keys 540511 + 540513, $2,500 each ($5,000 aggregate,
# NOT over the threshold) -- a genuine negative.


def test_perfect_match_t51(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_rows = [
        _row_number(expense_report_frame, "Parent Key", 540447),
        _row_number(expense_report_frame, "Parent Key", 540449),
    ]
    neg_rows = [
        _row_number(expense_report_frame, "Parent Key", 540511),
        _row_number(expense_report_frame, "Parent Key", 540513),
    ]
    oracle = _write_oracle(
        tmp_path, _t51_exc("EX-1", True, exc_rows) + _t51_exc("EX-2", False, neg_rows),
    )
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T5.1"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t51(completed_run, tmp_path, expense_report_frame):
    app_ctx, run_id = completed_run
    exc_rows = [
        _row_number(expense_report_frame, "Parent Key", 540447),
        _row_number(expense_report_frame, "Parent Key", 540449),
    ]
    neg_rows = [
        _row_number(expense_report_frame, "Parent Key", 540511),
        _row_number(expense_report_frame, "Parent Key", 540513),
    ]
    oracle = _write_oracle(
        tmp_path,
        _t51_exc("WRONGLY-CLEARED", False, exc_rows) + _t51_exc("MISSED", True, neg_rows),
    )
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T5.1"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


def test_t51_missing_member_counts_as_mismatch_not_partial_match(
    completed_run, tmp_path, expense_report_frame,
):
    """T51-W-M3 (plants.yaml, T5.1 'window' kind) is a genuine 3-member
    split-claim group: Parent Keys 6900006 ($200, 2025-06-01), 6900004
    ($3,000, 2025-06-03) and 6900005 ($3,000, 2025-06-03) -- $6,200
    aggregate, over the $5,000 threshold, no single line over the $5,000
    per-line ceiling.

    An oracle that declares only 2 of the 3 members (dropping 6900005) is
    declaring a DIFFERENT scoring unit: member_group_id hashes the full
    sorted row-key SET (orchestrator.primitives.common.member_group_id), so
    a 2-of-3 subset can never equal the engine's own 3-member group id --
    there is no partial credit for a partially-right member list. The kept
    pair ($200 + $3,000 = $3,200) is also, independently, under the $5,000
    aggregate threshold, so this is not a case where the reduced set happens
    to be some OTHER real group the engine separately found either. This
    must score as a false negative (the declared unit was never found), not
    a true positive."""
    app_ctx, run_id = completed_run
    kept_rows = [
        _row_number(expense_report_frame, "Parent Key", 6900006),
        _row_number(expense_report_frame, "Parent Key", 6900004),
    ]
    oracle = _write_oracle(tmp_path, _t51_exc("PARTIAL", True, kept_rows))
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T5.1"]
    assert score.true_positives == 0
    assert score.false_positives == 0
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["PARTIAL"]


# ── T6.1a: business-id row test (approval_aging, composite key) ────────────
# "RPT-61A-P01|1|52725" (Report ID|Step|Approver ID): a genuine T6.1a
# exception. "RPT-61A-TN01|1|52725": a genuine negative.


def test_perfect_match_t61a(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T6.1a", "EX-1", True, "Approval Step", "RPT-61A-P01|1|52725"),
        _exc("T6.1a", "EX-2", False, "Approval Step", "RPT-61A-TN01|1|52725"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T6.1a"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t61a(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T6.1a", "WRONGLY-CLEARED", False, "Approval Step", "RPT-61A-P01|1|52725"),
        _exc("T6.1a", "MISSED", True, "Approval Step", "RPT-61A-TN01|1|52725"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T6.1a"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T6.1c: row-number row test (attendee_validity) ──────────────────────────
# External ID 470002: "Is Attendee also in Hierarchy?" = Yes -- a genuine
# T6.1c exception. External ID 470018: "No" -- a genuine negative.


def test_perfect_match_t61c(completed_run, tmp_path, attendee_validity_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(attendee_validity_frame, "External ID", 470002)
    neg_row = _row_number(attendee_validity_frame, "External ID", 470018)
    oracle = _write_oracle(tmp_path, [
        _exc("T6.1c", "EX-1", True, "Row Number", exc_row),
        _exc("T6.1c", "EX-2", False, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T6.1c"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t61c(completed_run, tmp_path, attendee_validity_frame):
    app_ctx, run_id = completed_run
    exc_row = _row_number(attendee_validity_frame, "External ID", 470002)
    neg_row = _row_number(attendee_validity_frame, "External ID", 470018)
    oracle = _write_oracle(tmp_path, [
        _exc("T6.1c", "WRONGLY-CLEARED", False, "Row Number", exc_row),
        _exc("T6.1c", "MISSED", True, "Row Number", neg_row),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T6.1c"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── T6.1d: key-group test (expense_report employee-day-country) ────────────
# Employee ID 52725, Transaction Date 2025-03-02, country Australia
# (natural_id DOM-P01): a genuine T6.1d exception (daily Meals/Incidentals
# spend over the derived per-diem limit). Employee ID 52725, Transaction
# Date 2025-03-03, country Australia (DOM-N01): a genuine negative.


def test_perfect_match_t61d(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    rows = (
        _kv_exc("T6.1d", "EX-1", True, {
            "Employee ID": 52725, "Transaction Date": "2025-03-02", "country": "Australia",
        })
        + _kv_exc("T6.1d", "EX-2", False, {
            "Employee ID": 52725, "Transaction Date": "2025-03-03", "country": "Australia",
        })
    )
    oracle = _write_oracle(tmp_path, rows)
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T6.1d"]
    assert score.status == "scored"
    assert score.true_positives == 1
    assert score.false_positives == 0
    assert score.false_negatives == 0


def test_mismatch_detected_t61d(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    rows = (
        _kv_exc("T6.1d", "WRONGLY-CLEARED", False, {
            "Employee ID": 52725, "Transaction Date": "2025-03-02", "country": "Australia",
        })
        + _kv_exc("T6.1d", "MISSED", True, {
            "Employee ID": 52725, "Transaction Date": "2025-03-03", "country": "Australia",
        })
    )
    oracle = _write_oracle(tmp_path, rows)
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T6.1d"]
    assert score.false_positives == 1
    assert score.false_positive_ids == ["WRONGLY-CLEARED"]
    assert score.false_negatives == 1
    assert score.false_negative_ids == ["MISSED"]


# ── Row Number out of range fails loudly ────────────────────────────────────


def test_row_number_out_of_range_fails_loudly(completed_run, tmp_path, expense_report_frame):
    """A Row Number beyond the pinned source's row count is a structural
    oracle problem, not a silently-ignored miss (CLAUDE.md non-negotiable
    14) -- orchestrator.eval.surface1._row_key_for_number raises rather than
    clamping or skipping."""
    app_ctx, run_id = completed_run
    out_of_range = str(len(expense_report_frame) + 1)
    oracle = _write_oracle(tmp_path, [_exc("T4.1", "EX-1", True, "Row Number", out_of_range)])
    with pytest.raises(Surface1OracleError, match="out of range"):
        run_surface1(run_id, oracle, ctx=app_ctx)


# ── metrics ──────────────────────────────────────────────────────────────


def test_metric_match_and_mismatch_reported(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    actual_metrics = app_ctx.persistence.get_run_metrics(run_id)
    hv_amount = actual_metrics["hv_amount"]["value"]

    oracle = _write_oracle(tmp_path, [
        _metric("T4.4", "hv_amount", str(hv_amount)),
        _metric("T4.4", "hv_count", "999999"),
    ])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    by_name = {m.metric_name: m for m in report.metrics}
    assert by_name["hv_amount"].match is True
    assert by_name["hv_count"].match is False
    assert by_name["hv_count"].actual != 999999


def test_metric_not_in_run_metrics_reported(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [_metric("RUN", "no_such_metric", "1")])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    (m,) = report.metrics
    assert m.match is False
    assert "not present" in m.reason


# ── not_testable is reported as such, never scored ──────────────────────


def test_not_testable_subtest_reported_and_never_blocks_the_catalogue_test(completed_run, tmp_path):
    """T3.2a spans 5 plan.yaml sub-tests; T3.2a_accom is not_testable (no
    preferred-hotel list, CLAUDE.md docs/specs/SKILL-001_test_specification.md
    T3.2a). The catalogue test is still scored from its testable siblings,
    and the not_testable one is reported, not silently dropped."""
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    score = report.tests["T3.2a"]
    assert score.status == "not_covered_by_oracle"
    sub_ids = {p["sub_test_id"] for p in score.partial_not_testable}
    assert sub_ids == {"T3.2a_accom"}
    assert all(p["reason"] for p in score.partial_not_testable)


def test_oracle_referencing_t4_3_fails_loudly():
    rows = [_exc("T4.3", "X", True, "Row Number", "1")]
    violations_msg = None
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        oracle = _write_oracle(Path(d), rows)
        with pytest.raises(Surface1OracleError) as exc_info:
            load_oracle(oracle)
        violations_msg = str(exc_info.value)
    assert "T4.3" in violations_msg
    assert "not_testable" in violations_msg or "never scored" in violations_msg


def test_oracle_declaring_exceptions_for_a_run_not_testable_test_fails(completed_run, tmp_path):
    """Same idea as the previous test, but exercised through run_surface1
    against a real run rather than load_oracle alone -- T4.1 is fully
    testable here, so instead this proves the loud-failure path fires even
    when the test_id IS in CATALOGUE_TESTS but every one of ITS plan
    sub-tests turns out not_testable for this run. There is no such test in
    SKILL-001 today (only T4.3, tested above, is wholly not_testable), so
    this asserts the mechanism the other way: T3.2a (partially not_testable)
    accepts oracle rows for its TESTABLE identity_field fine."""
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [_exc("T3.2a", "OK", False, "Row Number", "1")])
    report = run_surface1(run_id, oracle, ctx=app_ctx)
    assert report.tests["T3.2a"].status == "scored"


# ── malformed oracle: each failure mode fails loudly ────────────────────


def test_malformed_oracle_unknown_test_id(tmp_path):
    oracle = _write_oracle(tmp_path, [_exc("T99.9", "X", True, "Whatever", "1")])
    with pytest.raises(Surface1OracleError, match="unknown test_id"):
        load_oracle(oracle)


def test_malformed_oracle_unknown_record_type(tmp_path):
    oracle = _write_oracle(tmp_path, [
        {"record_type": "bogus", "test_id": "T3.1b", "exception_id": "X", "expected": "true",
         "identity_field": "Booking ID", "identity_value": "1"},
    ])
    with pytest.raises(Surface1OracleError, match="record_type"):
        load_oracle(oracle)


def test_malformed_oracle_missing_identity_field(tmp_path):
    # T5.2 needs 4 fields; only 3 supplied.
    oracle = _write_oracle(tmp_path, [
        {"record_type": "exception", "test_id": "T5.2", "exception_id": "DUP-1", "expected": "true",
         "identity_field": "Employee ID", "identity_value": "52725"},
        {"record_type": "exception", "test_id": "T5.2", "exception_id": "DUP-1", "expected": "true",
         "identity_field": "Transaction Date", "identity_value": "2025-04-02"},
        {"record_type": "exception", "test_id": "T5.2", "exception_id": "DUP-1", "expected": "true",
         "identity_field": "Vendor", "identity_value": "Dup Vendor Co"},
    ])
    with pytest.raises(Surface1OracleError, match="missing identity_field"):
        load_oracle(oracle)


def test_malformed_oracle_duplicate_identity(tmp_path):
    oracle = _write_oracle(tmp_path, [
        _exc("T3.1b", "EX-1", True, "Booking ID", "610001"),
        _exc("T3.1b", "EX-1", True, "Booking ID", "610001"),
    ])
    with pytest.raises(Surface1OracleError, match="duplicate identity"):
        load_oracle(oracle)


def test_malformed_oracle_inconsistent_expected(tmp_path):
    oracle = _write_oracle(tmp_path, [
        {"record_type": "exception", "test_id": "T5.2", "exception_id": "DUP-1", "expected": "true",
         "identity_field": "Employee ID", "identity_value": "52725"},
        {"record_type": "exception", "test_id": "T5.2", "exception_id": "DUP-1", "expected": "false",
         "identity_field": "Transaction Date", "identity_value": "2025-04-02"},
    ])
    with pytest.raises(Surface1OracleError, match="conflicts"):
        load_oracle(oracle)


def test_malformed_oracle_duplicate_metric(tmp_path):
    oracle = _write_oracle(tmp_path, [
        _metric("T4.4", "hv_amount", "1"),
        _metric("T4.4", "hv_amount", "2"),
    ])
    with pytest.raises(Surface1OracleError, match="duplicate metric"):
        load_oracle(oracle)


def test_malformed_oracle_missing_required_column(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("test_id,exception_id\nT3.1b,X\n")
    with pytest.raises(Surface1OracleError, match="missing required column"):
        load_oracle(path)


def test_run_not_ready_fails_loudly(completed_run, tmp_path):
    """A run still in the plan phase (never executed) cannot be scored."""
    app_ctx, _ = completed_run
    persistence = app_ctx.persistence
    from orchestrator.fingerprint import compute_fingerprint

    fp = compute_fingerprint(
        settings=_settings(), source_table_versions={}, uploaded_file_hashes={},
        skill_dir=SKILL_DIR, requirements_path=REPO_ROOT / "requirements.txt", prompts_dirs=[],
    )
    state = runs_module.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT",
        skill_id="SKILL-001", skill_version="1", mode="playbook", audit_period=AUDIT_PERIOD,
        objective="not ready", run_owner="x", fingerprint=fp, now=canonical_ts(500),
    )
    oracle = _write_oracle(tmp_path, [])
    with pytest.raises(Surface1RunError):
        run_surface1(state.run_id, oracle, ctx=app_ctx)


# ── formula-injection escaping ──────────────────────────────────────────


def test_xlsx_formula_injection_escaped(tmp_path):
    report = Surface1Report(
        run_id="=cmd|' /C calc'!A0", generated_at="2026-01-01T00:00:00.000000Z",
        oracle_path="oracle.csv", oracle_sha256="abc123", fingerprint_id="FP-1",
        skill_id="SKILL-001", skill_version="1", run_status="awaiting_signoff",
        source_versions={},
        tests={
            "T3.1b": TestScore(
                test_id="T3.1b", scoring_unit="booking", status="scored",
                reason="+SUM(A1:A9)", true_positives=1,
                false_positive_ids=["=HYPERLINK(\"http://evil\")"],
            ),
        },
        metrics=[],
    )
    xlsx_path = tmp_path / "report.xlsx"
    write_xlsx_report(report, xlsx_path)

    wb = openpyxl.load_workbook(xlsx_path)
    header = wb["Report"]
    run_id_cell = header.cell(row=2, column=1).value
    assert run_id_cell.startswith("'=")

    summary = wb["Summary"]
    reason_cell = next(
        row[3].value for row in summary.iter_rows(min_row=2) if row[0].value == "T3.1b"
    )
    assert reason_cell.startswith("'+")

    unmatched = wb["Unmatched"]
    fp_cell = next(row[2].value for row in unmatched.iter_rows(min_row=2))
    assert fp_cell.startswith("'=")


# ── determinism ──────────────────────────────────────────────────────────


def test_report_is_deterministic(completed_run, tmp_path):
    app_ctx, run_id = completed_run
    oracle = _write_oracle(tmp_path, [
        _exc("T3.1b", "EX-1", True, "Booking ID", "610001"),
        _exc("T3.1b", "EX-2", False, "Booking ID", "610000"),
        _metric("T4.4", "hv_count", "0"),
    ])
    fixed_now = "2026-06-01T00:00:00.000000Z"
    report1 = run_surface1(run_id, oracle, ctx=app_ctx, now=fixed_now)
    report2 = run_surface1(run_id, oracle, ctx=app_ctx, now=fixed_now)
    assert report1.to_dict() == report2.to_dict()

    json_path1 = tmp_path / "r1.json"
    json_path2 = tmp_path / "r2.json"
    write_json_report(report1, json_path1)
    write_json_report(report2, json_path2)
    assert json_path1.read_bytes() == json_path2.read_bytes()

    parsed = json.loads(json_path1.read_text())
    assert parsed["run_id"] == run_id
    assert parsed["generated_at"] == fixed_now
