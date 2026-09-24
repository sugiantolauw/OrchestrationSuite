from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd

# Canonical timestamp format used everywhere in the ledger (CLAUDE.md §0.5, §9C):
# ISO-8601 UTC with microseconds and a literal 'Z' offset. Dates alone use YYYY-MM-DD.
_CANONICAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def to_business_local(value, tz_name: str):
    """The ONE audit-period-timezone rule (CLAUDE.md §0.5, NN14; independent
    test-gap audit #13/H9): "the contract's declared timezone is the
    business calendar; naive source datetimes are interpreted in that
    timezone; timezone-aware ones (a UC TIMESTAMP comes back UTC) are
    converted to it". Applied identically on every source path -- callers
    pass either a naive/aware pandas Series (a whole column, e.g.
    orchestrator.adapters.datasource_uc._normalise_datetime_dtypes) or a
    scalar pandas.Timestamp/datetime (e.g. a column_stats() MIN/MAX result).

    A NAIVE value already represents local wall-clock time in `tz_name` (the
    file/CSV/XLSX reading path never attaches a timezone, so its naive
    values ARE the business calendar by construction) and is returned
    unchanged -- this is a no-op, not a "treat as UTC" step. A TIMEZONE-AWARE
    value (the SQL warehouse returns UC TIMESTAMP columns tz-aware, CLAUDE.md
    §0.5) is converted to `tz_name` via the IANA tz database (so a DST
    transition on the conversion date is handled correctly, never a fixed
    offset) and then stripped back to naive, so every source path produces
    the exact same wall-clock digits before any audit-period boundary
    comparison happens (orchestrator.populations._apply_filter's `between`
    op)."""
    if isinstance(value, pd.Series):
        if not pd.api.types.is_datetime64_any_dtype(value):
            return value
        if getattr(value.dt, "tz", None) is not None:
            return value.dt.tz_convert(tz_name).dt.tz_localize(None)
        return value
    if value is None:
        return value
    ts = value if isinstance(value, pd.Timestamp) else pd.Timestamp(value)
    if pd.isna(ts):
        return ts
    if ts.tzinfo is not None:
        return ts.tz_convert(tz_name).tz_localize(None)
    return ts


def utc_now() -> str:
    return normalise_ts(datetime.now(timezone.utc))


def normalise_ts(value) -> str:
    """Accepts a datetime (naive treated as UTC) or an ISO-ish string, returns the
    canonical YYYY-MM-DDTHH:MM:SS.ffffffZ string. Never slices a string -- always
    parses it, so an already-canonical string round-trips exactly and a non-canonical
    one (e.g. a Delta driver's own ISO format, or a '+00:00' offset) is normalised
    rather than truncated."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"normalise_ts: unsupported type {type(value).__name__}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def is_canonical_ts(value: str) -> bool:
    return isinstance(value, str) and bool(_CANONICAL_RE.match(value))


def is_canonical_date(value: str) -> bool:
    return isinstance(value, str) and bool(_DATE_RE.match(value))
