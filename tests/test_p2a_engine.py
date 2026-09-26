from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.contract import ContractViolation, LocalFileDataSource, SourceVersionMismatch
from orchestrator.engine import execute_skill, to_ui_payload
from orchestrator.skills import load_skill

MINI_SKILL_DIR = Path(__file__).parent / "fixtures" / "skills" / "mini"

CLAIMS_CSV = (
    "Employee ID,Transaction Date,Amount,Vendor\n"
    "1,2025-01-05,600,Acme\n"
    "1,2025-01-06,100,Acme\n"
    "2,2025-01-10,900,Beta\n"
    "2,2025-01-11,50,Beta\n"
    "3,2024-12-31,800,Gamma\n"  # out of the audit period
)
REGISTER_CSV = "Employee ID,Transaction Date,Vendor\n1,2025-01-05,Acme\n2,2025-01-11,Beta\n"


def _write_fixture_data(tmp_path: Path) -> None:
    (tmp_path / "claims.csv").write_text(CLAIMS_CSV)
    (tmp_path / "register.csv").write_text(REGISTER_CSV)


@pytest.fixture()
def mini_skill():
    skill = load_skill(MINI_SKILL_DIR)
    skill.validate()
    return skill


def test_engine_end_to_end_on_mini_skill(tmp_path: Path, mini_skill):
    _write_fixture_data(tmp_path)
    ds = LocalFileDataSource(root_dir=tmp_path, sources=mini_skill.contract["sources"])

    result = execute_skill(
        mini_skill,
        data_source=ds,
        audit_period=("2025-01-01", "2025-01-31"),
        run_context={"run_id": "run-1"},
    )

    # source versions pinned before read (contract.py's own tests cover the
    # resolve-then-read ordering directly; here we assert the result carries them).
    assert set(result.source_versions) == {"claims", "register"}
    for name, version in result.source_versions.items():
        assert version == ds.resolve_version(name)

    assert result.metrics["hv_count"]["value"] == 2  # 600 and 900 both > 500
    assert result.metrics["hv_amount"]["value"] == 1500.0
    assert result.metrics["missing_count"]["value"] == 2  # both register rows match
    assert result.metrics["missing_pct"]["value"] == 50.0

    statuses = {t["test_id"]: t["status"] for t in result.test_results}
    assert statuses == {"T1": "exception", "T2": "exception", "T3": "not_testable"}
    not_testable = next(t for t in result.test_results if t["test_id"] == "T3")
    assert not_testable["reason"] == "no classification endpoint wired in P2a"

    # N11: a not_testable test's declared RF_* column exists in the flags
    # frame, all-null (never 0) -- P4 renders that as "Not tested", distinct
    # from a real "No breach" (0/false).
    assert "RF_T3_NotTested" in result.flags.columns
    assert str(result.flags["RF_T3_NotTested"].dtype) == "Int8"
    assert result.flags["RF_T3_NotTested"].isna().all()

    assert "RF_HV" in result.flags.columns
    assert "RF_MISSING" in result.flags.columns
    assert int(result.flags["RF_HV"].sum()) == 2
    assert int(result.flags["RF_MISSING"].sum()) == 2

    assert result.data_quality["claims_pop.excluded.filter[0]:Transaction Date:between"] == 1

    assert {f["rule_id"] for f in result.findings} == {"SKILL-MINI.T1", "SKILL-MINI.T2"}
    high_finding = next(f for f in result.findings if f["rule_id"] == "SKILL-MINI.T1")
    assert high_finding["severity"] == "High"  # hv_count=2 > hv_high threshold of 1


def test_engine_never_calls_an_llm_signature():
    # execute_skill's only inputs are a skill, a data source, an audit period,
    # run_context, an optional pinned_versions override and an optional
    # not_supplied set (independent review 2026-09-25 item 1, "run inputs")
    # -- no model client parameter exists to call (NN2).
    import inspect

    sig = inspect.signature(execute_skill)
    assert set(sig.parameters) == {
        "skill", "data_source", "audit_period", "run_context", "pinned_versions", "not_supplied",
    }


