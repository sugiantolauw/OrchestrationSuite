from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.contract import LocalFileDataSource
from orchestrator.engine import execute_skill
from orchestrator.skills import load_skill

MINI_SKILL_DIR = Path(__file__).parent / "fixtures" / "skills" / "mini"

CLAIMS_CSV = (
    "Employee ID,Transaction Date,Amount,Vendor\n"
    "1,2025-01-05,600,Acme\n"
    "1,2025-01-06,100,Acme\n"
    "2,2025-01-10,900,Beta\n"
)


@pytest.fixture()
def mini_skill():
    skill = load_skill(MINI_SKILL_DIR)
    skill.validate()
    return skill


def _full_run(tmp_path: Path, mini_skill):
    (tmp_path / "claims.csv").write_text(CLAIMS_CSV)
    (tmp_path / "register.csv").write_text("Employee ID,Transaction Date,Vendor\n")
    ds = LocalFileDataSource(root_dir=tmp_path, sources=mini_skill.contract["sources"])
    return execute_skill(
        mini_skill, data_source=ds, audit_period=("2025-01-01", "2025-01-31"),
        run_context={"run_id": "run-1"},
    )


def _unsupplied_run(tmp_path: Path, mini_skill):
    (tmp_path / "claims.csv").write_text(CLAIMS_CSV)
    # register.csv is deliberately absent -- register is not_supplied for this run.
    ds = LocalFileDataSource(root_dir=tmp_path, sources=mini_skill.contract["sources"])
    return execute_skill(
        mini_skill, data_source=ds, audit_period=("2025-01-01", "2025-01-31"),
        run_context={"run_id": "run-1"},
        not_supplied={"register": "not held by this business unit"},
    )


def test_only_the_dependent_test_becomes_not_testable(tmp_path: Path, mini_skill):
    result = _unsupplied_run(tmp_path, mini_skill)
    statuses = {t["test_id"]: t for t in result.test_results}
    assert statuses["T1"]["status"] == "exception"  # T1 depends only on claims, unaffected
    assert statuses["T2"]["status"] == "not_testable"
    assert "register" in statuses["T2"]["reason"]
    assert "not held by this business unit" in statuses["T2"]["reason"]
    assert statuses["T3"]["status"] == "not_testable"  # authoring-time not_testable, unrelated


def test_flags_are_null_not_zero_for_the_newly_not_testable_test(tmp_path: Path, mini_skill):
    result = _unsupplied_run(tmp_path, mini_skill)
    assert "RF_MISSING" in result.flags.columns
    assert str(result.flags["RF_MISSING"].dtype) == "Int8"
    assert result.flags["RF_MISSING"].isna().all()


def test_dependent_finding_is_not_raised(tmp_path: Path, mini_skill):
    result = _unsupplied_run(tmp_path, mini_skill)
    rule_ids = {f["rule_id"] for f in result.findings}
    assert "SKILL-MINI.T2" not in rule_ids
    assert "SKILL-MINI.T1" in rule_ids


def test_unsupplied_source_never_read_and_not_in_source_versions(tmp_path: Path, mini_skill):
    result = _unsupplied_run(tmp_path, mini_skill)
    assert "register" not in result.source_versions
    assert "register" not in result.raw_frames
    assert "register_pop" not in result.population_objects


def test_the_other_tests_numbers_are_identical_to_a_full_run(tmp_path: Path, mini_skill):
    full = _full_run(tmp_path, mini_skill)
    partial = _unsupplied_run(tmp_path, mini_skill)
    assert full.metrics["hv_count"]["value"] == partial.metrics["hv_count"]["value"]
    assert full.metrics["hv_amount"]["value"] == partial.metrics["hv_amount"]["value"]
