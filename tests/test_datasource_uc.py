from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from orchestrator.adapters.datasource_uc import (
    UCSourceError,
    UCTableDataSource,
    _build_where,
    _normalise_datetime_dtypes,
    _quote_ident,
)
from orchestrator.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
SYNTHETIC_DATA_DIR = REPO_ROOT / "synthetic_data"


# ── fake connection harness (mirrors tests/test_delta_sql.py's FakeConnection) ──


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self._rows = []
        self._pos = 0

    def execute(self, sql_text, params=None):
        self.conn.calls.append((sql_text, dict(params or {})))
        handler = self.conn.handlers.get(self._match(sql_text))
        if handler is None:
            raise AssertionError(f"no handler registered for statement: {sql_text}")
        columns, rows = handler(sql_text, params or {})
        self.description = [(c,) for c in columns]
        self._rows = list(rows)
        self._pos = 0
        return self

    def _match(self, sql_text):
        for key in self.conn.handlers:
            if key in sql_text:
                return key
        return None

    def fetchone(self):
        if self._pos < len(self._rows):
            row = self._rows[self._pos]
            self._pos += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rows

    def fetchall_arrow(self):
        columns = [d[0] for d in self.description]
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        data = {col: [r[i] for r in rows] for i, col in enumerate(columns)}
        return pa.table(data) if columns else pa.table({})

    def close(self):
        pass


class FakeConnection:
    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


def _settings():
    return Settings(host="https://x.cloud.databricks.com", warehouse_http_path="/sql/1.0/warehouses/abc")


def _ds(handlers, **kwargs):
    conn = FakeConnection(handlers)
    bindings = kwargs.pop("bindings", {"expense_report": "cat.sch.expense_report"})
    return UCTableDataSource(_settings(), bindings, connection_factory=lambda: conn, **kwargs), conn


def _schema_handler(columns):
    def handler(sql_text, params):
        return (columns, [])

    return handler


# ── identifier / injection safety ───────────────────────────────────────────


def test_quote_ident_escapes_backticks():
    assert _quote_ident("a`b") == "`a``b`"


def test_read_population_rejects_non_identifier_in_binding():
    ds, _ = _ds(
        {},
        bindings={"evil": "cat.sch.a; DROP TABLE x;--"},
    )
    with pytest.raises(UCSourceError, match="not a plain identifier"):
        ds.resolve_version("evil")


def test_read_population_rejects_wrong_shaped_fqn():
    ds, _ = _ds({}, bindings={"evil": "cat.sch"})
    with pytest.raises(UCSourceError, match="catalog.schema.table"):
        ds.resolve_version("evil")


def test_unbound_source_raises():
    ds, _ = _ds({})
    with pytest.raises(UCSourceError, match="no table bound"):
        ds.resolve_version("nope")


# ── resolve_version ──────────────────────────────────────────────────────────


def test_resolve_version_returns_latest_from_describe_history():
    handlers = {
        "DESCRIBE HISTORY `cat`.`sch`.`expense_report` LIMIT 1": lambda sql, p: (
            ["version", "timestamp", "operation"],
            [(7, "2026-01-01", "CREATE OR REPLACE TABLE AS SELECT")],
        ),
    }
    ds, conn = _ds(handlers)
    version = ds.resolve_version("expense_report")
    assert version == "7"
    assert conn.calls[0][0] == "DESCRIBE HISTORY `cat`.`sch`.`expense_report` LIMIT 1"


def test_resolve_version_no_history_rows_raises():
    handlers = {"DESCRIBE HISTORY": lambda sql, p: (["version"], [])}
    ds, _ = _ds(handlers)
    with pytest.raises(UCSourceError, match="no rows"):
        ds.resolve_version("expense_report")


def test_resolve_source_versions_takes_source_names():
    handlers = {"DESCRIBE HISTORY": lambda sql, p: (["version"], [(3,)])}
    ds, _ = _ds(handlers)
    assert ds.resolve_source_versions(["expense_report"]) == {"expense_report": "3"}


# ── read_population ──────────────────────────────────────────────────────────


def _std_handlers(row_count=2, raw_columns=("Employee", "Amount")):
    all_cols = ["_source_row", *raw_columns]

    def limit0(sql, p):
        return (all_cols, [])

    def count(sql, p):
        return (["n"], [(row_count,)])

    def data(sql, p):
        rows = [(i + 1, f"emp{i}", float(i)) for i in range(row_count)]
        return (all_cols, rows)

    return {
        "LIMIT 0": limit0,
        "SELECT count(*)": count,
        "ORDER BY `_source_row`": data,
    }


