from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from orchestrator.adapters.datasource_uc import (
    UCConnectionPoolExhausted,
    UCSourceError,
    UCTableDataSource,
    _build_where,
    _normalise_datetime_dtypes,
    _quote_ident,
    _quoted_fqn,
    _UCConnectionPool,
    clear_table_listing_cache,
)
from orchestrator.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"


@pytest.fixture(autouse=True)
def _reset_table_listing_cache():
    # Independent review 2026-09-24 item 6: list_tables()'s cache is
    # deliberately process-wide/cross-instance (see its own module
    # docstring) -- reset between tests so one test's fake UC listing can
    # never leak into another's under the same (catalog=None, schema=None)
    # cache key.
    clear_table_listing_cache()
    yield
    clear_table_listing_cache()
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


# ── resolve_source_versions concurrency (P3/P4 perf gap review 2026-09-25) ──


class _ConcurrencyTracker:
    """Records how many `_SleepingConnection.cursor().execute()` calls were
    in flight at once, so a test can assert real concurrency happened (not
    just that the wall-clock time was short by coincidence)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = 0
        self.max_seen = 0

    def enter(self):
        with self._lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)

    def exit(self):
        with self._lock:
            self.current -= 1


class _SleepingCursor:
    def __init__(self, version_by_quoted_fqn, sleep_s, tracker):
        self._version_by_quoted_fqn = version_by_quoted_fqn
        self._sleep_s = sleep_s
        self._tracker = tracker
        self.description = []
        self._rows = []
        self._pos = 0

    def execute(self, sql_text, params=None):
        self._tracker.enter()
        try:
            time.sleep(self._sleep_s)
            qfqn = sql_text[len("DESCRIBE HISTORY ") : -len(" LIMIT 1")]
            version = self._version_by_quoted_fqn[qfqn]
            self.description = [("version",)]
            self._rows = [(version,)]
            self._pos = 0
        finally:
            self._tracker.exit()

    def fetchone(self):
        if self._pos < len(self._rows):
            row = self._rows[self._pos]
            self._pos += 1
            return row
        return None


class _SleepingConnection:
    """A fresh instance is handed back on every `connection_factory()` call --
    the shape `resolve_source_versions` relies on for "each resolution on its
    own connection" (unlike `FakeConnection` above, which `_ds()` always
    reuses via a shared closure)."""

    def __init__(self, version_by_quoted_fqn, sleep_s, tracker):
        self._version_by_quoted_fqn = version_by_quoted_fqn
        self._sleep_s = sleep_s
        self._tracker = tracker
        self.closed = False

    def cursor(self):
        return _SleepingCursor(self._version_by_quoted_fqn, self._sleep_s, self._tracker)

    def close(self):
        self.closed = True


def test_resolve_source_versions_runs_concurrently_wall_time_is_max_not_sum():
    n = 8
    sleep_s = 0.05
    bindings = {f"s{i}": f"cat.sch.t{i}" for i in range(n)}
    version_by_quoted_fqn = {_quoted_fqn(fqn): str(i) for i, fqn in enumerate(bindings.values())}
    tracker = _ConcurrencyTracker()

    def factory():
        return _SleepingConnection(version_by_quoted_fqn, sleep_s, tracker)

    ds = UCTableDataSource(_settings(), bindings, connection_factory=factory)

    start = time.monotonic()
    result = ds.resolve_source_versions(list(bindings))
    elapsed = time.monotonic() - start

    assert result == {f"s{i}": str(i) for i in range(n)}
    sequential_time = n * sleep_s
    assert elapsed < sequential_time / 2, (
        f"resolve_source_versions took {elapsed:.3f}s for {n} sources at {sleep_s}s each "
        f"({sequential_time:.3f}s sequential) -- looks serialised, not concurrent"
    )
    assert tracker.max_seen > 1, "no two DESCRIBE HISTORY calls ever overlapped"


def test_resolve_source_versions_respects_max_connections_bound():
    n = 8
    sleep_s = 0.05
    cap = 2
    bindings = {f"s{i}": f"cat.sch.t{i}" for i in range(n)}
    version_by_quoted_fqn = {_quoted_fqn(fqn): str(i) for i, fqn in enumerate(bindings.values())}
    tracker = _ConcurrencyTracker()

    def factory():
        return _SleepingConnection(version_by_quoted_fqn, sleep_s, tracker)

    settings = Settings(
        host="https://x.cloud.databricks.com", warehouse_http_path="/sql/1.0/warehouses/abc",
        max_connections=cap,
    )
    ds = UCTableDataSource(settings, bindings, connection_factory=factory)

    ds.resolve_source_versions(list(bindings))

    assert tracker.max_seen <= cap


def test_resolve_source_versions_assembles_deterministically_by_source_name():
    bindings = {f"s{i}": f"cat.sch.t{i}" for i in range(5)}
    handlers = {}
    for i, fqn in enumerate(bindings.values()):
        def handler(sql, p, _v=i):
            return (["version"], [(_v,)])

        handlers[_quoted_fqn(fqn)] = handler

    def factory():
        return FakeConnection(handlers)

    ds = UCTableDataSource(_settings(), bindings, connection_factory=factory)
    result = ds.resolve_source_versions(list(bindings))

    assert result == {f"s{i}": str(i) for i in range(5)}
    assert list(result) == list(bindings), "result order must match the caller's input order"


def test_resolve_source_versions_one_failure_fails_the_whole_batch():
    bindings = {"a": "cat.sch.a", "b": "cat.sch.b", "c": "cat.sch.c"}

    def failing_handler(sql, p):
        raise RuntimeError("boom on b")

    def ok_handler(sql, p):
        return (["version"], [(1,)])

    handlers = {
        _quoted_fqn("cat.sch.a"): ok_handler,
        _quoted_fqn("cat.sch.b"): failing_handler,
        _quoted_fqn("cat.sch.c"): ok_handler,
    }

    def factory():
        return FakeConnection(handlers)

    ds = UCTableDataSource(_settings(), bindings, connection_factory=factory)
    with pytest.raises(RuntimeError, match="boom on b"):
        ds.resolve_source_versions(list(bindings))


def test_resolve_source_versions_checks_connections_back_into_the_pool_not_closed():
    """Warm-pool follow-up (P3/P4 perf gap review 2026-09-25): a resolve's
    connections must be returned to the pool for reuse (checkin), never
    closed outright -- closing them on every call is exactly the cold-open
    cost this redesign removes. Nothing is left OUTSIDE the pool's
    bookkeeping either: every connection opened ends up idle in the pool,
    ready for the next checkout, and closing the (privately-owned) pool
    afterwards closes all of them -- proving none leaked."""
    n = 4
    opened: list = []

    class _TrackedConnection(FakeConnection):
        def close(self):
            opened.remove(self)
            super().close()

    handlers = {_quoted_fqn(f"cat.sch.t{i}"): (lambda sql, p, _v=i: (["version"], [(_v,)])) for i in range(n)}
    bindings = {f"s{i}": f"cat.sch.t{i}" for i in range(n)}

    def factory():
        conn = _TrackedConnection(handlers)
        opened.append(conn)
        return conn

    ds = UCTableDataSource(_settings(), bindings, connection_factory=factory)
    ds.resolve_source_versions(list(bindings))

    # However many connections the pool actually needed to open (fast fake
    # queries may let it reuse one connection across several sequential
    # checkouts, same as a real pool would under no real overlap) -- none of
    # them were closed, and every one is idle in the pool, ready for reuse.
    assert len(opened) >= 1
    assert ds._pool.size() == len(opened)
    assert len(ds._pool._idle) == len(opened), "every connection must be idle in the pool, not held by an instance"

    ds.close()
    assert opened == [], "closing the (privately-owned) pool must close every connection it holds"


def test_resolve_source_versions_single_source_uses_the_existing_single_connection_path():
    """cap collapses to 1 for a single source -- no ThreadPoolExecutor, no
    extra connection opened; goes through the same `_execute`/shared-`_conn`
    path `resolve_version` always used, so a Skill with one source sees no
    behaviour change at all."""
    handlers = {"DESCRIBE HISTORY": lambda sql, p: (["version"], [(9,)])}
    ds, conn = _ds(handlers)
    result = ds.resolve_source_versions(["expense_report"])
    assert result == {"expense_report": "9"}
    assert ds._conn is conn, "singular resolve must still use the instance's shared lazy connection"


# ── persistent/shared connection pool (P3/P4 perf gap review 2026-09-25, ──
# warm-pool follow-up): a caller holding a process-lifetime _UCConnectionPool
# (AppContext.uc_pool) shares it across every UCTableDataSource instance it
# builds, so a cold connection open happens at most once per process, not
# once per run/node.


def _counting_factory(opened: list):
    handlers = {"DESCRIBE HISTORY": lambda sql, p: (["version"], [(1,)])}

    class _TrackedConnection(FakeConnection):
        def close(self):
            opened.remove(self)
            super().close()

    def factory():
        conn = _TrackedConnection(handlers)
        opened.append(conn)
        return conn

    return factory


def test_shared_pool_is_warm_on_a_second_uctabledatasource_instance():
    """The core new capability: TWO separate instances (exactly the shape
    `ctx.data_source_factory(...)` builds on every call -- once for
    start_audit_run's own resolve, again later for a node's read) sharing
    ONE pool must open a connection only ONCE across both, not once each."""
    opened: list = []
    pool = _UCConnectionPool(_counting_factory(opened), max_size=6, checkout_timeout_s=5.0)

    ds1 = UCTableDataSource(_settings(), {"expense_report": "cat.sch.expense_report"}, pool=pool)
    assert ds1.resolve_version("expense_report") == "1"
    assert len(opened) == 1
    ds1.close()  # returns the connection to the SHARED pool -- must not close it

    assert pool.size() == 1  # still open and tracked by the pool, not discarded

    ds2 = UCTableDataSource(_settings(), {"expense_report": "cat.sch.expense_report"}, pool=pool)
    assert ds2.resolve_version("expense_report") == "1"
    ds2.close()

    assert len(opened) == 1, "a second instance sharing the pool must reuse the warm connection, not open a new one"


def test_close_with_a_shared_pool_checks_in_without_closing():
    opened: list = []
    pool = _UCConnectionPool(_counting_factory(opened), max_size=6, checkout_timeout_s=5.0)
    ds = UCTableDataSource(_settings(), {"expense_report": "cat.sch.expense_report"}, pool=pool)
    ds.resolve_version("expense_report")
    ds.close()

    assert len(opened) == 1
    assert pool._idle == [opened[0]], "the connection must be idle in the SHARED pool, ready for reuse"
    # A shared pool is never torn down by one instance's close() -- it
    # outlives every UCTableDataSource that borrows from it.
    assert pool._closed is False


def test_close_with_no_pool_given_closes_the_private_pool_it_owns():
    opened: list = []
    ds = UCTableDataSource(
        _settings(), {"expense_report": "cat.sch.expense_report"}, connection_factory=_counting_factory(opened)
    )
    ds.resolve_version("expense_report")
    assert len(opened) == 1

    ds.close()

    assert opened == [], "closing an instance with no pool= given must close the private pool it owns"


def test_broken_connection_is_dropped_from_a_shared_pool_never_reused():
    calls = {"n": 0}

    class _FlakyConnection:
        def __init__(self, index):
            self.index = index
            self.closed = False

        def cursor(self):
            return self

        def execute(self, sql, params=None):
            if self.index == 1:
                raise RuntimeError("connection died mid-query")
            self.description = [("version",)]
            self._rows = [(7,)]

        def fetchone(self):
            row = self._rows[0]
            self._rows = []
            return row

        def close(self):
            self.closed = True

    def factory():
        calls["n"] += 1
        return _FlakyConnection(calls["n"])

    pool = _UCConnectionPool(factory, max_size=6, checkout_timeout_s=5.0)
    ds = UCTableDataSource(_settings(), {"expense_report": "cat.sch.expense_report"}, pool=pool)

    with pytest.raises(RuntimeError, match="connection died mid-query"):
        ds.resolve_version("expense_report")
    assert ds._conn is None, "a failed connection must be dropped, not cached for reuse by this instance"
    assert pool.size() == 0, "a broken connection must never sit in the pool for another instance to inherit"

    # The SAME shared pool, used again, opens a fresh (second, working)
    # connection rather than ever handing back the broken first one.
    ds2 = UCTableDataSource(_settings(), {"expense_report": "cat.sch.expense_report"}, pool=pool)
    assert ds2.resolve_version("expense_report") == "7"
    assert calls["n"] == 2


def test_uc_pool_checkout_times_out_loudly_when_exhausted():
    """CLAUDE.md NN14: an exhausted pool that never frees up fails loudly,
    never hangs forever or silently proceeds with no connection."""

    def factory():
        return FakeConnection({})

    pool = _UCConnectionPool(factory, max_size=1, checkout_timeout_s=0.2)
    held = pool.checkout()
    try:
        start = time.monotonic()
        with pytest.raises(UCConnectionPoolExhausted):
            pool.checkout()
        elapsed = time.monotonic() - start
        assert 0.15 <= elapsed < 2.0, f"checkout timeout took {elapsed:.2f}s, expected ~0.2s"
    finally:
        pool.checkin(held)


def test_uc_pool_concurrent_opens_are_not_serialised_under_the_lock():
    """Mirrors persistence_delta's own _ConnectionPool test: the slow
    factory() call must run OUTSIDE the pool's lock, so N concurrent
    checkouts against a cold pool open in parallel, not one at a time."""
    open_delay_s = 0.2
    n_concurrent = 4

    def factory():
        time.sleep(open_delay_s)
        return FakeConnection({})

    pool = _UCConnectionPool(factory, max_size=n_concurrent, checkout_timeout_s=5.0)

    def _checkout_and_return():
        conn = pool.checkout()
        pool.checkin(conn)

    threads = [threading.Thread(target=_checkout_and_return) for _ in range(n_concurrent)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.monotonic() - start

    assert elapsed < open_delay_s * (n_concurrent / 2), (
        f"{n_concurrent} concurrent checkouts against a cold pool took {elapsed:.3f}s for a "
        f"{open_delay_s}s-per-open factory -- looks like opens are serialised, not parallel"
    )
    assert pool.size() == n_concurrent


# ── get_row_count() / get_classification() (data_asset_card metadata) ───────
# CLAUDE.md §5 UI item 3: real per-table queries, run only for cards actually
# rendered, cached by (fqn, Delta version) so re-rendering the same card never
# re-counts a table it already counted.


def test_get_row_count_queries_and_caches_by_fqn_and_version(monkeypatch):
    from orchestrator.adapters import datasource_uc as duc

    monkeypatch.setattr(duc, "_ROW_COUNT_CACHE", {})
    handlers = {
        "DESCRIBE HISTORY": lambda sql, p: (["version"], [(5,)]),
        "SELECT COUNT(*)": lambda sql, p: (["count(1)"], [(92798,)]),
    }
    ds, conn = _ds(handlers, bindings={})

    count = ds.get_row_count("cat.sch.expense_report")
    assert count == 92798
    assert "VERSION AS OF 5" in conn.calls[-1][0]

    count_again = ds.get_row_count("cat.sch.expense_report")
    assert count_again == 92798
    count_queries = [c for c in conn.calls if "SELECT COUNT" in c[0]]
    assert len(count_queries) == 1, "the second call must hit the cache, not re-run COUNT(*)"


def test_get_classification_returns_matching_tag_case_insensitive():
    handlers = {
        "information_schema.table_tags": lambda sql, p: (
            ["tag_name", "tag_value"], [("Classification", "PII"), ("owner_team", "IA")],
        ),
    }
    ds, _ = _ds(handlers, bindings={})
    assert ds.get_classification("cat.sch.expense_report") == "PII"


def test_get_classification_returns_none_when_no_tag_present():
    handlers = {
        "information_schema.table_tags": lambda sql, p: (["tag_name", "tag_value"], []),
    }
    ds, _ = _ds(handlers, bindings={})
    assert ds.get_classification("cat.sch.expense_report") is None


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
    def __init__(self, name, comment, columns, owner=None, updated_at=None):
        self.name = name
        self.comment = comment
        self.columns = [_FakeColumn(c) for c in columns]
        self.owner = owner
        self.updated_at = updated_at


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
            # Independent review 2026-09-24 item 6: the real 403/
            # PERMISSION_DENIED the SDK raises is the specific PermissionDenied
            # subclass, not the bare base DatabricksError -- list_tables() now
            # only marks "restricted" for that specific class, so this fake
            # must raise the same class a real workspace would.
            from databricks.sdk.errors import PermissionDenied

            if catalog_name == "locked_catalog":
                raise PermissionDenied("PERMISSION_DENIED")
            return [_FakeNamed("tne_source"), _FakeNamed("locked_schema")]

    class _Tables:
        def list(self, catalog_name, schema_name):
            from databricks.sdk.errors import PermissionDenied

            if schema_name == "locked_schema":
                raise PermissionDenied("PERMISSION_DENIED")
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


def test_list_tables_includes_owner_and_last_refreshed_when_uc_reports_them():
    """CLAUDE.md §5 UI item 3: data_asset_card must never render the literal
    None for Owner/Refreshed. UCTableDataSource itself must not invent them
    either -- a key is only added to the result dict when the SDK actually
    returned a value, so a table UC has no owner/updated_at for is simply
    left without that key (unchanged from the exact-equality test above)."""

    class _TablesWithMetadata:
        def list(self, catalog_name, schema_name):
            return [
                _FakeTable("expense_report", "T&E expense claims", ["Employee ID"],
                           owner="alex@example.com", updated_at=1758585600000),
                _FakeTable("no_metadata", None, []),
            ]

    class _WSClientWithMetadata(_FakeWorkspaceClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.tables = _TablesWithMetadata()

    ds, _ = _ds({}, bindings={}, workspace_client_factory=_WSClientWithMetadata)
    results = {r["table"]: r for r in ds.list_tables() if not r.get("restricted")}

    assert results["expense_report"]["owner"] == "alex@example.com"
    assert results["expense_report"]["last_refreshed"] == "2025-09-23"
    assert "owner" not in results["no_metadata"]
    assert "last_refreshed" not in results["no_metadata"]


def test_list_tables_caches_workspace_client_across_calls():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _FakeWorkspaceClient()

    ds, _ = _ds({}, bindings={}, workspace_client_factory=factory)
    ds.list_tables()
    ds.list_tables()

    assert calls["n"] == 1


# ── independent review 2026-09-24 item 6 ─────────────────────────────────────


class _CountingWorkspaceClient(_FakeWorkspaceClient):
    """Counts how many times the underlying SDK calls were actually made,
    so caching/no-caching can be told apart directly."""

    call_count = 0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    class _Catalogs(_FakeWorkspaceClient._Catalogs):
        def list(self):
            _CountingWorkspaceClient.call_count += 1
            return super().list()


def test_list_tables_default_enumeration_is_cached_across_instances():
    """The cache is process-wide, not per-instance (its own docstring) --
    proven here with two SEPARATE UCTableDataSource instances, the shape
    every real service.list_data_asset_cards() call actually constructs
    (CLAUDE.md §8 P5, ctx.data_source_factory builds a fresh one per call)."""
    _CountingWorkspaceClient.call_count = 0
    ds1, _ = _ds({}, bindings={}, workspace_client_factory=_CountingWorkspaceClient)
    ds2, _ = _ds({}, bindings={}, workspace_client_factory=_CountingWorkspaceClient)

    ds1.list_tables()
    ds2.list_tables()

    assert _CountingWorkspaceClient.call_count == 1


def test_list_tables_explicit_catalog_bypasses_the_cache():
    _CountingWorkspaceClient.call_count = 0
    ds, _ = _ds({}, bindings={}, workspace_client_factory=_CountingWorkspaceClient)

    ds.list_tables(catalog="orchestrationsuite")
    ds.list_tables(catalog="orchestrationsuite")

    # catalogs.list() is only ever called for the default (catalog=None)
    # enumeration -- an explicit catalog= never triggers it at all, cached
    # or not, so this asserts the explicit-catalog path is simply never
    # cached (repeat calls keep working, never a stale cache hit tripping
    # an assertion elsewhere).
    assert _CountingWorkspaceClient.call_count == 0


def test_list_tables_default_enumeration_excludes_system_catalogs():
    class _WithSystemCatalog(_FakeWorkspaceClient):
        class _Catalogs:
            def list(self):
                return [_FakeNamed("orchestrationsuite"), _FakeNamed("system"), _FakeNamed("samples")]

    ds, _ = _ds({}, bindings={}, workspace_client_factory=_WithSystemCatalog)
    results = ds.list_tables()
    assert not any(r.get("catalog") in ("system", "samples") for r in results)
    assert not any(r.get("fqn") in ("system", "samples") for r in results)


# ── BUG-EXPLORER-1 (independent review round 2, 2026-09-25) ────────────────


class _WithLedgerAndAppSchemas(_FakeWorkspaceClient):
    """A single catalog holding the ledger schema (Settings.catalog/.schema),
    another internal/app schema, and a real business-source schema --
    exactly the shape that let the Explorer checklist's default (no search
    typed) list come back 100% internal tables."""

    class _Catalogs:
        def list(self):
            return [_FakeNamed("orchestrationsuite")]

    class _Schemas:
        def list(self, catalog_name):
            return [_FakeNamed("ledger_schema"), _FakeNamed("app_probe"), _FakeNamed("tne_source")]

    class _Tables:
        def list(self, catalog_name, schema_name):
            if schema_name == "ledger_schema":
                return [_FakeTable("controls", None, ["control_id"])]
            if schema_name == "app_probe":
                return [_FakeTable("heartbeat", None, ["ts"])]
            return [_FakeTable("expense_report", "T&E expense claims", ["Employee ID"])]


def test_list_tables_default_enumeration_excludes_the_ledger_schema():
    settings = Settings(
        host="https://x.cloud.databricks.com", warehouse_http_path="/sql/1.0/warehouses/abc",
        catalog="orchestrationsuite", schema="ledger_schema",
    )
    ds = UCTableDataSource(settings, {}, workspace_client_factory=_WithLedgerAndAppSchemas)

    results = ds.list_tables()

    schemas_seen = {r.get("schema") for r in results if not r.get("restricted")}
    assert "ledger_schema" not in schemas_seen
    assert not any(r.get("fqn", "").startswith("orchestrationsuite.ledger_schema.") for r in results)


def test_list_tables_configured_exclusion_list_hides_other_internal_schemas():
    settings = Settings(
        host="https://x.cloud.databricks.com", warehouse_http_path="/sql/1.0/warehouses/abc",
        catalog="orchestrationsuite", schema="ledger_schema",
        excluded_schemas=("orchestrationsuite.app_probe",),
    )
    ds = UCTableDataSource(settings, {}, workspace_client_factory=_WithLedgerAndAppSchemas)

    results = ds.list_tables()

    schemas_seen = {r.get("schema") for r in results if not r.get("restricted")}
    assert schemas_seen == {"tne_source"}


def test_list_tables_source_schemas_allowlist_narrows_to_only_those_schemas():
    settings = Settings(
        host="https://x.cloud.databricks.com", warehouse_http_path="/sql/1.0/warehouses/abc",
        catalog="orchestrationsuite", schema="ledger_schema",
        source_schemas=("orchestrationsuite.tne_source",),
    )
    ds = UCTableDataSource(settings, {}, workspace_client_factory=_WithLedgerAndAppSchemas)

    results = ds.list_tables()

    schemas_seen = {r.get("schema") for r in results if not r.get("restricted")}
    assert schemas_seen == {"tne_source"}
    found = {r["fqn"] for r in results if not r.get("restricted")}
    assert found == {"orchestrationsuite.tne_source.expense_report"}


def test_list_tables_source_schemas_allowlist_narrows_which_catalogs_are_walked():
    class _CountingSchemas(_WithLedgerAndAppSchemas._Schemas):
        call_count = 0

        def list(self, catalog_name):
            _CountingSchemas.call_count += 1
            return super().list(catalog_name)

    class _TwoCatalogWorkspaceClient(_WithLedgerAndAppSchemas):
        class _Catalogs:
            def list(self):
                return [_FakeNamed("orchestrationsuite"), _FakeNamed("other_catalog")]

        _Schemas = _CountingSchemas

    _CountingSchemas.call_count = 0
    settings = Settings(
        host="https://x.cloud.databricks.com", warehouse_http_path="/sql/1.0/warehouses/abc",
        catalog="orchestrationsuite", schema="ledger_schema",
        source_schemas=("orchestrationsuite.tne_source",),
    )
    ds = UCTableDataSource(settings, {}, workspace_client_factory=_TwoCatalogWorkspaceClient)

    ds.list_tables()

    # other_catalog holds no allow-listed schema, so its schemas are never
    # even listed -- one call for orchestrationsuite only, not two.
    assert _CountingSchemas.call_count == 1


def test_list_tables_explicit_schema_bypasses_ledger_exclusion():
    """The same "explicit means deliberate" precedent explicit `catalog=`
    already has: a caller that names the ledger schema by hand still
    reaches it -- only the AUTOMATIC (schema=None) enumeration filters it
    out, so this never blocks a legitimate need to read the ledger."""
    settings = Settings(
        host="https://x.cloud.databricks.com", warehouse_http_path="/sql/1.0/warehouses/abc",
        catalog="orchestrationsuite", schema="ledger_schema",
    )
    ds = UCTableDataSource(settings, {}, workspace_client_factory=_WithLedgerAndAppSchemas)

    results = ds.list_tables(catalog="orchestrationsuite", schema="ledger_schema")

    assert any(r.get("fqn") == "orchestrationsuite.ledger_schema.controls" for r in results)


def test_list_tables_explicit_schema_bypasses_source_schemas_allowlist():
    settings = Settings(
        host="https://x.cloud.databricks.com", warehouse_http_path="/sql/1.0/warehouses/abc",
        catalog="orchestrationsuite", schema="ledger_schema",
        source_schemas=("orchestrationsuite.tne_source",),
    )
    ds = UCTableDataSource(settings, {}, workspace_client_factory=_WithLedgerAndAppSchemas)

    results = ds.list_tables(catalog="orchestrationsuite", schema="app_probe")

    assert any(r.get("fqn") == "orchestrationsuite.app_probe.heartbeat" for r in results)


def test_list_tables_explicit_system_catalog_is_still_reachable():
    """`catalog=` bypasses both the default-enumeration skip-list AND the
    cache (see the two tests above) -- a caller that already knows it wants
    system.access.audit (CLAUDE.md §2.3's independent-trail note) is never
    blocked from reaching it."""
    class _WithSystemCatalog(_FakeWorkspaceClient):
        class _Schemas:
            def list(self, catalog_name):
                assert catalog_name == "system"
                return [_FakeNamed("access")]

        class _Tables:
            def list(self, catalog_name, schema_name):
                return [_FakeTable("audit", "System audit log", ["request_params"])]

    ds, _ = _ds({}, bindings={}, workspace_client_factory=_WithSystemCatalog)
    results = ds.list_tables(catalog="system")
    assert any(r.get("fqn") == "system.access.audit" for r in results)


def test_list_tables_marks_restricted_only_on_real_permission_errors():
    """A genuine PermissionDenied (403/PERMISSION_DENIED) still marks
    'restricted'; any OTHER DatabricksError (a timeout, a rate limit, a
    real platform error) is not a permission question and must propagate,
    never be silently mislabelled 'Restricted' (independent review
    2026-09-24 item 6)."""
    from databricks.sdk.errors import DeadlineExceeded

    class _FlakyWorkspaceClient(_FakeWorkspaceClient):
        class _Schemas:
            def list(self, catalog_name):
                raise DeadlineExceeded("upstream timeout")

    ds, _ = _ds({}, bindings={}, workspace_client_factory=_FlakyWorkspaceClient)
    with pytest.raises(DeadlineExceeded):
        ds.list_tables()


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
