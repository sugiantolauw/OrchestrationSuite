"""DataSourceAdapter (orchestrator/adapters/protocols.py) backed by governed Delta
tables in Unity Catalog, read through the attached serverless SQL warehouse
(CLAUDE.md §2.1, §7). Point the T&E Skill's contract.yaml sources at UC tables
(loaded by scripts/load_tne_sources_to_uc.py) instead of synthetic_data/ files by
constructing a UCTableDataSource with a {source_name: table_fqn} binding and handing
it to orchestrator.engine.execute_skill in place of a LocalFileDataSource -- the
engine cannot tell the difference (same __source/__row_key columns, same dtypes,
same ContractViolation-raising shape).

TOCTOU ordering (CLAUDE.md §4.1): callers MUST call resolve_version(source) first
and pass that exact version into read_population(..., version=...); read_population
pins the read with `VERSION AS OF <version>`, so a table written to between the two
calls does not change what was read.

Never samples. If a requested read would exceed the DBX_MAX_CELLS ceiling, it raises
loudly instead of silently truncating or sampling (CLAUDE.md §2.3 rule 4).
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from orchestrator.config import Settings
from orchestrator.explorer.profile import pandas_distinct_count, pandas_profile_columns
from orchestrator.timeutil import to_business_local

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Default cell ceiling (rows x selected columns) for a single read_population call.
# The largest current T&E source (expense_report) is ~92.8k rows x 46 cols =
# ~4.3M cells, so this leaves ample headroom while still being a real ceiling on an
# App container's memory (CLAUDE.md §2.3 rule 4). Overridden by DBX_MAX_CELLS.
_DEFAULT_MAX_CELLS = 20_000_000

# Process-wide, not per-instance: ctx.data_source_factory (orchestrator/service.py)
# builds a fresh UCTableDataSource on every call, so an instance-level cache would
# never actually be hit across separate service.list_data_asset_cards() calls (e.g.
# every keystroke in the data-search box). Keyed by (fqn, version) -- CLAUDE.md §5
# UI item 3's "cache by table fqn + Delta version".
_ROW_COUNT_CACHE: dict[tuple[str, str], int] = {}

# Independent review 2026-09-24 item 6: list_tables()'s DEFAULT enumeration
# (catalog=None) walks every catalog -> every schema -> every table with no
# query pushdown at all -- service.list_data_asset_cards() calls it fresh on
# every keystroke in the data-search box, which is exactly the query-volume
# shape the 2026-09-23 idle-cost incident was about. Cached process-wide
# (same reasoning as _ROW_COUNT_CACHE above) for _TABLE_LISTING_TTL_S: a
# keystroke inside that window hits the cache instead of re-walking Unity
# Catalog. Keyed on (catalog, schema) so an explicit catalog=/schema=
# narrowing never returns the default listing's cached entry by mistake.
_TABLE_LISTING_TTL_S = 300
_TABLE_LISTING_CACHE: dict[tuple[str | None, str | None], tuple[float, list[dict]]] = {}


def clear_table_listing_cache() -> None:
    """Test-only: this cache is deliberately process-wide/cross-instance
    (see its own comment above), which means it must be reset between tests
    that construct different fake UC listings under the same (catalog=None,
    schema=None) key -- production never needs this (a real workspace's
    listing does not change identity mid-process the way test fakes do)."""
    _TABLE_LISTING_CACHE.clear()

# Catalogs that are never audit source data -- excluded from the DEFAULT
# (catalog=None) enumeration so a keystroke search never pays the cost of
# walking them, never silently dropped when a caller actually wants one:
# passing catalog="system" explicitly (a caller that already knows it wants
# system.access.audit, CLAUDE.md §2.3's independent-trail note) bypasses
# this list entirely (see the `catalog is not None` branch below).
_SYSTEM_CATALOGS = {"system", "samples", "__databricks_internal", "hive_metastore"}


def _parse_schema_pairs(entries: tuple[str, ...]) -> set[tuple[str, str]]:
    """`Settings.source_schemas`/`excluded_schemas` are `catalog.schema`
    strings (the same shape `DBX_SOURCE_SCHEMAS` already has for
    scripts/deploy_app.py's grants, CLAUDE.md §7). A malformed entry (no
    dot, or an empty half) is dropped rather than raised on here --
    Settings.__post_init__ is not the enforcement point for this value and
    a malformed entry silently matching nothing is the safe failure mode
    for a schema FILTER (never widens what is shown; CLAUDE.md NN14)."""
    pairs: set[tuple[str, str]] = set()
    for entry in entries:
        catalog, sep, schema = entry.partition(".")
        if sep and catalog and schema:
            pairs.add((catalog, schema))
    return pairs


class UCSourceError(Exception):
    """Raised for anything that would otherwise force a silent guess: an unknown
    binding, a column that cannot be resolved, a query that would exceed the memory
    ceiling. Never caught and defaulted (CLAUDE.md NN14)."""


def _quote_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _validate_identifier_part(kind: str, value: str) -> None:
    if not _IDENTIFIER_RE.match(value):
        raise UCSourceError(
            f"refusing to use {kind}={value!r} in a query: not a plain identifier "
            f"(catalog/schema/table names are validated before being interpolated "
            f"into SQL -- this is what stops 'a; DROP TABLE ...' style injection)"
        )


def _split_and_validate_fqn(fqn: str) -> tuple[str, str, str]:
    parts = fqn.split(".")
    if len(parts) != 3:
        raise UCSourceError(f"table_fqn must be 'catalog.schema.table', got {fqn!r}")
    catalog, schema, table = parts
    for kind, part in (("catalog", catalog), ("schema", schema), ("table", table)):
        _validate_identifier_part(kind, part)
    return catalog, schema, table


def _quoted_fqn(fqn: str) -> str:
    catalog, schema, table = _split_and_validate_fqn(fqn)
    return f"{_quote_ident(catalog)}.{_quote_ident(schema)}.{_quote_ident(table)}"


def _epoch_ms_to_date(epoch_ms: int) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(epoch_ms / 1000, tz=datetime.timezone.utc).date().isoformat()


def _resolve_max_cells(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    raw = os.environ.get("DBX_MAX_CELLS")
    if not raw:
        return _DEFAULT_MAX_CELLS
    return int(raw)


def _default_connection_factory(settings: Settings):
    # Mirrors orchestrator.adapters.persistence_delta.DeltaPersistence's
    # _default_connection_factory. Duplicated rather than imported: that method is
    # bound to a DeltaPersistence instance (migrations, DDL dir, ...) that this
    # adapter has no business constructing, and persistence_delta.py is owned by
    # another in-flight P1A/P1B change this task must not touch.
    from databricks import sql
    from databricks.sdk.core import Config

    settings.require("warehouse_http_path", "host")
    cfg = Config(host=settings.host)
    hostname = settings.host.replace("https://", "").replace("http://", "").rstrip("/")
    return sql.connect(
        server_hostname=hostname,
        http_path=settings.warehouse_http_path,
        credentials_provider=lambda: cfg.authenticate,
    )


def _normalise_datetime_dtypes(df: pd.DataFrame, audit_timezone: str | None = None) -> pd.DataFrame:
    """The SQL warehouse returns TIMESTAMP columns as tz-aware pyarrow timestamps;
    pandas.read_excel (what LocalFileDataSource uses) returns naive
    datetime64[us]. Normalise so a UC-backed and file-backed read of the same data
    produce identical dtypes AND identical wall-clock values (verified in
    tests/test_datasource_uc.py's live equality test).

    `audit_timezone` is the contract's declared business-calendar timezone
    (CLAUDE.md §0.5, NN14; independent test-gap audit #13/H9) -- a tz-aware
    column is converted to it (never left as a hardcoded "UTC", which is what
    let a UC-backed read silently disagree with the file-backed read at an
    audit-period boundary) via orchestrator.timeutil.to_business_local, THEN
    stripped to naive so both paths end up with the same dtype. Defaults to
    "UTC" only for a caller that has no contract in hand yet (e.g. the dtype-
    only unit test below, or a pre-frame-snapshot legacy read) -- every real
    execute_skill()/NodeContext-routed read passes the Skill's own
    contract['timezone'] explicitly."""
    tz = audit_timezone or "UTC"
    for col in df.columns:
        series = df[col]
        if pd.api.types.is_datetime64_any_dtype(series):
            series = to_business_local(series, tz)
            df[col] = series.astype("datetime64[us]")
    return df


@dataclass
class UCTableDataSource:
    """`bindings` maps this Skill's contract.yaml source names (e.g.
    'expense_report') to the Unity Catalog table that holds them
    (e.g. 'orchestrationsuite.tne_source.expense_report')."""

    settings: Settings
    bindings: dict[str, str]
    max_cells: int | None = None
    connection_factory: Callable[[], object] | None = None
    workspace_client_factory: Callable[[], Any] | None = None
    _conn: object | None = field(default=None, init=False, repr=False, compare=False)
    _conn_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _ws_client: object | None = field(default=None, init=False, repr=False, compare=False)
    _ws_client_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._max_cells = _resolve_max_cells(self.max_cells)
        self._factory = self.connection_factory or (lambda: _default_connection_factory(self.settings))

    def _workspace_client(self):
        """Cached WorkspaceClient for list_tables() -- built once per adapter
        instance and reused (mirrors VolumeExportStorage._client() in
        orchestrator/adapters/export_storage.py, which re-pays SDK auth/config
        resolution on every call otherwise).

        Uses the SDK's default unified auth resolution (host from config, then
        whatever credential strategy `WorkspaceClient` itself picks -- PAT, OAuth
        M2M, etc.) rather than reaching into `databricks.sdk.core.Config` for a
        `credentials_strategy` attribute: that attribute does not exist on the
        installed SDK (0.140.0; it is `_credentials_strategy`, private), so doing
        that raised AttributeError and broke UC table discovery entirely (found
        live against the deployed App, whose service principal authenticates via
        injected OAuth M2M credentials -- the same unified-auth path this now
        relies on)."""
        with self._ws_client_lock:
            if self._ws_client is None:
                if self.workspace_client_factory is not None:
                    self._ws_client = self.workspace_client_factory()
                else:
                    from databricks.sdk import WorkspaceClient

                    self._ws_client = WorkspaceClient(host=self.settings.host)
            return self._ws_client

    # ── binding / connection plumbing ───────────────────────────────────────

    def _fqn(self, source: str) -> str:
        fqn = self.bindings.get(source)
        if fqn is None:
            raise UCSourceError(
                f"no table bound for source {source!r} -- bound sources: {sorted(self.bindings)}"
            )
        return fqn

    def _get_connection(self):
        with self._conn_lock:
            if self._conn is None:
                self._conn = self._factory()
            return self._conn

    def _execute(self, sql_text: str, params: dict | None = None):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql_text, params or {})
            return cur
        except Exception:
            # Connection-shaped failures aren't distinguished here the way
            # persistence_delta.py does (that heuristic lives with the
            # PersistenceAdapter this task must not touch); a caller that hits a
            # dead connection gets a clear failure and can retry with a fresh
            # UCTableDataSource rather than have this adapter guess.
            raise

    def close(self) -> None:
        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ── DataSourceAdapter protocol ──────────────────────────────────────────

    def _resolve_version_for_fqn(self, fqn: str) -> str:
        quoted = _quoted_fqn(fqn)
        cur = self._execute(f"DESCRIBE HISTORY {quoted} LIMIT 1")
        columns = [d[0] for d in cur.description]
        row = cur.fetchone()
        if row is None:
            raise UCSourceError(f"DESCRIBE HISTORY returned no rows for {fqn} -- table has no commits")
        idx = columns.index("version")
        return str(row[idx])

    def resolve_version(self, source: str) -> str:
        """Latest Delta version via DESCRIBE HISTORY, resolved BEFORE any read
        (CLAUDE.md §4.1 TOCTOU ordering) -- callers pass this straight into
        read_population's `version=`."""
        return self._resolve_version_for_fqn(self._fqn(source))

    def resolve_source_versions(self, sources: list[str]) -> dict[str, str]:
        # Matches orchestrator.contract.LocalFileDataSource.resolve_source_versions:
        # despite the Protocol's parameter name, this takes SOURCE NAMES (contract.yaml
        # keys), not table_fqns -- callers resolve a whole Skill's sources by name.
        #
        # P3/P4 perf gap review (2026-09-25): a plain `{name: self.resolve_version(name)
        # for name in sources}` loop runs every DESCRIBE HISTORY sequentially, because
        # resolve_version -> _resolve_version_for_fqn -> _execute -> _get_connection() all
        # share this instance's ONE lazily-opened connection, guarded by `_conn_lock` --
        # a second thread's resolve_version blocks on the lock for the first thread's
        # entire round trip. Live measurement: service.start_audit_run resolving
        # SKILL-001's 8 bound sources sequentially took 13.0s (0.97-5.66s each).
        #
        # Below, each resolution gets its OWN short-lived connection (`self._factory()`,
        # opened and closed just for that one DESCRIBE HISTORY) and they run concurrently
        # via a bounded ThreadPoolExecutor -- CLAUDE.md §2.3 rule 3's concurrency cap
        # (DBX_MAX_CONNECTIONS, `settings.max_connections`, the same knob
        # persistence_delta.DeltaPersistence's connection pool is bounded by) applies here
        # too, capped further at `len(sources)` so a small Skill never opens connections
        # it has no use for. `self._fqn(name)` is resolved for every source BEFORE any
        # thread starts, so an unbound source (UCSourceError) fails loudly before a single
        # connection is opened, exactly as it did in the sequential version. Every source's
        # version is still resolved before any read (CLAUDE.md §4.1 TOCTOU ordering) --
        # this only changes HOW the resolutions happen, never when relative to a read.
        if not sources:
            return {}
        fqns = {name: self._fqn(name) for name in sources}
        cap = max(1, min(len(sources), self.settings.max_connections))
        if cap == 1:
            return {name: self._resolve_version_for_fqn(fqns[name]) for name in sources}

        def _resolve_on_own_connection(fqn: str) -> str:
            conn = self._factory()
            try:
                cur = conn.cursor()
                cur.execute(f"DESCRIBE HISTORY {_quoted_fqn(fqn)} LIMIT 1")
                columns = [d[0] for d in cur.description]
                row = cur.fetchone()
                if row is None:
                    raise UCSourceError(
                        f"DESCRIBE HISTORY returned no rows for {fqn} -- table has no commits"
                    )
                idx = columns.index("version")
                return str(row[idx])
            finally:
                # No idle connections left behind: each ephemeral connection
                # is closed the moment its own resolution is done, win or
                # lose, never held open waiting for the batch's other calls.
                try:
                    conn.close()
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=cap) as pool:
            futures = {name: pool.submit(_resolve_on_own_connection, fqns[name]) for name in sources}
            # .result() on each future re-raises that source's exception (if
            # any) here -- a single failure fails the whole batch loudly
            # (CLAUDE.md NN14), exactly as the sequential version's first
            # failing resolve_version() call would have. Iterating `sources`
            # (not completion order) assembles the returned dict
            # deterministically by source name, matching the input order,
            # regardless of which resolution actually finished first.
            return {name: futures[name].result() for name in sources}

    def _describe_columns(self, quoted_fqn: str, version: int) -> list[str]:
        cur = self._execute(f"SELECT * FROM {quoted_fqn} VERSION AS OF {version} LIMIT 0")
        return [d[0] for d in cur.description]

    def _row_count_at_version(self, quoted_fqn: str, version: int) -> int:
        cur = self._execute(f"SELECT count(*) AS n FROM {quoted_fqn} VERSION AS OF {version}")
        row = cur.fetchone()
        return int(row[0])

    @staticmethod
    def _logical_column_map(raw_columns: list[str]) -> dict[str, str]:
        """Maps a logical (contract.yaml-declared) column name to the actual UC
        column name that produces it. Exact matches win; a header_trim-style source
        (raw column carries a stray leading/trailing space, e.g. 'Currency ') is
        reached by stripping. Mirrors LocalFileDataSource's header_trim behaviour,
        but per-column instead of per-source, since this adapter has no contract.yaml
        access -- it only ever needs to agree with contract.yaml, never to police it
        (validate_contract does that after read_population returns)."""
        merged: dict[str, str] = {c: c for c in raw_columns}  # exact matches, priority baseline
        for c in raw_columns:
            stripped = c.strip()
            if stripped == c:
                continue
            if stripped in merged and merged[stripped] != c:
                raise UCSourceError(
                    f"columns {merged[stripped]!r} and {c!r} both resolve to logical name "
                    f"{stripped!r} -- ambiguous"
                )
            merged[stripped] = c
        return merged

    def read_population(
        self,
        source: str,
        *,
        version: int | str,
        columns: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        audit_timezone: str | None = None,
    ) -> pd.DataFrame:
        fqn = self._fqn(source)
        quoted = _quoted_fqn(fqn)
        try:
            version_int = int(version)
        except (TypeError, ValueError) as exc:
            raise UCSourceError(f"{source}: version must be an integer Delta version, got {version!r}") from exc

        actual_columns = self._describe_columns(quoted, version_int)
        if "_source_row" not in actual_columns:
            raise UCSourceError(
                f"{source}: table {fqn} has no `_source_row` column -- it was not loaded by "
                f"scripts/load_tne_sources_to_uc.py, so rows cannot be traced back to the source file"
            )
        raw_columns = [c for c in actual_columns if c != "_source_row"]
        logical_to_actual = self._logical_column_map(raw_columns)

        if columns:
            selected_logical = list(columns)
        else:
            selected_logical = [c.strip() for c in raw_columns]

        select_bits = ["`_source_row`"]
        for logical in selected_logical:
            actual = logical_to_actual.get(logical)
            if actual is None:
                raise UCSourceError(
                    f"{source}: column {logical!r} not found in {fqn} "
                    f"(available: {sorted(logical_to_actual)})"
                )
            if actual == logical:
                select_bits.append(_quote_ident(actual))
            else:
                select_bits.append(f"{_quote_ident(actual)} AS {_quote_ident(logical)}")

        where_sql, where_params = _build_where(filters, logical_to_actual)

        row_count = self._row_count_at_version(quoted, version_int)
        n_selected_cols = len(select_bits) - 1  # `_source_row` is bookkeeping, not a data column
        cells = row_count * max(n_selected_cols, 1)
        if cells > self._max_cells:
            raise UCSourceError(
                f"{source}: {row_count:,} rows x {n_selected_cols} columns = {cells:,} cells "
                f"exceeds the DBX_MAX_CELLS ceiling ({self._max_cells:,}). Narrow `columns` or "
                f"push a filter down; this adapter never silently samples (CLAUDE.md §2.3 rule 4)."
            )

        sql_text = f"SELECT {', '.join(select_bits)} FROM {quoted} VERSION AS OF {version_int}"
        if where_sql:
            sql_text += f" WHERE {where_sql}"
        # Spark/Delta makes no row-order guarantee without an ORDER BY. Ordering by
        # `_source_row` (assigned 1-based, in file order, at load time) is what makes
        # a UC-backed read line up row-for-row with the equivalent LocalFileDataSource
        # read on the same file (verified in tests/test_datasource_uc.py's live
        # equality test) rather than an incidental artifact of a single-file CTAS.
        sql_text += " ORDER BY `_source_row`"

        cur = self._execute(sql_text, where_params)
        arrow_table = cur.fetchall_arrow()
        df = arrow_table.to_pandas()
        df = _normalise_datetime_dtypes(df, audit_timezone)
        df = df.reset_index(drop=True)

        if "_source_row" not in df.columns:
            raise UCSourceError(f"{source}: query result is missing `_source_row`")
        src_row = df.pop("_source_row")
        row_key_prefix = f"{fqn}@v{version_int}"
        df.insert(0, "__row_key", [f"{row_key_prefix}:{v}" for v in src_row])
        df.insert(0, "__source", source)
        return df

    def aggregate(
        self,
        table_fqn: str,
        *,
        version: str | None = None,
        group_by: list[str],
        aggregations: dict[str, str],
        filters: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        """Pushes a group-by aggregation into the warehouse so a caller never has to
        pull a full population into pandas just to sum or count it (CLAUDE.md §2.3
        rule 4). `table_fqn` here is the fully-qualified table, not a bound source
        name -- callers that only have a source name resolve it via self.bindings
        first. `aggregations` maps output-column-name -> 'FUNC(column)', e.g.
        {'total': 'sum(`Expense Amount (reimbursement currency)`)'}; callers are
        responsible for quoting the column reference themselves since the function
        name varies."""
        quoted = _quoted_fqn(table_fqn)
        version_int = int(version) if version is not None else None

        actual_columns = self._describe_columns(quoted, version_int) if version_int is not None else None
        logical_to_actual = self._logical_column_map(actual_columns) if actual_columns else {}

        select_bits = []
        for g in group_by:
            actual = logical_to_actual.get(g, g)
            select_bits.append(
                _quote_ident(actual) if actual == g else f"{_quote_ident(actual)} AS {_quote_ident(g)}"
            )
        for out_name, expr in aggregations.items():
            select_bits.append(f"{expr} AS {_quote_ident(out_name)}")

        where_sql, where_params = _build_where(filters, logical_to_actual)

        sql_text = f"SELECT {', '.join(select_bits)} FROM {quoted}"
        if version_int is not None:
            sql_text += f" VERSION AS OF {version_int}"
        if where_sql:
            sql_text += f" WHERE {where_sql}"
        if group_by:
            sql_text += " GROUP BY " + ", ".join(
                _quote_ident(logical_to_actual.get(g, g)) for g in group_by
            )

        cur = self._execute(sql_text, where_params)
        arrow_table = cur.fetchall_arrow()
        return _normalise_datetime_dtypes(arrow_table.to_pandas())

    def row_count(self, source: str, *, version: str | None = None) -> int:
        """Takes a bound source name (contract.yaml key, e.g. 'expense_report'),
        exactly like resolve_version/read_population -- NOT an already-qualified
        table_fqn. The G6 reconciliation call site (orchestrator/nodes/fieldwork.py)
        is the only production caller and always passes a source name; it must get
        an INDEPENDENT row count at the same pinned version, so this resolves the
        binding itself rather than trusting an fqn the caller already looked up
        (this parameter was previously misnamed `table_fqn` and called `_quoted_fqn`
        directly on the bare source name, raising UCSourceError -- found live during
        the P3 integration pass, CLAUDE.md §2.3 rule 4)."""
        fqn = self._fqn(source)
        quoted = _quoted_fqn(fqn)
        v = int(version) if version is not None else None
        if v is None:
            cur = self._execute(f"DESCRIBE HISTORY {quoted} LIMIT 1")
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            v = int(row[cols.index("version")])
        return self._row_count_at_version(quoted, v)

    def column_stats(
        self,
        source: str,
        *,
        version: str | None = None,
        amount_column: str | None = None,
        date_column: str | None = None,
        audit_timezone: str | None = None,
    ) -> dict:
        """G6's amount/min-max-date reconciliation (CLAUDE.md §5 G6, P2/P3 gate
        review item 2), pushed into the warehouse (§2.3 rule 4) rather than
        pulling the whole source into pandas just to sum or bound-check it.

        `audit_timezone` applies the same rule read_population's
        `_normalise_datetime_dtypes` does (CLAUDE.md §0.5, NN14): the raw
        MIN()/MAX() comes back tz-aware from the warehouse, and must be
        converted to the contract's declared timezone -- never left as
        whatever the connector's own default is -- before its date is
        extracted, so this independent G6 figure agrees with the engine's
        own (read_population-derived) min/max date at the same boundary."""
        if not amount_column and not date_column:
            return {"amount": None, "min_date": None, "max_date": None}
        fqn = self._fqn(source)
        quoted = _quoted_fqn(fqn)
        v = int(version) if version is not None else None
        if v is None:
            cur = self._execute(f"DESCRIBE HISTORY {quoted} LIMIT 1")
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            v = int(row[cols.index("version")])

        actual_columns = self._describe_columns(quoted, v)
        logical_to_actual = self._logical_column_map([c for c in actual_columns if c != "_source_row"])

        aggregations: dict[str, str] = {}
        if amount_column:
            actual = logical_to_actual.get(amount_column)
            if actual is None:
                raise UCSourceError(f"{source}: amount_column {amount_column!r} not found in {fqn}")
            aggregations["__amount"] = f"sum({_quote_ident(actual)})"
        if date_column:
            actual = logical_to_actual.get(date_column)
            if actual is None:
                raise UCSourceError(f"{source}: date_column {date_column!r} not found in {fqn}")
            aggregations["__min_date"] = f"min({_quote_ident(actual)})"
            aggregations["__max_date"] = f"max({_quote_ident(actual)})"

        select_sql = ", ".join(f"{expr} AS {_quote_ident(out)}" for out, expr in aggregations.items())
        cur = self._execute(f"SELECT {select_sql} FROM {quoted} VERSION AS OF {v}")
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]

        amount = None
        if amount_column:
            v_amount = row[cols.index("__amount")]
            amount = float(v_amount) if v_amount is not None else 0.0
        min_date = max_date = None
        if date_column:
            tz = audit_timezone or "UTC"
            v_min, v_max = row[cols.index("__min_date")], row[cols.index("__max_date")]
            if v_min is not None:
                min_date = to_business_local(pd.Timestamp(v_min), tz).date().isoformat()
            if v_max is not None:
                max_date = to_business_local(pd.Timestamp(v_max), tz).date().isoformat()
        return {"amount": amount, "min_date": min_date, "max_date": max_date}

    # ── Explorer Mode profiling (docs/specs/P6_P8_explorer_llm_design.md §4.3) ──

    def profile_columns(
        self,
        source: str,
        *,
        version: int | str,
        max_distinct: int,
        min_count: int,
        audit_period: tuple[str, str] | None = None,
        audit_timezone: str | None = None,
    ) -> dict:
        # Documented simplification (see orchestrator.explorer.profile's own
        # docstring): reads the same bound population a Playbook profile()
        # node already reads (its own cell-ceiling guard, §2.3 rule 4,
        # applies unchanged), then computes every statistic in pandas --
        # ONE shared implementation with the local/upload adapters rather
        # than a second, SQL-pushdown one that could drift from it.
        # `audit_timezone` is threaded into the read (not just into
        # pandas_profile_columns' own in_period_count boundary math) so a
        # date/datetime column's min/max/values are the same business-local
        # wall-clock digits a Playbook run would see (CLAUDE.md §0.5, NN14).
        df = self.read_population(source, version=version, audit_timezone=audit_timezone)
        return pandas_profile_columns(
            df, max_distinct=max_distinct, min_count=min_count,
            audit_period=audit_period, audit_timezone=audit_timezone,
        )

    def distinct_count(self, source: str, *, version: int | str, columns: list[str]) -> int:
        df = self.read_population(source, version=version, columns=columns)
        return pandas_distinct_count(df, columns)

    def column_tags(self, source: str, *, version: int | str | None = None) -> dict[str, list[str]] | None:
        """§4.3.1 rule 2: `{column_name: [tag_name, ...]}` from
        `information_schema.column_tags`. `None` (never raised) only when
        this identity is genuinely denied that query -- the same
        PermissionDenied-vs-everything-else distinction `list_tables` makes
        (independent review 2026-09-24 item 6): a real platform error still
        raises, because that is not a permission question and must not be
        silently swallowed into a false 'tags unreadable'."""
        from databricks.sdk.errors import PermissionDenied

        fqn = self._fqn(source)
        catalog, schema, table = _split_and_validate_fqn(fqn)
        try:
            cur = self._execute(
                f"SELECT column_name, tag_name FROM {_quote_ident(catalog)}.information_schema.column_tags "
                f"WHERE schema_name = :schema_name AND table_name = :table_name",
                {"schema_name": schema, "table_name": table},
            )
            rows = cur.fetchall()
        except PermissionDenied:
            return None
        tags: dict[str, list[str]] = {}
        for column_name, tag_name in rows:
            tags.setdefault(column_name, []).append(tag_name)
        return tags

    # ── UI helper (data_asset_card metadata) ────────────────────────────────

    def get_row_count(self, fqn: str) -> int:
        """Real row count for one table, pinned to the version DESCRIBE
        HISTORY resolves right now (never an estimate from table properties,
        which can be stale). Cached by (fqn, version) on this adapter
        instance so re-rendering the same card (e.g. the search box firing
        again) never re-counts a table it already counted this process
        (CLAUDE.md §5 UI item 3: "cheap; cache by table fqn + Delta
        version"). Callers are expected to call this only for the small
        number of cards actually rendered, never for a whole catalog
        listing."""
        version = self._resolve_version_for_fqn(fqn)
        key = (fqn, version)
        cached = _ROW_COUNT_CACHE.get(key)
        if cached is not None:
            return cached
        quoted = _quoted_fqn(fqn)
        cur = self._execute(f"SELECT COUNT(*) FROM {quoted} VERSION AS OF {version}")
        row = cur.fetchone()
        count = int(row[0]) if row is not None else 0
        _ROW_COUNT_CACHE[key] = count
        return count

    def get_classification(self, fqn: str) -> str | None:
        """A UC tag named (case-insensitively) 'classification' on this
        table, if the workspace has tagged it -- never invented when no tag
        exists. `information_schema.table_tags` is catalog-scoped, so the
        query runs against the table's own catalog."""
        catalog, schema, table = _split_and_validate_fqn(fqn)
        cur = self._execute(
            f"SELECT tag_name, tag_value FROM {_quote_ident(catalog)}.information_schema.table_tags "
            f"WHERE schema_name = :schema_name AND table_name = :table_name",
            {"schema_name": schema, "table_name": table},
        )
        for tag_name, tag_value in cur.fetchall():
            if str(tag_name).lower() == "classification":
                return tag_value
        return None

    # ── UI helper (source-binding dropdowns) ────────────────────────────────

    def list_tables(self, catalog: str | None = None, schema: str | None = None) -> list[dict]:
        """Lists tables for the UI's source-binding dropdowns (CLAUDE.md §8 P5): one
        entry per accessible table `{fqn, catalog, schema, table, comment, columns}`.
        A catalog/schema this identity is genuinely denied access to (a real
        PermissionDenied -- HTTP 403/PERMISSION_DENIED) surfaces as
        `{"fqn": ..., "restricted": True}` rather than being silently dropped
        or raising, so the UI can show 'Restricted' instead of an empty list
        (independent review 2026-09-24 item 6: any OTHER DatabricksError --
        a timeout, a rate limit, a genuine platform error -- is not a
        permission question and must surface as a real error, never be
        mislabelled 'Restricted').

        The default (catalog=None) enumeration is cached process-wide for
        _TABLE_LISTING_TTL_S and skips _SYSTEM_CATALOGS (item 6: a keystroke
        in the data-search box must not re-walk every catalog/schema, and
        must not walk catalogs that are never audit source data) -- pass
        `catalog` explicitly to reach one of those, or any catalog, without
        either restriction.

        BUG-EXPLORER-1 (independent review round 2, 2026-09-25): the AUTOMATIC
        schema enumeration (schema=None, whether or not catalog is pinned) also
        never walks this platform's own ledger schema (Settings.catalog +
        Settings.schema), nor any schema in Settings.excluded_schemas -- both
        are configuration (CLAUDE.md NN16), never a hardcoded name. When
        Settings.source_schemas is non-empty it is an ALLOW-list instead: only
        those catalog.schema pairs are ever walked (and, for the default
        catalog=None enumeration, only their catalogs), the same narrowing
        scripts/deploy_app.py already grants the App's service principal for.
        A caller that passes `schema` explicitly still reaches it unfiltered --
        the same "explicit means deliberate" precedent as `catalog=` above."""
        from databricks.sdk.errors import DatabricksError, PermissionDenied

        cache_key = (catalog, schema)
        if catalog is None:
            cached = _TABLE_LISTING_CACHE.get(cache_key)
            if cached is not None:
                cached_at, cached_results = cached
                if time.monotonic() - cached_at < _TABLE_LISTING_TTL_S:
                    return cached_results

        allow_schemas = _parse_schema_pairs(self.settings.source_schemas)
        deny_schemas = _parse_schema_pairs(self.settings.excluded_schemas)
        if self.settings.catalog and self.settings.schema:
            deny_schemas = deny_schemas | {(self.settings.catalog, self.settings.schema)}

        w = self._workspace_client()

        results: list[dict] = []
        if catalog is not None:
            catalogs = [catalog]
        else:
            try:
                catalogs = [c.name for c in w.catalogs.list() if c.name not in _SYSTEM_CATALOGS]
            except DatabricksError as exc:
                raise UCSourceError(f"could not list catalogs: {exc}") from exc
            if allow_schemas:
                allowed_catalogs = {c for c, _ in allow_schemas}
                catalogs = [c for c in catalogs if c in allowed_catalogs]

        for cat in catalogs:
            try:
                if schema is not None:
                    schemas = [schema]
                else:
                    schemas = [s.name for s in w.schemas.list(catalog_name=cat)]
                    if allow_schemas:
                        schemas = [s for s in schemas if (cat, s) in allow_schemas]
                    else:
                        schemas = [s for s in schemas if (cat, s) not in deny_schemas]
            except PermissionDenied:
                results.append({"fqn": cat, "restricted": True})
                continue
            for sch in schemas:
                try:
                    for t in w.tables.list(catalog_name=cat, schema_name=sch):
                        entry = {
                            "fqn": f"{cat}.{sch}.{t.name}",
                            "catalog": cat,
                            "schema": sch,
                            "table": t.name,
                            "comment": t.comment,
                            "columns": [c.name for c in (t.columns or [])],
                        }
                        # UC-reported metadata, included only when the SDK
                        # actually returned it -- never a placeholder for a
                        # field UC left unset (CLAUDE.md NN14).
                        if t.owner:
                            entry["owner"] = t.owner
                        if t.updated_at:
                            entry["last_refreshed"] = _epoch_ms_to_date(t.updated_at)
                        results.append(entry)
                except PermissionDenied:
                    results.append({"fqn": f"{cat}.{sch}", "restricted": True})

        if catalog is None:
            _TABLE_LISTING_CACHE[cache_key] = (time.monotonic(), results)
        return results


