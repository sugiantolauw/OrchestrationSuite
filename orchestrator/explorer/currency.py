"""Resolves the Explorer wire schema's `unit: "currency"` placeholder to a
real ISO-4217 code (CLAUDE.md §0.5; docs/specs/P6_P8_explorer_llm_design.md
§4.6 "Units" / §4.7 V-C1). A pure, profile-driven lookup with no side
effects -- `validate.py`'s V-C1 and `materialise.py` both call this SAME
function so a test's resolved currency can never drift between "what made
it valid" and "what got written into the Skill"."""

from __future__ import annotations


def resolve_currency_unit(source: str, profile: dict) -> str | None:
    """`profile` is the Explorer profile's `sources` mapping (§4.3:
    `{source_name: {"row_count", "null_counts", "columns": [...]}}`).
    Returns the evidenced ISO-4217 code when `source` has exactly one
    non-PII `semantic_type == "currency_code"` column with exactly one
    profiled value and no suppressed values -- `None` otherwise (V-C1: "the
    test is invalid" is the caller's job to raise, not this function's)."""
    src = profile.get(source)
    if not src:
        return None
    currency_columns = [c for c in src.get("columns", []) if c.get("semantic_type") == "currency_code"]
    if len(currency_columns) != 1:
        return None
    col = currency_columns[0]
    if col.get("pii"):
        return None
    values = col.get("values")
    if not values or len(values) != 1:
        return None
    if col.get("suppressed_values"):
        return None
    value = values[0].get("value")
    if not isinstance(value, str):
        return None
    return value.strip().upper()
