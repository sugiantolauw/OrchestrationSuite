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


def _evidenced_currency_column(source: str, profile: dict) -> dict | None:
    """The ONE profiled column (§4.3 columns shape) `resolve_currency_unit`
    and `resolve_currency_column` both resolve to, or `None` when there
    isn't exactly one -- shared so the two can never independently pick a
    different column for the same source (see resolve_currency_unit's own
    docstring for why "exactly one CANDIDATE column" is not the same
    requirement as "exactly one EVIDENCED column")."""
    src = profile.get(source)
    if not src:
        return None
    candidates = [c for c in src.get("columns", []) if c.get("semantic_type") == "currency_code"]
    evidenced = [c for c in candidates if _is_evidenced(c)]
    if len(evidenced) != 1:
        return None
    return evidenced[0]


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
    col = _evidenced_currency_column(source, profile)
    if col is None:
        return None
    return col["values"][0]["value"].strip().upper()


def resolve_currency_column(source: str, profile: dict) -> str | None:
    """The NAME of the one column resolve_currency_unit's code came from,
    or `None` on the same terms. Live regression companion to the
    resolve_currency_unit fix above (independent review round 2): once a
    second, non-evidenced currency-shaped column stopped blocking
    resolution, materialise.py's own "which column does the contract's
    allowed_values constraint go on" logic -- previously just the LAST
    semantic_type == "currency_code" column in profile order, with no
    regard for which one was actually evidenced -- could pick the WRONG
    one (the multi-valued "noise" column) and write an `allowed_values:
    [AUD]` constraint onto it, failing every real run at G7/contract
    validation with real per-line FX values. Sharing this resolution with
    resolve_currency_unit is what makes that impossible: the contract's
    constrained column and the code it is constrained to always come from
    the SAME evidenced column."""
    col = _evidenced_currency_column(source, profile)
    return col["name"] if col is not None else None