def test_read_population_pins_version_as_of():
    ds, conn = _ds(_std_handlers())
    ds.read_population("expense_report", version=7)
    select_calls = [c for c in conn.calls if c[0].startswith("SELECT `_source_row`")]
    assert len(select_calls) == 1
    assert "VERSION AS OF 7" in select_calls[0][0]
    assert "ORDER BY `_source_row`" in select_calls[0][0]


def test_read_population_returns_source_and_row_key_columns_first():
    ds, _ = _ds(_std_handlers(row_count=3))
    df = ds.read_population("expense_report", version=7)
    assert list(df.columns[:2]) == ["__source", "__row_key"]
    assert (df["__source"] == "expense_report").all()
    assert list(df["__row_key"]) == [
        "cat.sch.expense_report@v7:1",
        "cat.sch.expense_report@v7:2",
        "cat.sch.expense_report@v7:3",
    ]
    assert "_source_row" not in df.columns


def test_read_population_no_source_row_column_raises():
    handlers = {
        "LIMIT 0": lambda sql, p: (["Employee", "Amount"], []),
    }
    ds, _ = _ds(handlers)
    with pytest.raises(UCSourceError, match="_source_row"):
        ds.read_population("expense_report", version=7)


def test_read_population_strips_header_trim_style_column():
    all_cols = ["_source_row", "Currency ", "Amount"]

    def limit0(sql, p):
        return (all_cols, [])

    def count(sql, p):
        return (["n"], [(1,)])

    def data(sql, p):
        # the real driver's cursor.description reflects post-alias names (SELECT
        # `Currency ` AS `Currency`), unlike DESCRIBE/LIMIT 0 which sees the raw ones
        return (["_source_row", "Currency", "Amount"], [(1, "AUD", 10.0)])

    ds, conn = _ds({"LIMIT 0": limit0, "SELECT count(*)": count, "ORDER BY `_source_row`": data})
    df = ds.read_population("expense_report", version=1)
    assert "Currency" in df.columns
    assert "Currency " not in df.columns
    select_call = [c for c in conn.calls if c[0].startswith("SELECT `_source_row`")][0][0]
    assert "`Currency ` AS `Currency`" in select_call


def test_read_population_ambiguous_stripped_columns_raises():
    all_cols = ["_source_row", "Amount ", "Amount"]
    handlers = {"LIMIT 0": lambda sql, p: (all_cols, [])}
    ds, _ = _ds(handlers)
    with pytest.raises(UCSourceError, match="ambiguous"):
        ds.read_population("expense_report", version=1)


def test_read_population_explicit_columns_selects_subset():
    all_cols = ["_source_row", "Employee", "Amount", "Vendor"]

    def limit0(sql, p):
        return (all_cols, [])

    def count(sql, p):
        return (["n"], [(1,)])

    def data(sql, p):
        return (["_source_row", "Employee"], [(1, "emp0")])

    ds, conn = _ds({"LIMIT 0": limit0, "SELECT count(*)": count, "ORDER BY `_source_row`": data})
    df = ds.read_population("expense_report", version=1, columns=["Employee"])
    assert list(df.columns) == ["__source", "__row_key", "Employee"]
    select_call = [c for c in conn.calls if c[0].startswith("SELECT `_source_row`")][0][0]
    assert "`Amount`" not in select_call
    assert "`Vendor`" not in select_call


def test_read_population_missing_requested_column_raises():
    handlers = {"LIMIT 0": lambda sql, p: (["_source_row", "Employee"], [])}
    ds, _ = _ds(handlers)
    with pytest.raises(UCSourceError, match="not found"):
        ds.read_population("expense_report", version=1, columns=["DoesNotExist"])


def test_read_population_version_must_be_int_like():
    ds, _ = _ds({})
    with pytest.raises(UCSourceError, match="integer Delta version"):
        ds.read_population("expense_report", version="not-a-number")


# ── memory guard (never samples) ─────────────────────────────────────────────


def test_memory_guard_raises_and_never_issues_the_data_query():
    all_cols = ["_source_row", "Employee", "Amount"]

    def limit0(sql, p):
        return (all_cols, [])

    def count(sql, p):
        return (["n"], [(1_000_000,)])

    ds, conn = _ds({"LIMIT 0": limit0, "SELECT count(*)": count}, max_cells=10)
    with pytest.raises(UCSourceError, match="DBX_MAX_CELLS"):
        ds.read_population("expense_report", version=1)
    assert not [c for c in conn.calls if "ORDER BY" in c[0]]


