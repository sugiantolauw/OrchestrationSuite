"""Resolves the Explorer wire schema's `unit: "currency"` placeholder to a
real ISO-4217 code (CLAUDE.md §0.5; docs/specs/P6_P8_explorer_llm_design.md
§4.6 "Units" / §4.7 V-C1). A pure, profile-driven lookup with no side
effects -- `validate.py`'s V-C1 and `materialise.py` both call this SAME
function so a test's resolved currency can never drift between "what made
it valid" and "what got written into the Skill"."""

from __future__ import annotations


def _is_evidenced(col: dict) -> bool:
    if col.get("pii"):
        return False
    values = col.get("values")
    if not values or len(values) != 1:
        return False
    if col.get("suppressed_values"):
        return False
    return isinstance(values[0].get("value"), str)


def resolve_currency_unit(source: str, profile: dict) -> str | None:
    """`profile` is the Explorer profile's `sources` mapping (§4.3:
    `{source_name: {"row_count", "null_counts", "columns": [...]}}`).
    Returns the evidenced ISO-4217 code when `source` has exactly one
    non-PII `semantic_type == "currency_code"` column that is itself
    single-valued (one profiled value, no suppressed values) -- `None`
    otherwise (V-C1: "the test is invalid" is the caller's job to raise,
    not this function's).

    Independent review round 2 (BUG-EXPLORER-2, validator false positive):
    the requirement used to be "exactly one currency_code column, full
    stop" -- live 2026-09-25 against the real SKILL-001 source data, a
    genuinely reimbursement-ready source (single-valued "Reimbursement
    Currency", CLAUDE.md §0.5's own accepted decision 6) ALSO carries a
    second, unrelated "Transaction Currency" column (per-line FX currency,
    19 distinct values) that the same name-and-value heuristic also
    classifies `currency_code` -- so no test on this source could EVER be
    valid, regardless of what the planner wrote. The requirement is now
    "exactly one column among the candidates is itself single-valued and
    evidenced": a second currency-shaped column that is not itself usable
    (many distinct values) no longer blocks the one that is, but two
    single-valued columns that genuinely disagree still correctly resolve
    to nothing -- real ambiguity, not noise."""
    src = profile.get(source)
    if not src:
        return None
    candidates = [c for c in src.get("columns", []) if c.get("semantic_type") == "currency_code"]
    evidenced = [c for c in candidates if _is_evidenced(c)]
    if len(evidenced) != 1:
        return None
    return evidenced[0]["values"][0]["value"].strip().upper()
