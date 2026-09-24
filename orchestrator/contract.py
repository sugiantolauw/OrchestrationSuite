from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orchestrator.explorer.profile import pandas_distinct_count, pandas_profile_columns


class ContractViolation(Exception):
    """Raised when data read from a source does not conform to its contract.yaml
    declaration (CLAUDE.md NN14/G7). Carries every violation found, not just the
    first, so a single run failure names the whole problem."""

    def __init__(self, violations: list[str]):
        self.violations = list(violations)
        super().__init__("; ".join(self.violations))


class SourceVersionMismatch(Exception):
    def __init__(self, source: str, expected: str, actual: str):
        self.source = source
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"source {source!r}: expected version {expected!r} (resolved before read), "
            f"file now hashes to {actual!r} -- it changed between resolve_version() and "
            f"read_population()"
        )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _coerce_string(series: pd.Series) -> tuple[pd.Series, pd.Index]:
    coerced = series.map(lambda v: v if _is_missing(v) else str(v))
    return coerced, pd.Index([])


def _coerce_number(series: pd.Series) -> tuple[pd.Series, pd.Index]:
    numeric = pd.to_numeric(series, errors="coerce")
    bad = series.index[numeric.isna() & ~series.map(_is_missing)]
    return numeric, bad


def _coerce_integer(series: pd.Series) -> tuple[pd.Series, pd.Index]:
    numeric = pd.to_numeric(series, errors="coerce")
    bad_mask = numeric.isna() & ~series.map(_is_missing)
    frac_mask = numeric.notna() & (numeric % 1 != 0)
    bad = series.index[bad_mask | frac_mask]
    coerced = numeric.where(~frac_mask, np.nan)
    return coerced, bad


_TRUE_STRINGS = {"true", "yes", "y", "1"}
_FALSE_STRINGS = {"false", "no", "n", "0"}


def _coerce_boolean(series: pd.Series) -> tuple[pd.Series, pd.Index]:
    def conv(v):
        if _is_missing(v):
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        s = str(v).strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
        return np.nan  # sentinel for "could not coerce"

    coerced = series.map(conv)
    bad = series.index[coerced.map(lambda v: isinstance(v, float) and math.isnan(v))]
    coerced = coerced.where(~coerced.index.isin(bad), None)
    return coerced, bad


def _coerce_date(series: pd.Series) -> tuple[pd.Series, pd.Index]:
    coerced = pd.to_datetime(series, errors="coerce")
    bad = series.index[coerced.isna() & ~series.map(_is_missing)]
    return coerced, bad


_COERCERS = {
    "string": _coerce_string,
    "integer": _coerce_integer,
    "number": _coerce_number,
    "boolean": _coerce_boolean,
    "date": _coerce_date,
    "datetime": _coerce_date,
}


def _coerce(series: pd.Series, type_name: str) -> tuple[pd.Series, pd.Index]:
    fn = _COERCERS.get(type_name)
    if fn is None:
        raise ContractViolation([f"unsupported contract type: {type_name!r}"])
    return fn(series)


def validate_contract(df: pd.DataFrame, source_contract: dict) -> None:
    """Validates `df` (as read by a DataSourceAdapter, carrying __row_key) against a
    single source's contract.yaml entry. Raises ContractViolation listing EVERY
    problem found. Mutates `df` in place: declared columns are trimmed
    (trim_values) and coerced to their declared type, so a caller that wants the
    typed/trimmed frame just keeps using `df` after this returns. No fuzzy column
    matching and no defaults (CLAUDE.md NN14) -- a declared column name must match
    exactly (after the source's own header_trim, applied by the DataSourceAdapter
    before this is called)."""
    violations: list[str] = []
    columns_cfg = source_contract.get("columns", {})
    row_key_col = df["__row_key"] if "__row_key" in df.columns else None

    def sample_keys(idx: pd.Index) -> list[str]:
        if row_key_col is None:
            return [str(i) for i in list(idx)[:5]]
        return row_key_col.loc[idx].tolist()[:5]

    for col, spec in columns_cfg.items():
        if col not in df.columns:
            violations.append(f"missing column: {col!r}")
            continue
        series = df[col]
        if spec.get("trim_values"):
            series = series.map(lambda v: v.strip() if isinstance(v, str) else v)
            df[col] = series

        type_name = spec.get("type", "string")
        coerced, bad_idx = _coerce(series, type_name)
        if len(bad_idx):
            violations.append(
                f"{col}: {len(bad_idx)} value(s) fail type coercion to {type_name!r} "
                f"(sample row keys: {sample_keys(bad_idx)})"
            )

        if not spec.get("nullable", True):
            null_idx = coerced.index[coerced.map(_is_missing)]
            if len(null_idx):
                violations.append(
                    f"{col}: {len(null_idx)} null value(s) in non-nullable column "
                    f"(sample row keys: {sample_keys(null_idx)})"
                )

        allowed = spec.get("allowed_values")
        if allowed is not None:
            non_null = ~coerced.map(_is_missing)
            bad_mask = non_null & ~coerced.isin(allowed)
            bad_idx2 = coerced.index[bad_mask]
            if len(bad_idx2):
                violations.append(
                    f"{col}: value(s) outside allowed_values {allowed!r} "
                    f"(sample row keys: {sample_keys(bad_idx2)})"
                )

        required = spec.get("required_values")
        if required is not None:
            non_null = ~coerced.map(_is_missing)
            bad_mask = non_null & ~coerced.isin(required)
            bad_idx3 = coerced.index[bad_mask]
            if len(bad_idx3):
                bad_values = sorted({str(v) for v in coerced.loc[bad_idx3].tolist()})
                violations.append(
                    f"{col}: required_values {required!r} violated by value(s) {bad_values} "
                    f"(sample row keys: {sample_keys(bad_idx3)})"
                )

        df[col] = coerced

    if violations:
        raise ContractViolation(violations)


