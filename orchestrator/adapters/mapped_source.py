"""MappedDataSource -- independent review 2026-09-25 item 1 ("run inputs",
docs/specs/P7_mapping_authoring_design.md §1.3): applies a run's declared
column mappings ({source: {contract_column: physical_column}}, from
orchestrator.run_inputs.resolve_run_inputs) to ANY DataSourceAdapter, so the
rest of the pipeline -- engine.py, contract validation, populations,
primitives -- never has to know a source's real header differs from the
contract's. One wrapper, adapter-agnostic, following the same delegation
pattern as orchestrator.nodes.context._CachingDataSource: every method this
class does not explicitly override is passed straight through via
__getattr__.

Renames logical (contract) to physical names in `columns=`/`filters=`
before delegating (so UC/Volume SQL pushdown still runs against the real
column), and renames physical back to logical in whatever the inner adapter
returns. `read_population`'s __source/__row_key bookkeeping columns are
never touched. Recoding VALUES (e.g. a code -> label lookup) is not column
mapping and is out of scope here (§1.3) -- a declared lookup parameter can
express that later."""

from __future__ import annotations

from typing import Any


class MappedDataSource:
    def __init__(self, inner: Any, mappings: dict[str, dict[str, str]]):
        self._inner = inner
        self._mappings = mappings or {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _contract_to_physical(self, source: str) -> dict[str, str]:
        return self._mappings.get(source) or {}

    def _physical_to_contract(self, source: str) -> dict[str, str]:
        return {phys: contract for contract, phys in self._contract_to_physical(source).items()}

    def _map_columns(self, source: str, columns: list[str] | None) -> list[str] | None:
        if not columns:
            return columns
        m = self._contract_to_physical(source)
        return [m.get(c, c) for c in columns]

    def _map_filters(self, source: str, filters: dict[str, Any] | None) -> dict[str, Any] | None:
        if not filters:
            return filters
        m = self._contract_to_physical(source)
        return {m.get(k, k): v for k, v in filters.items()}

    def _rename_frame(self, source: str, df):
        rev = self._physical_to_contract(source)
        if not rev:
            return df
        # Drop a pre-existing column that a rename would shadow -- a
        # physical->logical rename must never leave two columns claiming the
        # SAME logical name (CLAUDE.md §1.3: "drops any shadowed physical
        # column").
        for physical, logical in rev.items():
            if physical == logical:
                continue
            if logical in df.columns and physical in df.columns:
                df = df.drop(columns=[logical])
        rename_map = {phys: logical for phys, logical in rev.items() if phys in df.columns}
        return df.rename(columns=rename_map) if rename_map else df

    def read_population(
        self,
        source: str,
        *,
        version,
        columns: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        audit_timezone: str | None = None,
    ):
        df = self._inner.read_population(
            source,
            version=version,
            columns=self._map_columns(source, columns),
            filters=self._map_filters(source, filters),
            audit_timezone=audit_timezone,
        )
        return self._rename_frame(source, df)

    def column_stats(
        self,
        source: str,
        *,
        version: str | None = None,
        amount_column: str | None = None,
        date_column: str | None = None,
        audit_timezone: str | None = None,
    ) -> dict:
        m = self._contract_to_physical(source)
        return self._inner.column_stats(
            source,
            version=version,
            amount_column=m.get(amount_column, amount_column) if amount_column else amount_column,
            date_column=m.get(date_column, date_column) if date_column else date_column,
            audit_timezone=audit_timezone,
        )

    def profile_columns(
        self,
        source: str,
        *,
        version,
        max_distinct: int,
        min_count: int,
        audit_period: tuple[str, str] | None = None,
        audit_timezone: str | None = None,
    ) -> dict:
        result = self._inner.profile_columns(
            source, version=version, max_distinct=max_distinct, min_count=min_count,
            audit_period=audit_period, audit_timezone=audit_timezone,
        )
        rev = self._physical_to_contract(source)
        if not rev or not isinstance(result, dict):
            return result
        columns = result.get("columns")
        if isinstance(columns, dict):
            result = {**result, "columns": {rev.get(k, k): v for k, v in columns.items()}}
        elif isinstance(columns, list):
            renamed_cols = []
            for c in columns:
                c = dict(c)
                if c.get("name") in rev:
                    c["name"] = rev[c["name"]]
                renamed_cols.append(c)
            result = {**result, "columns": renamed_cols}
        return result

    def distinct_count(self, source: str, *, version, columns: list[str]) -> int:
        return self._inner.distinct_count(source, version=version, columns=self._map_columns(source, columns))