def test_engine_source_version_pinned_before_read_hash_mismatch_fails(tmp_path: Path, mini_skill):
    _write_fixture_data(tmp_path)
    ds = LocalFileDataSource(root_dir=tmp_path, sources=mini_skill.contract["sources"])

    # Resolve the version as execute_skill would, then mutate the file underneath
    # it -- the TOCTOU gap CLAUDE.md §4.1 requires resolve-then-read to close.
    stale_version = ds.resolve_version("claims")
    (tmp_path / "claims.csv").write_text(CLAIMS_CSV + "4,2025-01-15,700,Delta\n")

    with pytest.raises(SourceVersionMismatch):
        ds.read_population("claims", version=stale_version)


def test_engine_contract_violation_propagates_and_fails_the_run(tmp_path: Path, mini_skill):
    bad_claims = (
        "Employee ID,Transaction Date,Amount,Vendor\n"
        "1,2025-01-05,not-a-number,Acme\n"
    )
    (tmp_path / "claims.csv").write_text(bad_claims)
    (tmp_path / "register.csv").write_text(REGISTER_CSV)
    ds = LocalFileDataSource(root_dir=tmp_path, sources=mini_skill.contract["sources"])

    with pytest.raises(ContractViolation):
        execute_skill(
            mini_skill,
            data_source=ds,
            audit_period=("2025-01-01", "2025-01-31"),
            run_context={"run_id": "run-1"},
        )


def test_to_ui_payload_shape_matches_app_py_metric_keys(tmp_path: Path, mini_skill):
    _write_fixture_data(tmp_path)
    ds = LocalFileDataSource(root_dir=tmp_path, sources=mini_skill.contract["sources"])
    result = execute_skill(
        mini_skill, data_source=ds, audit_period=("2025-01-01", "2025-01-31"), run_context={"run_id": "run-1"}
    )
    payload = to_ui_payload(result, audit_period_label="01 Jan 2025 - 31 Jan 2025")

    assert payload["audit_period"] == "01 Jan 2025 - 31 Jan 2025"
    # missing_amount (CLAUDE.md P2/P3 gate review item 1): added to the mini
    # fixture's T2 so it remains a monetary finding under
    # orchestrator.nodes.fieldwork.prioritise's metric-driven exposure rule --
    # see tests/test_exposure_dedup.py's module docstring.
    assert set(payload["metrics"]) == {"hv_count", "hv_amount", "missing_count", "missing_pct", "missing_amount"}
    # app.py's compute_evidence_payload metrics are {value, unit, source_file}
    # dicts (source_ref is this engine's addition, carrying NN10 provenance).
    for name, metric in payload["metrics"].items():
        assert set(metric) >= {"value", "unit", "source_file", "source_ref"}
        assert metric["source_file"] == "claims"
    assert payload["metrics"]["hv_count"]["value"] == 2
    assert payload["metrics"]["hv_amount"]["unit"] == "AUD"

    # to_ui_payload never invents a key the engine cannot compute -- no
    # Skill-specific keys like app.py's `exco_members` appear here.
    assert "exco_members" not in payload


def test_duplicate_metric_name_across_tests_fails_loudly(tmp_path: Path, mini_skill):
    _write_fixture_data(tmp_path)
    ds = LocalFileDataSource(root_dir=tmp_path, sources=mini_skill.contract["sources"])
    # Force a metric-name collision by aliasing T2's metric to T1's name.
    mini_skill.plan["tests"][1]["params"]["metrics"]["hv_count"] = {"kind": "count"}
    del mini_skill.plan["tests"][1]["params"]["metrics"]["missing_count"]

    with pytest.raises(ValueError, match="duplicate metric name"):
        execute_skill(
            mini_skill,
            data_source=ds,
            audit_period=("2025-01-01", "2025-01-31"),
            run_context={"run_id": "run-1"},
        )