def parse_source_bytes(
    data: bytes, source: str, version: int | str, cfg: dict, columns: list[str] | None = None,
) -> pd.DataFrame:
    """Parses already-read, already-version-verified file bytes into a
    population frame -- the contract-driven parsing (format/sheet/
    header_trim, `__source`/`__row_key` columns, `columns` projection) both
    LocalFileDataSource and orchestrator.adapters.datasource_volume_upload.
    VolumeUploadAwareDataSource (CLAUDE.md §5 UI item 5: an uploaded file
    read from the Volume via the Files API "applies the same contract
    validation") apply -- one implementation, not two that could drift."""
    fmt = cfg.get("format", "csv")
    if fmt == "xlsx":
        df = pd.read_excel(io.BytesIO(data), sheet_name=cfg.get("sheet", 0))
    elif fmt == "csv":
        df = pd.read_csv(io.BytesIO(data))
    else:
        raise ContractViolation([f"{source}: unsupported format {fmt!r}"])

    if cfg.get("header_trim"):
        df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]

    df = df.reset_index(drop=True)
    short = str(version)[:16]
    df.insert(0, "__row_key", [f"{short}:{i + 1}" for i in range(len(df))])
    df.insert(0, "__source", source)

    if columns:
        keep = ["__source", "__row_key"] + [c for c in columns if c in df.columns]
        df = df[keep]
    return df


@dataclass
class LocalFileDataSource:
    """DataSourceAdapter (orchestrator/adapters/protocols.py) backed by files on
    local disk / an attached Volume path. `sources` is the parsed contract.yaml
    `sources` mapping: {name: {format, sheet?, file, header_trim, columns}}."""

    root_dir: str | Path
    sources: dict[str, dict]

    def _cfg(self, source: str) -> dict:
        if source not in self.sources:
            raise ContractViolation([f"unknown source: {source!r}"])
        return self.sources[source]

    def _path(self, source: str) -> Path:
        cfg = self._cfg(source)
        return Path(self.root_dir) / cfg["file"]

    def resolve_version(self, source: str) -> str:
        path = self._path(source)
        if not path.is_file():
            raise ContractViolation([f"{source}: file not found: {path}"])
        return sha256_file(path)

    def resolve_source_versions(self, sources: list[str]) -> dict[str, str]:
        return {name: self.resolve_version(name) for name in sources}

    def read_population(
        self,
        source: str,
        *,
        version: int | str,
        columns: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        cfg = self._cfg(source)
        path = self._path(source)
        # N9: read the file's bytes ONCE, hash that buffer, and parse from the
        # SAME buffer -- two separate reads (hash the path, then re-open it to
        # parse) is a TOCTOU gap: the file on disk could change between the two
        # reads, so the hash recorded as this run's provenance would not
        # actually describe the bytes that were parsed.
        data = path.read_bytes()
        actual_version = hashlib.sha256(data).hexdigest()
        if actual_version != version:
            raise SourceVersionMismatch(source, str(version), actual_version)

        # `filters` is accepted (matching the DataSourceAdapter Protocol so a SQL
        # implementation can push filtering down) but a local file read is cheap
        # enough that filtering is left to orchestrator.populations after read.
        return parse_source_bytes(data, source, version, cfg, columns)

    def row_count(self, source: str, *, version: str | None = None) -> int:
        v = version or self.resolve_version(source)
        return len(self.read_population(source, version=v))

    def column_stats(
        self,
        source: str,
        *,
        version: str | None = None,
        amount_column: str | None = None,
        date_column: str | None = None,
    ) -> dict:
        # A SEPARATE read (re-parsed from the file's bytes, same as row_count
        # above) rather than reusing the engine's own in-memory population --
        # this is the independent side of G6's amount/date reconciliation
        # (CLAUDE.md §5 G6, P2/P3 gate review item 2).
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
        df = self.read_population(source, version=version)
        return pandas_profile_columns(
            df, max_distinct=max_distinct, min_count=min_count,
            audit_period=audit_period, audit_timezone=audit_timezone,
        )

    def distinct_count(self, source: str, *, version: int | str, columns: list[str]) -> int:
        df = self.read_population(source, version=version, columns=columns)
        return pandas_distinct_count(df, columns)