def test_max_cells_from_env(monkeypatch):
    monkeypatch.setenv("DBX_MAX_CELLS", "5")
    ds, _ = _ds({}, )
    assert ds._max_cells == 5


def test_max_cells_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("DBX_MAX_CELLS", raising=False)
    ds, _ = _ds({})
    assert ds._max_cells == 20_000_000


# ── filter pushdown ──────────────────────────────────────────────────────────


def test_build_where_equality():
    sql, params = _build_where({"Employee ID": 42}, {})
    assert sql == "`Employee ID` = :f0"
    assert params == {"f0": 42}


def test_build_where_range():
    sql, params = _build_where({"Amount": {"gte": 10, "lte": 20}}, {})
    assert "`Amount` >= :f0_gte" in sql
    assert "`Amount` <= :f0_lte" in sql
    assert params == {"f0_gte": 10, "f0_lte": 20}


def test_build_where_in():
    sql, params = _build_where({"Status": {"in": ["A", "B"]}}, {})
    assert sql == "`Status` IN (:f0_0, :f0_1)"
    assert params == {"f0_0": "A", "f0_1": "B"}


def test_build_where_uses_logical_to_actual_mapping():
    sql, _ = _build_where({"Currency": 1}, {"Currency": "Currency "})
    assert sql == "`Currency ` = :f0"


def test_build_where_rejects_unsupported_spec():
    with pytest.raises(UCSourceError, match="unsupported filter spec"):
        _build_where({"Amount": {"nope": 1}}, {})


def test_build_where_empty_filters():
    assert _build_where(None, {}) == ("", {})
    assert _build_where({}, {}) == ("", {})


# ── dtype normalisation ──────────────────────────────────────────────────────


