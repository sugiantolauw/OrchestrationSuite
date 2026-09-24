"""orchestrator.explorer.profile (docs/specs/P6_P8_explorer_llm_design.md
§4.3, §4.3.1): type/semantic-type inference, the shared pandas profiling
implementation, PII classification + masking, and `profile_columns`/
`distinct_count` on all three DataSourceAdapters. Fully offline."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from orchestrator.adapters.datasource_volume_upload import VolumeUploadAwareDataSource
from orchestrator.contract import LocalFileDataSource
from orchestrator.explorer.profile import (
    classify_pii,
    classify_semantic_type,
    infer_column_type,
    load_repo_pii_flags,
    mask_pii_column,
    pandas_distinct_count,
    pandas_profile_columns,
    profile_source,
)
from test_datasource_uc import FakeConnection, _ds as _uc_ds, _std_handlers

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── infer_column_type / classify_semantic_type ──────────────────────────────


def test_infer_column_type_basic_dtypes():
    assert infer_column_type(pd.Series([1, 2, 3])) == "integer"
    assert infer_column_type(pd.Series([1.0, 2.5])) == "number"
    assert infer_column_type(pd.Series(["a", "b"])) == "string"
    assert infer_column_type(pd.Series([True, False])) == "boolean"


def test_infer_column_type_distinguishes_date_from_datetime():
    dates = pd.to_datetime(["2026-01-01", "2026-01-02"])
    datetimes = pd.to_datetime(["2026-01-01 10:30:00", "2026-01-02 00:00:00"])
    assert infer_column_type(pd.Series(dates)) == "date"
    assert infer_column_type(pd.Series(datetimes)) == "datetime"


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (dict(name="Transaction Date", col_type="date", distinct_count=5, row_count=10, unique=False,
              is_currency_code=False, max_distinct=30), "date"),
        (dict(name="Currency", col_type="string", distinct_count=1, row_count=10, unique=False,
              is_currency_code=True, max_distinct=30), "currency_code"),
        (dict(name="Approved", col_type="string", distinct_count=2, row_count=10, unique=False,
              is_currency_code=False, max_distinct=30, two_value_set={"y", "n"}), "flag"),
        (dict(name="Employee ID", col_type="integer", distinct_count=10, row_count=10, unique=True,
              is_currency_code=False, max_distinct=30), "identifier"),
        (dict(name="Expense Amount", col_type="number", distinct_count=10, row_count=10, unique=True,
              is_currency_code=False, max_distinct=30), "identifier"),  # unique wins over amount
        (dict(name="Total Cost", col_type="number", distinct_count=8, row_count=10, unique=False,
              is_currency_code=False, max_distinct=5), "amount"),
        (dict(name="Category", col_type="string", distinct_count=3, row_count=10, unique=False,
              is_currency_code=False, max_distinct=30), "category"),
        (dict(name="Employee Name", col_type="string", distinct_count=40, row_count=40, unique=False,
              is_currency_code=False, max_distinct=5), "person"),
        (dict(name="Contact Email", col_type="string", distinct_count=40, row_count=40, unique=False,
              is_currency_code=False, max_distinct=5), "contact"),
        (dict(name="Notes", col_type="string", distinct_count=40, row_count=40, unique=False,
              is_currency_code=False, max_distinct=5), "free_text"),
        (dict(name="Status Code", col_type="string", distinct_count=40, row_count=100, unique=False,
              is_currency_code=False, max_distinct=5), "other"),
    ],
)
def test_classify_semantic_type_first_match_wins(kwargs, expected):
    assert classify_semantic_type(**kwargs) == expected


# ── pandas_profile_columns ───────────────────────────────────────────────────


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame({
        "__source": ["s"] * 8,
        "__row_key": [f"k{i}" for i in range(8)],
        "Employee Name": ["Alice", "Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace"],
        "Amount": [100.0, -50.0, 0.0, 200.0, 300.0, 400.0, 500.0, 600.0],
        "Currency": ["AUD"] * 8,
        "Transaction Date": pd.to_datetime(
            ["2026-01-01", "2026-01-05", "2026-02-01", "2026-02-15",
             "2026-03-01", "2026-03-20", "2026-04-01", "2026-04-15"]
        ),
        "Category": ["Travel", "Travel", "Meals", "Travel", "Meals", "Other", "Travel", "Travel"],
    })


def test_pandas_profile_columns_row_and_null_counts():
    df = _sample_df()
    df.loc[0, "Category"] = None
    result = pandas_profile_columns(df, max_distinct=30, min_count=1)
    assert result["row_count"] == 8
    assert result["null_counts"]["Category"] == 1
    names = [c["name"] for c in result["columns"]]
    assert "__source" not in names and "__row_key" not in names


def test_pandas_profile_columns_numeric_stats():
    df = _sample_df()
    result = pandas_profile_columns(df, max_distinct=30, min_count=1)
    amount = next(c for c in result["columns"] if c["name"] == "Amount")
    assert amount["negative_count"] == 1
    assert amount["zero_count"] == 1
    assert amount["min"] == -50.0
    assert amount["max"] == 600.0


def test_pandas_profile_columns_suppresses_low_count_category_values():
    df = _sample_df()
    result = pandas_profile_columns(df, max_distinct=30, min_count=2)
    category = next(c for c in result["columns"] if c["name"] == "Category")
    values = {v["value"]: v["count"] for v in category["values"]}
    assert values == {"Travel": 5, "Meals": 2}
    assert "Other" not in values
    assert category["suppressed_values"] == 1


def test_pandas_profile_columns_in_period_count_only_when_period_and_timezone_given():
    df = _sample_df()
    no_period = pandas_profile_columns(df, max_distinct=30, min_count=1)
    date_col = next(c for c in no_period["columns"] if c["name"] == "Transaction Date")
    assert "in_period_count" not in date_col

    with_period = pandas_profile_columns(
        df, max_distinct=30, min_count=1,
        audit_period=("2026-01-01", "2026-02-28"), audit_timezone="Australia/Sydney",
    )
    date_col2 = next(c for c in with_period["columns"] if c["name"] == "Transaction Date")
    assert date_col2["in_period_count"] == 4


def test_pandas_profile_columns_currency_code_column():
    df = _sample_df()
    result = pandas_profile_columns(df, max_distinct=30, min_count=1)
    currency = next(c for c in result["columns"] if c["name"] == "Currency")
    assert currency["semantic_type"] == "currency_code"


def test_pandas_distinct_count_composite_key():
    df = pd.DataFrame({
        "A": [1, 1, 2, 2, 3],
        "B": ["x", "x", "y", "z", "x"],
    })
    assert pandas_distinct_count(df, ["A", "B"]) == 4
    assert pandas_distinct_count(df, ["A"]) == 3
    assert pandas_distinct_count(df, []) == 0


# ── §4.3.1 PII classification ────────────────────────────────────────────────


def test_load_repo_pii_flags_finds_skill001_declared_flags():
    flags = load_repo_pii_flags()
    assert ("expense_report", "Employee") in flags
    assert ("expense_report", "Employee ID") in flags


def test_load_repo_pii_flags_missing_dir_returns_empty(tmp_path):
    assert load_repo_pii_flags(tmp_path / "no-such-skills-dir") == set()


def test_classify_pii_rule1_contract_flag_wins_even_with_matching_uc_tag():
    pii, basis, note = classify_pii(
        source_name="expense_report", column_name="Employee",
        semantic_type="category", repo_pii_flags={("expense_report", "Employee")},
        uc_tags={"Employee": ["not-a-pii-tag"]}, pii_tag_names=("pii",),
    )
    assert (pii, basis, note) == (True, "contract", None)


def test_classify_pii_rule2_uc_tag():
    pii, basis, note = classify_pii(
        source_name="s", column_name="col", semantic_type="category",
        repo_pii_flags=set(), uc_tags={"col": ["PII", "other"]}, pii_tag_names=("pii",),
    )
    assert (pii, basis, note) == (True, "uc_tag", None)


def test_classify_pii_rule2_tags_unreadable_falls_through_to_rule3():
    pii, basis, note = classify_pii(
        source_name="s", column_name="Employee Email", semantic_type="contact",
        repo_pii_flags=set(), uc_tags=None, pii_tag_names=("pii",),
    )
    assert pii is True
    assert basis == "heuristic"
    assert note == "tags unreadable"


def test_classify_pii_rule3_heuristic_name_token():
    pii, basis, note = classify_pii(
        source_name="s", column_name="Approver Name", semantic_type="category",
        repo_pii_flags=set(), uc_tags={}, pii_tag_names=(),
    )
    assert (pii, basis) == (True, "heuristic")


def test_classify_pii_rule3_heuristic_semantic_type():
    pii, basis, note = classify_pii(
        source_name="s", column_name="Notes", semantic_type="free_text",
        repo_pii_flags=set(), uc_tags={}, pii_tag_names=(),
    )
    assert (pii, basis) == (True, "heuristic")


def test_classify_pii_no_rule_matches():
    pii, basis, note = classify_pii(
        source_name="s", column_name="Vendor", semantic_type="category",
        repo_pii_flags=set(), uc_tags={}, pii_tag_names=(),
    )
    assert (pii, basis) == (False, None)


def test_mask_pii_column_strips_every_value_revealing_field():
    col = {
        "name": "Amount", "type": "number", "null_count": 0, "distinct_count": 5,
        "unique": True, "semantic_type": "amount", "min": 1, "max": 100,
        "negative_count": 0, "zero_count": 0, "values": [{"value": 1, "count": 1}],
        "suppressed_values": 0,
    }
    masked = mask_pii_column(col, pii_basis="heuristic", pii_basis_note=None)
    assert masked == {
        "name": "Amount", "type": "number", "null_count": 0, "distinct_count": 5,
        "unique": True, "semantic_type": "amount", "pii": True, "pii_basis": "heuristic",
    }
    for field in ("min", "max", "negative_count", "zero_count", "values", "suppressed_values", "in_period_count"):
        assert field not in masked


def test_profile_source_masks_contract_flagged_column_end_to_end():
    class _FakeDS:
        def __init__(self, df):
            self.df = df

        def profile_columns(self, source, *, version, max_distinct, min_count,
                             audit_period=None, audit_timezone=None):
            return pandas_profile_columns(
                self.df, max_distinct=max_distinct, min_count=min_count,
                audit_period=audit_period, audit_timezone=audit_timezone,
            )

    df = pd.DataFrame({
        "Employee": ["Alice Smith"] * 3 + ["Bob Jones"] * 3,
        "Amount": [10, 20, 30, 40, 50, 60],
    })
    result = profile_source(
        _FakeDS(df), "expense_report", version=1, max_distinct=30, min_count=1,
        pii_tag_names=(),
    )
    employee = next(c for c in result["columns"] if c["name"] == "Employee")
    amount = next(c for c in result["columns"] if c["name"] == "Amount")
    assert employee["pii"] is True
    assert "values" not in employee
    assert amount["pii"] is False
    assert amount["values"]


# ── LocalFileDataSource.profile_columns / distinct_count ────────────────────


def _write_csv(tmp_path: Path) -> Path:
    df = pd.DataFrame({
        "Employee ID": [1, 1, 2, 3],
        "Amount": [100, 200, 300, 400],
        "Category": ["Travel", "Travel", "Meals", "Travel"],
    })
    root = tmp_path / "data"
    root.mkdir()
    df.to_csv(root / "claims.csv", index=False)
    return root


def test_local_file_data_source_profile_columns():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = _write_csv(Path(tmp))
        sources = {"claims": {"format": "csv", "file": "claims.csv"}}
        ds = LocalFileDataSource(root_dir=root, sources=sources)
        version = ds.resolve_version("claims")
        result = ds.profile_columns("claims", version=version, max_distinct=30, min_count=1)
        assert result["row_count"] == 4
        names = {c["name"] for c in result["columns"]}
        assert names == {"Employee ID", "Amount", "Category"}


def test_local_file_data_source_distinct_count():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = _write_csv(Path(tmp))
        sources = {"claims": {"format": "csv", "file": "claims.csv"}}
        ds = LocalFileDataSource(root_dir=root, sources=sources)
        version = ds.resolve_version("claims")
        assert ds.distinct_count("claims", version=version, columns=["Employee ID"]) == 3


# ── VolumeUploadAwareDataSource.profile_columns / distinct_count ────────────


class _ExplodingTableSource:
    def profile_columns(self, source, **kwargs):
        raise AssertionError("table_source.profile_columns called for a flat-file-bound source")

    def distinct_count(self, source, **kwargs):
        raise AssertionError("table_source.distinct_count called for a flat-file-bound source")


class _FakePersistence:
    def __init__(self, uploaded_files):
        self._uploaded_files = uploaded_files

    def list_uploaded_files(self, engagement_id=None):
        return self._uploaded_files


class _FakeExportStorage:
    def __init__(self, files):
        self.files = files

    def read(self, path):
        return self.files[path]


def test_volume_upload_aware_data_source_profile_columns_for_flat_file():
    csv_bytes = b"Employee ID,Amount\n1,100\n1,200\n2,300\n"
    sha = hashlib.sha256(csv_bytes).hexdigest()
    upload_row = {
        "upload_id": "UP-1", "filename": "claims.csv",
        "volume_path": "/Volumes/cat/sch/vol/uploads/UP-1/claims.csv",
        "sha256": sha, "status": "Ready",
    }
    ds = VolumeUploadAwareDataSource(
        table_source=_ExplodingTableSource(),
        bindings={"claims": upload_row["volume_path"]},
        contract_sources={},
        persistence=_FakePersistence([upload_row]),
        export_storage=_FakeExportStorage({upload_row["volume_path"]: csv_bytes}),
    )
    result = ds.profile_columns("claims", version=sha, max_distinct=30, min_count=1)
    assert result["row_count"] == 3
    assert ds.distinct_count("claims", version=sha, columns=["Employee ID"]) == 2
    assert ds.column_tags("claims", version=sha) is None


def test_volume_upload_aware_data_source_delegates_for_table_bound_source():
    class _Recording:
        def __init__(self):
            self.called = False

        def profile_columns(self, source, **kwargs):
            self.called = True
            return {"row_count": 0, "null_counts": {}, "columns": []}

        def distinct_count(self, source, **kwargs):
            return 42

    table_source = _Recording()
    ds = VolumeUploadAwareDataSource(
        table_source=table_source, bindings={"expense_report": "cat.sch.expense_report"},
        contract_sources={}, persistence=_FakePersistence([]), export_storage=_FakeExportStorage({}),
    )
    ds.profile_columns("expense_report", version=7, max_distinct=30, min_count=1)
    assert table_source.called
    assert ds.distinct_count("expense_report", version=7, columns=["x"]) == 42


# ── UCTableDataSource.profile_columns / distinct_count / column_tags ───────


def test_uc_table_data_source_profile_columns_delegates_to_read_population():
    ds, conn = _uc_ds(_std_handlers(row_count=4))
    result = ds.profile_columns("expense_report", version=7, max_distinct=30, min_count=1)
    assert result["row_count"] == 4
    names = {c["name"] for c in result["columns"]}
    assert names == {"Employee", "Amount"}


def test_uc_table_data_source_distinct_count():
    ds, conn = _uc_ds(_std_handlers(row_count=4))
    # _std_handlers' fake data ignores the requested `columns` projection and
    # always returns both raw columns -- distinct_count still only considers
    # the columns it was asked about.
    n = ds.distinct_count("expense_report", version=7, columns=["Amount"])
    assert n == 4


def test_uc_table_data_source_column_tags_returns_mapping():
    handlers = {
        "information_schema.column_tags": lambda sql, p: (
            ["column_name", "tag_name"], [("Employee", "PII"), ("Employee", "sensitive"), ("Amount", "other")],
        ),
    }
    ds, _ = _uc_ds(handlers, bindings={"expense_report": "cat.sch.expense_report"})
    tags = ds.column_tags("expense_report", version=7)
    assert set(tags["Employee"]) == {"PII", "sensitive"}
    assert tags["Amount"] == ["other"]


def test_uc_table_data_source_column_tags_returns_none_on_permission_denied():
    from databricks.sdk.errors import PermissionDenied

    def _raise(sql, p):
        raise PermissionDenied("nope")

    handlers = {"information_schema.column_tags": _raise}
    ds, _ = _uc_ds(handlers, bindings={"expense_report": "cat.sch.expense_report"})
    assert ds.column_tags("expense_report", version=7) is None
