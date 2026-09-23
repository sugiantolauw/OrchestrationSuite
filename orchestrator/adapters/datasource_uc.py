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
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from orchestrator.config import Settings

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Default cell ceiling (rows x selected columns) for a single read_population call.
# The largest current T&E source (expense_report) is ~92.8k rows x 46 cols =
# ~4.3M cells, so this leaves ample headroom while still being a real ceiling on an
# App container's memory (CLAUDE.md §2.3 rule 4). Overridden by DBX_MAX_CELLS.
_DEFAULT_MAX_CELLS = 20_000_000


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


def _normalise_datetime_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """The SQL warehouse returns TIMESTAMP columns as tz-aware pyarrow timestamps;
    pandas.read_excel (what LocalFileDataSource uses) returns naive
    datetime64[us]. Normalise so a UC-backed and file-backed read of the same data
    produce identical dtypes (verified in tests/test_datasource_uc.py's live
    equality test)."""
    for col in df.columns:
        series = df[col]
        if pd.api.types.is_datetime64_any_dtype(series):
            if getattr(series.dt, "tz", None) is not None:
                series = series.dt.tz_convert("UTC").dt.tz_localize(None)
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

    def resolve_version(self, source: str) -> str:
        """Latest Delta version via DESCRIBE HISTORY, resolved BEFORE any read
        (CLAUDE.md §4.1 TOCTOU ordering) -- callers pass this straight into
        read_population's `version=`."""
        fqn = self._fqn(source)
        quoted = _quoted_fqn(fqn)
        cur = self._execute(f"DESCRIBE HISTORY {quoted} LIMIT 1")
        columns = [d[0] for d in cur.description]
        row = cur.fetchone()
        if row is None:
            raise UCSourceError(f"{source}: DESCRIBE HISTORY returned no rows for {fqn} -- table has no commits")
        idx = columns.index("version")
        return str(row[idx])

    def resolve_source_versions(self, sources: list[str]) -> dict[str, str]:
        # Matches orchestrator.contract.LocalFileDataSource.resolve_source_versions:
        # despite the Protocol's parameter name, this takes SOURCE NAMES (contract.yaml
        # keys), not table_fqns -- callers resolve a whole Skill's sources by name.
        return {name: self.resolve_version(name) for name in sources}

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
        df = _normalise_datetime_dtypes(df)
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

    # ── UI helper (source-binding dropdowns) ────────────────────────────────

    def list_tables(self, catalog: str | None = None, schema: str | None = None) -> list[dict]:
        """Lists tables for the UI's source-binding dropdowns (CLAUDE.md §8 P5): one
        entry per accessible table `{fqn, catalog, schema, table, comment, columns}`.
        A catalog this identity cannot list surfaces as `{"fqn": catalog,
        "restricted": True}` rather than being silently dropped or raising, so the
        UI can show 'Restricted' instead of an empty list."""
        from databricks.sdk.errors import DatabricksError

        w = self._workspace_client()

        results: list[dict] = []
        if catalog is not None:
            catalogs = [catalog]
        else:
            try:
                catalogs = [c.name for c in w.catalogs.list()]
            except DatabricksError as exc:
                raise UCSourceError(f"could not list catalogs: {exc}") from exc

        for cat in catalogs:
            try:
                schemas = [schema] if schema is not None else [s.name for s in w.schemas.list(catalog_name=cat)]
            except DatabricksError:
                results.append({"fqn": cat, "restricted": True})
                continue
            for sch in schemas:
                try:
                    for t in w.tables.list(catalog_name=cat, schema_name=sch):
                        results.append(
                            {
                                "fqn": f"{cat}.{sch}.{t.name}",
                                "catalog": cat,
                                "schema": sch,
                                "table": t.name,
                                "comment": t.comment,
                                "columns": [c.name for c in (t.columns or [])],
                            }
                        )
                except DatabricksError:
                    results.append({"fqn": f"{cat}.{sch}", "restricted": True})
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
