from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from orchestrator.contract import (
    ContractViolation,
    LocalFileDataSource,
    SourceVersionMismatch,
    sha256_file,
    validate_contract,
)


def _df(rows, row_keys=None):
    df = pd.DataFrame(rows)
    df.insert(0, "__row_key", row_keys or [f"k:{i + 1}" for i in range(len(df))])
    df.insert(0, "__source", "src")
    return df


def test_missing_column_named_exactly():
    df = _df({"Currency": ["AUD", "AUD"]})
    contract = {"columns": {"Amount": {"type": "number", "nullable": False}}}
    with pytest.raises(ContractViolation) as exc:
        validate_contract(df, contract)
    assert "missing column: 'Amount'" in str(exc.value)


def test_no_fuzzy_column_matching_amount_with_trailing_space():
    # A column literally named "Amount " does not satisfy a contract requiring
    # "Amount" -- header_trim is a DataSourceAdapter-level, declared behaviour,
    # not something validate_contract infers on the caller's behalf.
    df = _df({"Amount ": [1, 2]})
    contract = {"columns": {"Amount": {"type": "number", "nullable": False}}}
    with pytest.raises(ContractViolation) as exc:
        validate_contract(df, contract)
    assert "missing column: 'Amount'" in str(exc.value)


def test_no_fuzzy_column_matching_substring_never_matches():
    # "Expense Amount" never satisfies "Amount" -- no substring/contains matching.
    df = _df({"Expense Amount": [1, 2]})
    contract = {"columns": {"Amount": {"type": "number", "nullable": False}}}
    with pytest.raises(ContractViolation):
        validate_contract(df, contract)


def test_type_coercion_failure_reports_sample_row_keys():
    df = _df({"Amount": [100, "not-a-number", 50]}, row_keys=["k:1", "k:2", "k:3"])
    contract = {"columns": {"Amount": {"type": "number", "nullable": True}}}
    with pytest.raises(ContractViolation) as exc:
        validate_contract(df, contract)
    msg = str(exc.value)
    assert "1 value(s) fail type coercion to 'number'" in msg
    assert "k:2" in msg


def test_integer_type_rejects_fractional_values():
    df = _df({"Count": [1, 2.5, 3]})
    contract = {"columns": {"Count": {"type": "integer", "nullable": True}}}
    with pytest.raises(ContractViolation) as exc:
        validate_contract(df, contract)
    assert "Count" in str(exc.value)


def test_null_in_non_nullable_column():
    df = _df({"Employee ID": [1, None, 3]})
    contract = {"columns": {"Employee ID": {"type": "integer", "nullable": False}}}
    with pytest.raises(ContractViolation) as exc:
        validate_contract(df, contract)
    assert "null value(s) in non-nullable column" in str(exc.value)


def test_required_values_aud_violation():
    df = _df({"Reimbursement Currency": ["AUD", "USD", "AUD"]}, row_keys=["k:1", "k:2", "k:3"])
    contract = {
        "columns": {
            "Reimbursement Currency": {
                "type": "string",
                "nullable": False,
                "required_values": ["AUD"],
            }
        }
    }
    with pytest.raises(ContractViolation) as exc:
        validate_contract(df, contract)
    msg = str(exc.value)
    assert "required_values" in msg
    assert "USD" in msg
    assert "k:2" in msg


def test_allowed_values_violation():
    df = _df({"Status": ["Approved", "Weird"]})
    contract = {"columns": {"Status": {"type": "string", "nullable": True, "allowed_values": ["Approved", "Rejected"]}}}
    with pytest.raises(ContractViolation) as exc:
        validate_contract(df, contract)
    assert "allowed_values" in str(exc.value)


def test_trim_values_declared_vs_not():
    df = _df({"Currency": [" AUD ", "AUD"]})
    trimming_contract = {"columns": {"Currency": {"type": "string", "nullable": True, "trim_values": True, "required_values": ["AUD"]}}}
    validate_contract(df, trimming_contract)  # no violation once trimmed
    assert df["Currency"].tolist() == ["AUD", "AUD"]

    df2 = _df({"Currency": [" AUD ", "AUD"]})
    no_trim_contract = {"columns": {"Currency": {"type": "string", "nullable": True, "required_values": ["AUD"]}}}
    with pytest.raises(ContractViolation):
        validate_contract(df2, no_trim_contract)


def test_multiple_violations_all_reported_together():
    df = _df({"Amount": [None, "bad"], "Currency": ["USD", "AUD"]})
    contract = {
        "columns": {
            "Amount": {"type": "number", "nullable": False},
            "Currency": {"type": "string", "nullable": False, "required_values": ["AUD"]},
        }
    }
    with pytest.raises(ContractViolation) as exc:
        validate_contract(df, contract)
    violations = exc.value.violations
    assert any("Amount" in v for v in violations)
    assert any("Currency" in v for v in violations)


def test_extra_columns_are_allowed_and_ignored():
    df = _df({"Amount": [1, 2], "Unrelated Column": ["x", "y"]})
    contract = {"columns": {"Amount": {"type": "number", "nullable": False}}}
    validate_contract(df, contract)  # no raise


# --- LocalFileDataSource -----------------------------------------------------


def test_local_file_data_source_reads_and_pins_version(tmp_path: Path):
    csv_path = tmp_path / "claims.csv"
    csv_path.write_text("Amount ,Vendor\n100,Acme\n200,Beta\n")

    sources = {"claims": {"format": "csv", "file": "claims.csv", "header_trim": True, "columns": {}}}
    ds = LocalFileDataSource(root_dir=tmp_path, sources=sources)

    version = ds.resolve_version("claims")
    assert version == sha256_file(csv_path)

    df = ds.read_population("claims", version=version)
    assert list(df.columns)[:2] == ["__source", "__row_key"]
    assert "Amount" in df.columns  # header trimmed
    assert df["__row_key"].tolist() == [f"{version[:16]}:1", f"{version[:16]}:2"]
    assert (df["__source"] == "claims").all()


def test_local_file_data_source_detects_version_mismatch(tmp_path: Path):
    csv_path = tmp_path / "claims.csv"
    csv_path.write_text("Amount\n1\n")
    sources = {"claims": {"format": "csv", "file": "claims.csv", "columns": {}}}
    ds = LocalFileDataSource(root_dir=tmp_path, sources=sources)

    stale_version = ds.resolve_version("claims")
    csv_path.write_text("Amount\n1\n2\n")  # file changes after resolve_version
    with pytest.raises(SourceVersionMismatch):
        ds.read_population("claims", version=stale_version)


def test_local_file_data_source_missing_file_raises(tmp_path: Path):
    sources = {"claims": {"format": "csv", "file": "missing.csv", "columns": {}}}
    ds = LocalFileDataSource(root_dir=tmp_path, sources=sources)
    with pytest.raises(ContractViolation):
        ds.resolve_version("claims")
