from __future__ import annotations

import re
from datetime import datetime, timezone

# Canonical timestamp format used everywhere in the ledger (CLAUDE.md §0.5, §9C):
# ISO-8601 UTC with microseconds and a literal 'Z' offset. Dates alone use YYYY-MM-DD.
_CANONICAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