def test_normalise_datetime_dtypes_strips_timezone_and_unit():
    df = pd.DataFrame({"d": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"])})
    df["d"] = df["d"].dt.tz_localize("UTC") if df["d"].dt.tz is None else df["d"]
    out = _normalise_datetime_dtypes(df.copy())
    assert str(out["d"].dtype) == "datetime64[us]"


def test_normalise_datetime_dtypes_leaves_non_datetime_alone():
    df = pd.DataFrame({"n": [1, 2, 3]})
    out = _normalise_datetime_dtypes(df.copy())
    assert out["n"].tolist() == [1, 2, 3]


# ── list_tables() / WorkspaceClient construction ────────────────────────────
# CLAUDE.md §11 Batch-1b item 1: Config has no `credentials_strategy` attribute
# on the installed SDK (0.140.0; it's the private `_credentials_strategy`), so
# `WorkspaceClient(host=..., credentials_strategy=cfg.credentials_strategy)`
# raised AttributeError before ever reaching the workspace, breaking UC table
# discovery for the run-setup page entirely.


class _FakeColumn:
    def __init__(self, name):
        self.name = name


class _FakeTable:
    def __init__(self, name, comment, columns):
        self.name = name
        self.comment = comment
        self.columns = [_FakeColumn(c) for c in columns]


class _FakeNamed:
    def __init__(self, name):
        self.name = name


class _FakeWorkspaceClient:
    """Records constructor kwargs and serves a small fixed catalog tree,
    including one restricted catalog and one restricted schema, so list_tables()
    is exercised the same way it is against a real workspace (CLAUDE.md §8 P5:
    'no-access catalogs surface as Restricted')."""

    last_init_kwargs: dict | None = None

    def __init__(self, **kwargs):
        _FakeWorkspaceClient.last_init_kwargs = kwargs
        self.catalogs = self._Catalogs()
        self.schemas = self._Schemas()
        self.tables = self._Tables()

    class _Catalogs:
        def list(self):
            return [_FakeNamed("orchestrationsuite"), _FakeNamed("locked_catalog")]

    class _Schemas:
        def list(self, catalog_name):
            from databricks.sdk.errors import DatabricksError

            if catalog_name == "locked_catalog":
                raise DatabricksError("PERMISSION_DENIED")
            return [_FakeNamed("tne_source"), _FakeNamed("locked_schema")]

    class _Tables:
        def list(self, catalog_name, schema_name):
            from databricks.sdk.errors import DatabricksError

            if schema_name == "locked_schema":
                raise DatabricksError("PERMISSION_DENIED")
            return [
                _FakeTable("expense_report", "T&E expense claims", ["Employee ID", "Expense Amount"]),
                _FakeTable("booking_detail", None, ["Booking Type"]),
            ]


def test_list_tables_constructs_workspace_client_with_only_host(monkeypatch):
    """Guards against the AttributeError regression directly: whatever kwargs
    list_tables() uses to build a WorkspaceClient, `credentials_strategy` must
    not be one of them -- Config on the installed SDK has no such public
    attribute, and passing it either raises or silently breaks auth."""
    import databricks.sdk as sdk_module

    _FakeWorkspaceClient.last_init_kwargs = None
    monkeypatch.setattr(sdk_module, "WorkspaceClient", _FakeWorkspaceClient)

    ds, _ = _ds({}, bindings={})
    ds.list_tables()

    assert _FakeWorkspaceClient.last_init_kwargs is not None
    assert "credentials_strategy" not in _FakeWorkspaceClient.last_init_kwargs
    assert _FakeWorkspaceClient.last_init_kwargs == {"host": "https://x.cloud.databricks.com"}


def test_list_tables_returns_expected_shape_including_restricted_catalog():
    ds, _ = _ds({}, bindings={}, workspace_client_factory=_FakeWorkspaceClient)

    results = ds.list_tables()

    restricted = [r for r in results if r.get("restricted")]
    assert {"orchestrationsuite.locked_schema", "locked_catalog"} <= {r["fqn"] for r in restricted}

    found = {r["fqn"]: r for r in results if not r.get("restricted")}
    assert "orchestrationsuite.tne_source.expense_report" in found
    row = found["orchestrationsuite.tne_source.expense_report"]
    assert row == {
        "fqn": "orchestrationsuite.tne_source.expense_report",
        "catalog": "orchestrationsuite",
        "schema": "tne_source",
        "table": "expense_report",
        "comment": "T&E expense claims",
        "columns": ["Employee ID", "Expense Amount"],
    }


def test_list_tables_caches_workspace_client_across_calls():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _FakeWorkspaceClient()

    ds, _ = _ds({}, bindings={}, workspace_client_factory=factory)
    ds.list_tables()
    ds.list_tables()

    assert calls["n"] == 1


# ── live equality tests: UC-backed read vs LocalFileDataSource read ─────────
# Skipped unless RUN_DELTA_TESTS=1 -- prove that pointing the Skill at UC gives
# byte-identical population data to reading the original files (CLAUDE.md §2.1).

pytestmark_live = pytest.mark.skipif(
    os.environ.get("RUN_DELTA_TESTS") != "1",
    reason="RUN_DELTA_TESTS not set — live workspace is unavailable (CLAUDE.md §11)",
)


def _live_bindings():
    import yaml

    catalog = os.environ["DBX_CATALOG"]
    schema = os.environ.get("DBX_TNE_SCHEMA", "tne_source")
    with open(SKILL_DIR / "contract.yaml") as f:
        contract = yaml.safe_load(f)
    return {name: f"{catalog}.{schema}.{name}" for name in contract["sources"]}, contract["sources"]


@pytest.mark.skipif(os.environ.get("RUN_DELTA_TESTS") != "1", reason="RUN_DELTA_TESTS not set")
@pytest.mark.parametrize(
    "source_name",
    [
        "expense_report",
        "attendee_validity",
        "missing_receipt",
        "approval_aging",
        "travel_requests_no_expense",
        "travel_request_segment",
        "booking_detail",
        "per_diem_rates",
    ],
)
def test_uc_read_matches_local_file_read(source_name):
    from orchestrator.config import load_settings
    from orchestrator.contract import LocalFileDataSource

    bindings, sources_cfg = _live_bindings()
    ds = UCTableDataSource(load_settings(), bindings)
    try:
        uc_version = ds.resolve_version(source_name)
        uc_df = ds.read_population(source_name, version=uc_version)

        local = LocalFileDataSource(root_dir=SYNTHETIC_DATA_DIR, sources=sources_cfg)
        local_version = local.resolve_version(source_name)
        local_df = local.read_population(source_name, version=local_version)

        uc_data = uc_df.drop(columns=["__source", "__row_key"]).reset_index(drop=True)
        local_data = local_df.drop(columns=["__source", "__row_key"]).reset_index(drop=True)

        assert list(uc_data.columns) == list(local_data.columns), (
            f"{source_name}: column set/order differs\n"
            f"  uc:    {list(uc_data.columns)}\n"
            f"  local: {list(local_data.columns)}"
        )
        pd.testing.assert_frame_equal(uc_data, local_data, check_dtype=True, check_like=False)
    finally:
        ds.close()