def _build_where(filters: dict[str, Any] | None, logical_to_actual: dict[str, str]) -> tuple[str, dict]:
    """Minimal, explicit filter pushdown: {column: scalar} -> equality,
    {column: {"in": [...]}} -> IN, {column: {"gte": x, "lte": y}} -> range. Anything
    else raises rather than being silently ignored -- a filter that looks like it
    narrowed the population but didn't would be exactly the silent-wrong-answer
    shape CLAUDE.md NN14 forbids."""
    if not filters:
        return "", {}
    clauses = []
    params: dict[str, Any] = {}
    for i, (col, spec) in enumerate(filters.items()):
        actual = logical_to_actual.get(col, col)
        ident = _quote_ident(actual)
        if isinstance(spec, dict):
            if set(spec) - {"gte", "lte", "gt", "lt", "in"}:
                raise UCSourceError(f"filter on {col!r}: unsupported filter spec {spec!r}")
            if "in" in spec:
                keys = []
                for j, v in enumerate(spec["in"]):
                    key = f"f{i}_{j}"
                    params[key] = v
                    keys.append(f":{key}")
                clauses.append(f"{ident} IN ({', '.join(keys)})")
            else:
                op_sql = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}
                for op, sql_op in op_sql.items():
                    if op in spec:
                        key = f"f{i}_{op}"
                        params[key] = spec[op]
                        clauses.append(f"{ident} {sql_op} :{key}")
        else:
            key = f"f{i}"
            params[key] = spec
            clauses.append(f"{ident} = :{key}")
    return " AND ".join(clauses), params
