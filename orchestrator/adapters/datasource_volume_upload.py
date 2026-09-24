"""VolumeUploadAwareDataSource wraps a table-backed DataSourceAdapter (e.g.
orchestrator.adapters.datasource_uc.UCTableDataSource) so a contract source
bound to a flat file's Volume path is read from the Volume via the Files API
instead of being sent through the UC table SQL path, which cannot make sense
of a Volume path (it is not a `catalog.schema.table` FQN). Two kinds of flat
file are handled, checked in this order:

1. **An ad-hoc upload** (service.upload_file's uploaded_files.volume_path --
   run_setup's own binding-suggestion logic binds a source to an upload by
   exact filename-stem match). Its sha256 was recorded at upload time
   (CLAUDE.md §5 UI item 5).
2. **A configured Volume file** (independent review 2026-09-24 item 1 --
   CLAUDE.md §11 "Corporate workspace assessment": at the corporate
   workspace, T&E sources are Excel files in a UC Volume, not Delta tables).
   `configured_volume_paths` is `{exact volume path: binding}` for this
   Skill's `kind: volume_file` SOURCE_BINDINGS entries
   (orchestrator.source_bindings) -- an ops-declared exact path, never a
   filename guess. It carries no pre-recorded hash, so its sha256 is
   computed by reading the file, both at `resolve_version` (pinning it
   before any node reads it, CLAUDE.md §4.1 TOCTOU ordering) and again at
   `read_population` (re-verified against the pinned version).

Every other source in `bindings` is passed straight through to
`table_source` unchanged.

The sha256 (recorded at upload time, or computed for a configured Volume
file) is verified against the bytes actually read, on every read, before
they are parsed: a mismatch fails the run loudly (SourceVersionMismatch), it
is never silently accepted or defaulted around (CLAUDE.md NN14). A missing
or unreadable configured Volume file fails loudly too
(ConfiguredSourceUnavailable, naming the source and the path) -- there is no
bundled fallback, which matters most for per-diem rates (CLAUDE.md §11 user
decision: "Per-diem rates must be supplied in the Volume ... there is no
fallback").

Independent review 2026-09-24 item 7: `filters` cannot be pushed down into
a flat CSV/XLSX read the way UCTableDataSource pushes one into a WHERE
clause -- honouring it would mean silently reading the WHOLE file and
filtering in pandas, which is not what a caller asking for server-side
pushdown asked for, and pretending to push it down would be worse. A
non-empty `filters` on a flat-file-bound source raises rather than being
silently ignored. The cell ceiling (CLAUDE.md §2.3 rule 4, the same
DBX_MAX_CELLS knob datasource_uc.UCTableDataSource enforces) is checked
after parsing -- a flat file has no queryable row count ahead of the read,
so it cannot be checked before, but it is still checked before the frame
is handed back to a caller, never silently truncated or sampled.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from orchestrator.adapters.datasource_uc import _resolve_max_cells
from orchestrator.contract import SourceVersionMismatch, parse_source_bytes
from orchestrator.errors import ConfiguredSourceUnavailable
from orchestrator.explorer.profile import pandas_distinct_count, pandas_profile_columns


@dataclass
class VolumeUploadAwareDataSource:
    table_source: Any
    bindings: dict[str, str]
    contract_sources: dict[str, dict]
    persistence: Any
    export_storage: Any
    max_cells: int | None = None  # None resolves DBX_MAX_CELLS / the shared default, same as UCTableDataSource
    # Independent review 2026-09-24 item 1: this Skill's `kind: volume_file`
    # SOURCE_BINDINGS entries, keyed by their exact declared path (see this
    # module's own docstring, point 2). Empty by default so every caller
    # that has not adopted configured bindings behaves exactly as before.
    configured_volume_paths: dict[str, dict] = field(default_factory=dict)
    _uploaded_by_path: dict[str, dict] | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._max_cells = _resolve_max_cells(self.max_cells)

    def _uploaded_files_by_path(self) -> dict[str, dict]:
        if self._uploaded_by_path is None:
            self._uploaded_by_path = {r["volume_path"]: r for r in self.persistence.list_uploaded_files()}
        return self._uploaded_by_path

    def _uploaded_row(self, source: str) -> dict | None:
        path = self.bindings.get(source)
        if path is None:
            return None
        return self._uploaded_files_by_path().get(path)

    def _configured_binding(self, source: str) -> dict | None:
        path = self.bindings.get(source)
        if path is None:
            return None
        return self.configured_volume_paths.get(path)

    def _is_flat_file(self, source: str) -> bool:
        return self._uploaded_row(source) is not None or self._configured_binding(source) is not None

    def _cfg_for_path(self, source: str, path_or_filename: str, sheet_override: object = None) -> dict:
        cfg = dict(self.contract_sources.get(source) or {})
        if "format" not in cfg:
            # A flat file's own extension decides its format when the
            # contract doesn't declare one -- the same rule
            # service.upload_file's inline profiling already uses (never
            # guessed from file content).
            lower = path_or_filename.lower()
            cfg["format"] = "xlsx" if lower.endswith((".xlsx", ".xls")) else "csv"
        if sheet_override is not None:
            cfg["sheet"] = sheet_override
        return cfg

    def _cfg_for(self, source: str, row: dict) -> dict:
        return self._cfg_for_path(source, row.get("filename") or self.bindings[source])

    def _read_configured_bytes(self, source: str, path: str) -> bytes:
        try:
            data = self.export_storage.read(path)
        except Exception as exc:  # noqa: BLE001 -- any read failure means "unavailable", named below
            raise ConfiguredSourceUnavailable(source, path, str(exc)) from exc
        if data is None:
            raise ConfiguredSourceUnavailable(source, path, "read() returned no content")
        return data

    def _enforce_cell_ceiling(self, source: str, df: pd.DataFrame) -> None:
        n_data_cols = max(len(df.columns) - 2, 1)  # __source/__row_key are bookkeeping, not data columns
        cells = len(df) * n_data_cols
        if cells > self._max_cells:
            raise ValueError(
                f"{source}: {len(df):,} rows x {n_data_cols} columns = {cells:,} cells exceeds "
                f"the DBX_MAX_CELLS ceiling ({self._max_cells:,}). Narrow `columns`; this adapter "
                f"never silently samples (CLAUDE.md §2.3 rule 4)."
            )

    # ── DataSourceAdapter protocol ──────────────────────────────────────────

    def resolve_version(self, source: str) -> str:
        row = self._uploaded_row(source)
        if row is not None:
            return row["sha256"]
        binding = self._configured_binding(source)
        if binding is not None:
            path = self.bindings[source]
            data = self._read_configured_bytes(source, path)
            return hashlib.sha256(data).hexdigest()
        return self.table_source.resolve_version(source)

    def resolve_source_versions(self, sources: list[str]) -> dict[str, str]:
        return {name: self.resolve_version(name) for name in sources}

    def read_population(
        self, source: str, *, version, columns: list[str] | None = None, filters: dict | None = None,
    ) -> pd.DataFrame:
        row = self._uploaded_row(source)
        binding = None if row is not None else self._configured_binding(source)
        if row is None and binding is None:
            return self.table_source.read_population(source, version=version, columns=columns, filters=filters)

        if filters:
            # Independent review 2026-09-24 item 7: a flat file cannot push a
            # filter down (see this module's own docstring) -- silently
            # ignoring it would read and return the WHOLE file when the
            # caller asked for a filtered subset, a silent wrong answer
            # (CLAUDE.md NN14), never a case to guess through.
            raise ValueError(
                f"{source}: a flat-file-bound source cannot push filters down (got {filters!r}) -- "
                f"filter the returned DataFrame yourself, or bind this contract source to a "
                f"governed table instead"
            )

        volume_path = self.bindings[source]

        if row is not None:
            recorded_version = row["sha256"]
            # Pinned-version check FIRST (CLAUDE.md §4.1 TOCTOU ordering): the
            # version this call was asked to read must still be the version
            # recorded for this upload before any byte is fetched.
            if str(version) != recorded_version:
                raise SourceVersionMismatch(source, str(version), recorded_version)
            data = self._read_configured_bytes(source, volume_path)
            actual_version = hashlib.sha256(data).hexdigest()
            if actual_version != recorded_version:
                raise SourceVersionMismatch(source, recorded_version, actual_version)
            cfg = self._cfg_for(source, row)
        else:
            data = self._read_configured_bytes(source, volume_path)
            actual_version = hashlib.sha256(data).hexdigest()
            if actual_version != str(version):
                raise SourceVersionMismatch(source, str(version), actual_version)
            cfg = self._cfg_for_path(source, volume_path, sheet_override=binding.get("sheet"))

        df = parse_source_bytes(data, source, version, cfg, columns)

        # Cell ceiling (CLAUDE.md §2.3 rule 4) -- checked after parsing since
        # a flat file's row count is not knowable before reading it, but
        # still enforced before this frame reaches a caller: never silently
        # truncated or sampled.
        self._enforce_cell_ceiling(source, df)
        return df

    def row_count(self, source: str, *, version: str | None = None) -> int:
        if not self._is_flat_file(source):
            return self.table_source.row_count(source, version=version)
        v = version or self.resolve_version(source)
        return len(self.read_population(source, version=v))

    def column_stats(
        self, source: str, *, version: str | None = None,
        amount_column: str | None = None, date_column: str | None = None,
    ) -> dict:
        if not self._is_flat_file(source):
            return self.table_source.column_stats(
                source, version=version, amount_column=amount_column, date_column=date_column,
            )
        v = version or self.resolve_version(source)
        amount = min_date = max_date = None
        if amount_column or date_column:
            df = self.read_population(
                source, version=v, columns=[c for c in (amount_column, date_column) if c]
            )
            if amount_column:
                amount = float(df[amount_column].sum()) if len(df) else 0.0
            if date_column and len(df):
                dates = pd.to_datetime(df[date_column])
                if dates.notna().any():
                    min_date = dates.min().date().isoformat()
                    max_date = dates.max().date().isoformat()
        return {"amount": amount, "min_date": min_date, "max_date": max_date}

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
        if not self._is_flat_file(source):
            return self.table_source.profile_columns(
                source, version=version, max_distinct=max_distinct, min_count=min_count,
                audit_period=audit_period, audit_timezone=audit_timezone,
            )
        v = version or self.resolve_version(source)
        df = self.read_population(source, version=v)
        return pandas_profile_columns(
            df, max_distinct=max_distinct, min_count=min_count,
            audit_period=audit_period, audit_timezone=audit_timezone,
        )

    def distinct_count(self, source: str, *, version: int | str, columns: list[str]) -> int:
        if not self._is_flat_file(source):
            return self.table_source.distinct_count(source, version=version, columns=columns)
        v = version or self.resolve_version(source)
        df = self.read_population(source, version=v, columns=columns)
        return pandas_distinct_count(df, columns)

    def column_tags(self, source: str, *, version: int | str | None = None) -> dict[str, list[str]] | None:
        # A flat file (upload or configured Volume file) has no UC column
        # tags at all -- None here means "not applicable", the same signal
        # classify_pii treats a denied UC tag query as (§4.3.1 rule 2: falls
        # through to the heuristic rule, conservative never permissive).
        if not self._is_flat_file(source):
            column_tags = getattr(self.table_source, "column_tags", None)
            return column_tags(source, version=version) if column_tags is not None else None
        return None

    def close(self) -> None:
        close = getattr(self.table_source, "close", None)
        if close is not None:
            close()

    # ── Pass-through for UC-only extension methods ──────────────────────────
    # list_tables/get_row_count/get_classification (orchestrator/service.py's
    # list_governed_tables/list_data_asset_cards, CLAUDE.md §5 UI item 3) are
    # not part of the DataSourceAdapter Protocol this class implements above
    # -- they are UCTableDataSource-specific catalogue-discovery calls that
    # never touch `bindings`, so there is nothing upload-aware to do for
    # them. Without this, every UC-backend caller of the table_source built
    # by service._uc_factory silently lost these methods the moment this
    # wrapper was introduced (they are looked up with getattr(..., None) and
    # treated as "not available"), which is the bug this delegates around.
    def __getattr__(self, name: str):
        return getattr(self.table_source, name)
