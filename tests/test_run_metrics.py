"""Run-level reporting metrics (CLAUDE.md build brief P3 §2, N6; the spec's
§2 "Run-level metrics (all computed)"): total_records, total_files,
months_covered and claims_prepared/approved/combined, declared in SKILL-001's
plan.yaml `run_metrics:` and evaluated generically by
orchestrator.engine._evaluate_run_metrics. These replace the hardcoded
152_921 / 8 / 16 in computation.py:641-643 -- verified below against the
real synthetic_data/ figures the spec itself records (§0
"Expense population P_EXP"): 3,711 rows before the period filter (prepared
1,417 / approved 2,317 / both 23)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.contract import LocalFileDataSource
from orchestrator.engine import execute_skill
from orchestrator.skills import load_skill

REPO_ROOT = Path(__file__).parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
PLANTED_DATA_DIR = REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "data"
SYNTHETIC_DATA_DIR = REPO_ROOT / "synthetic_data"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

pytestmark = pytest.mark.skipif(
    not PLANTED_DATA_DIR.is_dir(), reason="tests/fixtures/tne_planted/data/ not present -- run generate.py first"
)


def _run(data_dir: Path):
    skill = load_skill(SKILL_DIR)
    skill.validate()
    ds = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    return execute_skill(skill, data_source=ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "run-metrics-test"})


def test_run_metrics_present_and_well_formed():
    result = _run(PLANTED_DATA_DIR)
    for name in (
        "total_records", "total_files", "months_covered",
        "claims_prepared_rows", "claims_prepared_amount",
        "claims_approved_rows", "claims_approved_amount",
        "claims_combined_rows", "claims_combined_amount",
    ):
        assert name in result.metrics, f"missing run metric {name!r}"

    assert result.metrics["total_files"]["value"] == 8
    assert result.metrics["months_covered"]["value"] == 16  # 2025-01 .. 2026-04 inclusive
    # combined is the row-key UNION of prepared and approved, never their sum
    # (CLAUDE.md §0 -- computation.py's concatenation double-counted overlap
    # rows; the fix here is exactly that overlap never being counted twice).
    prepared = result.metrics["claims_prepared_rows"]["value"]
    approved = result.metrics["claims_approved_rows"]["value"]
    combined = result.metrics["claims_combined_rows"]["value"]
    assert combined <= prepared + approved
    assert combined >= max(prepared, approved)


@pytest.mark.skipif(not SYNTHETIC_DATA_DIR.is_dir(), reason="synthetic_data/ not present in this checkout")
def test_run_metrics_match_the_spec_reference_figures():
    """The one place this checkout can check against a real, independently
    stated number (docs/specs/SKILL-001_test_specification.md §0): the full
    real synthetic_data/, unfiltered by period, is 3,711 P_EXP rows --
    1,417 prepared, 2,317 approved, 23 in both (union = 1417+2317-23)."""
    result = _run(SYNTHETIC_DATA_DIR)
    assert result.metrics["total_records"]["value"] == 152_921
    assert result.metrics["total_files"]["value"] == 8
    assert result.metrics["months_covered"]["value"] == 16
    assert result.metrics["claims_prepared_rows"]["value"] == 1417
    assert result.metrics["claims_approved_rows"]["value"] == 2317
    assert result.metrics["claims_combined_rows"]["value"] == 3711
