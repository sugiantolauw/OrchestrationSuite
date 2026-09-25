from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.contract import ContractViolation, LocalFileDataSource
from orchestrator.run_inputs import resolve_run_inputs
from orchestrator.skills import load_skill

MINI_SKILL_DIR = Path(__file__).parent / "fixtures" / "skills" / "mini"

CLAIMS_CSV = (
    "Employee ID,Transaction Date,Amount,Vendor\n"
    "1,2025-01-05,600,Acme\n"
)
REGISTER_CSV = "Employee ID,Transaction Date,Vendor\n1,2025-01-05,Acme\n"


@pytest.fixture()
def mini_skill():
    return load_skill(MINI_SKILL_DIR)


@pytest.fixture()
def data_source(tmp_path: Path, mini_skill):
    (tmp_path / "claims.csv").write_text(CLAIMS_CSV)
    (tmp_path / "register.csv").write_text(REGISTER_CSV)
    return LocalFileDataSource(root_dir=tmp_path, sources=mini_skill.contract["sources"])


def _versions(data_source, mini_skill):
    return {name: data_source.resolve_version(name) for name in mini_skill.contract["sources"]}


def test_empty_configured_returns_empty_run_inputs(mini_skill, data_source):
    result = resolve_run_inputs(mini_skill, {}, data_source)
    assert result == {"mappings": {}, "not_supplied": {}, "parameters": {}}


def test_column_mapping_is_validated_and_returned(mini_skill, data_source):
    configured = {"claims": {"kind": "volume_file", "path": "claims.csv", "columns": {"Vendor": "Vendor"}}}
    result = resolve_run_inputs(mini_skill, configured, data_source, source_versions=_versions(data_source, mini_skill))
    assert result["mappings"] == {"claims": {"Vendor": "Vendor"}}


def test_unknown_contract_column_key_is_refused(mini_skill, data_source):
    configured = {"claims": {"kind": "volume_file", "path": "claims.csv", "columns": {"Not A Column": "Vendor"}}}
    with pytest.raises(ContractViolation, match="Not A Column"):
        resolve_run_inputs(mini_skill, configured, data_source, source_versions=_versions(data_source, mini_skill))


def test_missing_physical_column_is_refused(mini_skill, data_source):
    configured = {"claims": {"kind": "volume_file", "path": "claims.csv", "columns": {"Vendor": "Nonexistent Col"}}}
    with pytest.raises(ContractViolation, match="Nonexistent Col"):
        resolve_run_inputs(mini_skill, configured, data_source, source_versions=_versions(data_source, mini_skill))


def test_collision_two_contract_columns_to_same_physical_is_refused(mini_skill, data_source):
    configured = {
        "claims": {
            "kind": "volume_file", "path": "claims.csv",
            "columns": {"Vendor": "Amount", "Employee ID": "Amount"},
        }
    }
    with pytest.raises(ContractViolation, match="collision"):
        resolve_run_inputs(mini_skill, configured, data_source, source_versions=_versions(data_source, mini_skill))


def test_ambiguous_shadowing_is_refused(mini_skill, data_source):
    # "Vendor" is remapped to "Amount"; "Amount" itself is left unmapped, but its
    # own name is now also the physical target of the Vendor mapping -- ambiguous.
    configured = {"claims": {"kind": "volume_file", "path": "claims.csv", "columns": {"Vendor": "Amount"}}}
    with pytest.raises(ContractViolation, match="ambiguous"):
        resolve_run_inputs(mini_skill, configured, data_source, source_versions=_versions(data_source, mini_skill))


def test_not_supplied_requires_optional_source(mini_skill, data_source):
    configured = {"claims": {"kind": "not_supplied", "reason": "no such file"}}
    with pytest.raises(ContractViolation, match="not declare it optional"):
        resolve_run_inputs(mini_skill, configured, data_source)


def test_not_supplied_requires_non_empty_reason(mini_skill, data_source):
    configured = {"register": {"kind": "not_supplied", "reason": "   "}}
    with pytest.raises(ContractViolation, match="non-empty reason"):
        resolve_run_inputs(mini_skill, configured, data_source)


def test_not_supplied_records_reason_and_affected_tests(mini_skill, data_source):
    configured = {"register": {"kind": "not_supplied", "reason": "not held by this BU"}}
    result = resolve_run_inputs(mini_skill, configured, data_source)
    assert result["not_supplied"] == {
        "register": {"reason": "not held by this BU", "affected_tests": ["T2"]},
    }


def test_unknown_source_name_is_refused(mini_skill, data_source):
    configured = {"not_a_real_source": {"kind": "volume_file", "path": "x.csv"}}
    with pytest.raises(ContractViolation, match="not a contract source"):
        resolve_run_inputs(mini_skill, configured, data_source)


def test_unknown_parameter_name_is_refused(mini_skill, data_source):
    configured = {
        "parameters": {
            "not_a_real_parameter": {
                "path": "x.csv", "format": "csv", "provenance": {"owner": "a", "as_of": "2026-01-01"},
            },
        },
    }
    with pytest.raises(ContractViolation, match="unknown parameter"):
        resolve_run_inputs(mini_skill, configured, data_source)


def test_parameter_missing_provenance_is_refused(mini_skill, data_source, tmp_path):
    ids_path = tmp_path / "ids.csv"
    ids_path.write_text("id\n1\n2\n")
    configured = {
        "parameters": {
            "population_of_interest": {"path": str(ids_path), "format": "csv", "provenance": {}},
        },
    }
    with pytest.raises(ContractViolation, match="owner"):
        resolve_run_inputs(mini_skill, configured, data_source)


def test_parameter_empty_file_is_refused(mini_skill, data_source, tmp_path):
    ids_path = tmp_path / "ids.csv"
    ids_path.write_text("id\n")
    configured = {
        "parameters": {
            "population_of_interest": {
                "path": str(ids_path), "format": "csv",
                "provenance": {"owner": "a", "as_of": "2026-01-01"},
            },
        },
    }
    with pytest.raises(ContractViolation, match="empty"):
        resolve_run_inputs(mini_skill, configured, data_source)


def test_parameter_resolves_and_hashes(mini_skill, data_source, tmp_path):
    ids_path = tmp_path / "ids.csv"
    ids_path.write_text("id\n1\n2\n3\n")
    configured = {
        "parameters": {
            "population_of_interest": {
                "path": str(ids_path), "format": "csv",
                "provenance": {"owner": "a", "as_of": "2026-01-01"},
            },
        },
    }
    result = resolve_run_inputs(mini_skill, configured, data_source)
    param = result["parameters"]["population_of_interest"]
    assert param["value"] == [1, 2, 3]
    assert param["kind"] == "id_list"
    assert len(param["sha256"]) == 64
    assert param["provenance"] == {"owner": "a", "as_of": "2026-01-01"}


def test_parameters_key_never_a_source_name(mini_skill, data_source):
    # "parameters" as a key inside `configured` is always the reserved block,
    # never mistaken for a contract source named "parameters" (none is).
    assert "parameters" not in mini_skill.contract["sources"]
