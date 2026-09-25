from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.adapters.mapped_source import MappedDataSource
from orchestrator.contract import LocalFileDataSource

SOURCES = {
    "claims": {
        "format": "csv", "file": "claims.csv", "header_trim": True,
        "columns": {
            "Employee ID": {"type": "integer", "nullable": False},
            "Amount": {"type": "number", "nullable": False},
        },
    },
}
CLAIMS_CSV = "Emp No,Amt\n1,600\n2,50\n"


@pytest.fixture()
def inner(tmp_path: Path):
    (tmp_path / "claims.csv").write_text(CLAIMS_CSV)
    return LocalFileDataSource(root_dir=tmp_path, sources=SOURCES)


def test_read_population_renames_both_ways(inner):
    mapped = MappedDataSource(inner, {"claims": {"Employee ID": "Emp No", "Amount": "Amt"}})
    version = mapped.resolve_version("claims")
    df = mapped.read_population("claims", version=version)
    assert set(df.columns) == {"__source", "__row_key", "Employee ID", "Amount"}
    assert list(df["Employee ID"]) == [1, 2]
    assert list(df["Amount"]) == [600, 50]


def test_read_population_columns_pushdown_translated(inner):
    mapped = MappedDataSource(inner, {"claims": {"Employee ID": "Emp No", "Amount": "Amt"}})
    version = mapped.resolve_version("claims")
    df = mapped.read_population("claims", version=version, columns=["Employee ID"])
    assert set(df.columns) == {"__source", "__row_key", "Employee ID"}


def test_no_mapping_for_source_is_a_no_op(inner):
    mapped = MappedDataSource(inner, {})
    version = mapped.resolve_version("claims")
    df_inner = inner.read_population("claims", version=version)
    df_mapped = mapped.read_population("claims", version=version)
    assert list(df_mapped.columns) == list(df_inner.columns)


def test_column_stats_translates_amount_and_date_columns(inner):
    mapped = MappedDataSource(inner, {"claims": {"Amount": "Amt"}})
    version = mapped.resolve_version("claims")
    stats = mapped.column_stats("claims", version=version, amount_column="Amount")
    assert stats["amount"] == 650.0


def test_distinct_count_translates_columns(inner):
    mapped = MappedDataSource(inner, {"claims": {"Employee ID": "Emp No"}})
    version = mapped.resolve_version("claims")
    assert mapped.distinct_count("claims", version=version, columns=["Employee ID"]) == 2


def test_row_count_and_resolve_version_delegate_unmodified(inner):
    mapped = MappedDataSource(inner, {"claims": {"Employee ID": "Emp No"}})
    version = mapped.resolve_version("claims")
    assert version == inner.resolve_version("claims")
    assert mapped.row_count("claims", version=version) == inner.row_count("claims", version=version)


def test_shadowed_physical_column_is_dropped(tmp_path: Path):
    # The physical file has BOTH "Emp No" (mapped to "Employee ID") and a
    # column literally already named "Employee ID" -- after the rename, the
    # pre-existing "Employee ID" column is shadowed and dropped rather than
    # colliding with the renamed one.
    (tmp_path / "claims.csv").write_text("Emp No,Employee ID,Amt\n1,999,600\n")
    sources = dict(SOURCES)
    inner = LocalFileDataSource(root_dir=tmp_path, sources=sources)
    mapped = MappedDataSource(inner, {"claims": {"Employee ID": "Emp No"}})
    version = mapped.resolve_version("claims")
    df = mapped.read_population("claims", version=version)
    assert list(df["Employee ID"]) == [1]  # the renamed "Emp No", never the shadowed 999
