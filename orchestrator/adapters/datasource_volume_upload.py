"""VolumeUploadAwareDataSource wraps a table-backed DataSourceAdapter (e.g.
orchestrator.adapters.datasource_uc.UCTableDataSource) so a contract source
bound to an uploaded file's Volume path (service.upload_file's
uploaded_files.volume_path -- run_setup's own binding-suggestion logic binds
a source to an upload by exact filename-stem match) is read from the Volume
via the Files API instead of being sent through the UC table SQL path, which
cannot make sense of a Volume path (it is not a `catalog.schema.table` FQN)
(CLAUDE.md §5 UI item 5). Every other source in `bindings` is passed straight
through to `table_source` unchanged.

The sha256 recorded at upload time (uploaded_files.sha256 -- also what
run_fingerprints.uploaded_file_hashes pins, CLAUDE.md §4.1) is verified
against the bytes actually read, on every read, before they are parsed: a
mismatch fails the run loudly (SourceVersionMismatch), it is never silently
accepted or defaulted around (CLAUDE.md NN14).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from orchestrator.contract import SourceVersionMismatch, parse_source_bytes


@dataclass
class VolumeUploadAwareDataSource:
    table_source: Any
    bindings: dict[str, str]
    contract_sources: dict[str, dict]
    persistence: Any
    export_storage: Any
    _uploaded_by_path: dict[str, dict] | None = field(default=None, init=False, repr=False, compare=False)

    def _uploaded_files_by_path(self) -> dict[str, dict]:
        if self._uploaded_by_path is None:
            self._uploaded_by_path = {r["volume_path"]: r for r in self.persistence.list_uploaded_files()}
        return self._uploaded_by_path

    def _uploaded_row(self, source: str) -> dict | None:
        path = self.bindings.get(source)
        if path is None:
            return None
        return self._uploaded_files_by_path().get(path)

    def _cfg_for(self, source: str, row: dict) -> dict:
        cfg = dict(self.contract_sources.get(source) or {})
        if "format" not in cfg:
            # An uploaded file's own extension decides its format when the
            # contract doesn't declare one -- the same rule
            # service.upload_file's inline profiling already uses (never
            # guessed from file content).
            lower = (row.get("filename") or self.bindings[source]).lower()
            cfg["format"] = "xlsx" if lower.endswith((".xlsx", ".xls")) else "csv"
        return cfg

    # ── DataSourceAdapter protocol ──────────────────────────────────────────

    def resolve_version(self, source: str) -> str:
        row = self._uploaded_row(source)
        if row is not None:
            return row["sha256"]
        return self.table_source.resolve_version(source)

    def resolve_source_versions(self, sources: list[str]) -> dict[str, str]:
        return {name: self.resolve_version(name) for name in sources}

    def read_population(
        self, source: str, *, version, columns: list[str] | None = None, filters: dict | None = None,
    ) -> pd.DataFrame:
        row = self._uploaded_row(source)
        if row is None:
            return self.table_source.read_population(source, version=version, columns=columns, filters=filters)

        volume_path = self.bindings[source]
        recorded_sha256 = row["sha256"]
        # Pinned-version check FIRST (CLAUDE.md §4.1 TOCTOU ordering): the
        # version this call was asked to read must still be the version
        # recorded for this upload before any byte is fetched.
        if str(version) != recorded_sha256:
            raise SourceVersionMismatch(source, str(version), recorded_sha256)

        data = self.export_storage.read(volume_path)
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != recorded_sha256:
            raise SourceVersionMismatch(source, recorded_sha256, actual_sha256)

        cfg = self._cfg_for(source, row)
        return parse_source_bytes(data, source, version, cfg, columns)

    def row_count(self, source: str, *, version: str | None = None) -> int:
        row = self._uploaded_row(source)
        if row is None:
            return self.table_source.row_count(source, version=version)
        v = version or self.resolve_version(source)
        return len(self.read_population(source, version=v))

    def column_stats(
        self, source: str, *, version: str | None = None,
        amount_column: str | None = None, date_column: str | None = None,
    ) -> dict:
        row = self._uploaded_row(source)
        if row is None:
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
