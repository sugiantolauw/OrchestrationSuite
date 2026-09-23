"""SKILL-001 (T&E ExCo) escape hatches: T6.1a (approver review sufficiency, a
custom primitive per CLAUDE.md §4.3 -- it genuinely does not fit any of the 8
generic primitives), plus the small custom derivations plan.yaml needs because
they require conditional branching a declarative op cannot express:
  - cross_approver_numeric: coerces "Employee ID(Cross Change Approver)" to a
    numeric id, counting (never defaulting) the non-numeric "Concur System"
    sentinel and any other unparseable value.
  - country_from_city_or_currency: docs/specs/SKILL-001_test_specification.md
    T6.1d's 3-step country rule (city lookup, else currency lookup, else
    unmapped) -- a declarative `lookup`+coalesce cannot gate the currency
    fallback on "city absent" specifically without this branching.
"""

from __future__ import annotations

import pandas as pd

from orchestrator.primitives.common import (
    Metric,
    PrimitiveContext,
    PrimitiveResult,
    flags_from_rows,
)


T6_1A_PARAMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "population": {"type": "string"},
        "receipt_viewed_columns": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "instant_minutes_column": {"type": "string"},
        "instant_threshold": {
            "type": "object",
            "properties": {"threshold": {"type": "string"}},
            "required": ["threshold"],
            "additionalProperties": False,
        },
        "receipts_viewed_date_column": {"type": "string"},
        "approver_received_date_column": {"type": "string"},
        "approved_datetime_column": {"type": "string"},
        "approver_id_column": {"type": "string"},
        "approver_name_column": {"type": "string"},
        "flag": {"type": "string"},
    },
    "required": [
        "population",
        "receipt_viewed_columns",
        "instant_minutes_column",
        "instant_threshold",
        "receipts_viewed_date_column",
        "approver_received_date_column",
        "approved_datetime_column",
        "approver_id_column",
        "approver_name_column",
    ],
    "additionalProperties": False,
}


def t6_1a_approver_review(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    """T6.1a -- approver review sufficiency (spec §1 T6.1a, CLAUDE.md build brief
    item B2). Not expressible with a generic primitive: it combines an
    OR-of-two-columns "receipt viewed" flag, an "instant" flag gated on that same
    viewed flag, a worst-case flag that is a distinct comparison entirely, and a
    per-approver breakdown table, in one pass over one population.

    Exception unit = approval step where receipts were NOT viewed, OR, where
    viewed, approval was instant (Minutes of Approval from Receipt View <
    thresholds.instant_approval_minutes). "Viewed" is the two-column OR
    (Report Receipt Viewed == 'Yes' OR All Entry Receipts Viewed == 'Yes') --
    Receipts Viewed Date is not used to gate this, per the build brief: it is
    evidence, not the definition of "viewed". Worst case (not viewed AND
    approved within the instant threshold of Approver Received Date) is a
    separate, always-defined metric -- it is never the exception definition."""
    population = ctx.population(params["population"])
    df = population.df.copy()

    viewed_cols = params["receipt_viewed_columns"]
    receipt_viewed = pd.Series(False, index=df.index)
    for col in viewed_cols:
        receipt_viewed = receipt_viewed | (df[col].astype(str).str.strip().str.upper() == "YES")

    minutes_col = params["instant_minutes_column"]
    instant_threshold = ctx.thresholds[params["instant_threshold"]["threshold"]]["value"]
    receipts_viewed_date_col = params["receipts_viewed_date_column"]
    # "Instant" is assessed only on steps where a receipt was actually viewed
    # (the two-column OR above) -- Minutes of Approval from Receipt View is
    # undefined otherwise, so those steps are never counted as instant (never
    # defaulted to either True or False by inference; simply excluded).
    instant = receipt_viewed & (df[minutes_col] < instant_threshold)

    # The exception: not viewed, or (viewed and instant). Disjoint by
    # construction, since `instant` can only be True when `receipt_viewed` is.
    exception = (~receipt_viewed) | instant

    # "Worst case" (spec T6.1a) is a distinct, always-defined comparison:
    # approved within the same minute it was received by the approver
    # (Approver Received Date -> Approved Date/Time), independent of whether a
    # receipt was ever viewed. It stays a separate metric, never the exception.
    received_col = params["approver_received_date_column"]
    approved_col = params["approved_datetime_column"]
    minutes_since_received = (df[approved_col] - df[received_col]).dt.total_seconds() / 60.0
    approved_within_a_minute_of_receipt = minutes_since_received < instant_threshold
    worst_case = (~receipt_viewed) & approved_within_a_minute_of_receipt

    df["__receipt_viewed"] = receipt_viewed
    df["__instant"] = instant
    df["__exception"] = exception
    df["__worst_case"] = worst_case

    total_reports = len(df)
    approver_id_col = params["approver_id_column"]
    approver_name_col = params["approver_name_column"]
    approver_count = int(df[approver_id_col].nunique()) if total_reports else 0
    # CLAUDE.md P2/P3 gate review item 6 (G10): a percentage over zero
    # reports is undefined, not zero -- 0.0 would read identically to
    # "every report reviewed properly", the opposite of "nothing to assess".
    no_receipt_pct = round(float((~receipt_viewed).mean() * 100), 1) if total_reports else None
    instant_pct = round(float(instant.mean() * 100), 1) if total_reports else None
    worst_case_n = int(worst_case.sum())
    worst_case_pct = round(worst_case_n / total_reports * 100, 1) if total_reports else None

    approver_detail = []
    if total_reports:
        grouped = df.groupby(approver_name_col, as_index=False).agg(
            reports=(approver_name_col, "count"),
            receipt_viewed_pct=("__receipt_viewed", lambda s: round(float(s.mean() * 100), 1)),
            instant_pct=("__instant", lambda s: round(float(s.mean() * 100), 1)),
        )
        grouped["no_receipt_pct"] = (100 - grouped["receipt_viewed_pct"]).round(1)
        approver_detail = grouped.sort_values("receipt_viewed_pct", ascending=False).to_dict("records")

    flag = params.get("flag", "RF_APR_InsufficientReview")
    row_df = df[exception].copy()
    flags = flags_from_rows(row_df, flag=flag)
    scored_units = row_df["__row_key"].tolist()

    source_ref = {
        "sources": [{"name": population.source, "version": population.source_version}],
        "columns": list(viewed_cols) + [minutes_col, receipts_viewed_date_col],
        "grain": "approval_step",
        "population": population.name,
    }

    def m(value, unit):
        return Metric(value=value, unit=unit, source_ref=source_ref).to_dict()

    metrics = {
        "approver_total_reports": m(total_reports, "count"),
        "approver_count": m(approver_count, "count"),
        "approver_no_receipt_pct": m(no_receipt_pct, "%"),
        "approver_instant_pct": m(instant_pct, "%"),
        "approver_worst_case_n": m(worst_case_n, "count"),
        "approver_worst_case_pct": m(worst_case_pct, "%"),
        "approver_detail": m(approver_detail, "table"),
    }
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)


def cross_approver_numeric(df: pd.DataFrame, params: dict, ctx) -> tuple:
    col = params.get("column", "Employee ID(Cross Change Approver)")
    numeric = pd.to_numeric(df[col], errors="coerce")
    non_numeric = numeric.isna() & df[col].notna()
    return numeric, {"cross_approver_non_numeric_rows": int(non_numeric.sum())}


def attendee_external_classification(df: pd.DataFrame, params: dict, ctx) -> tuple:
    """T3.3b's internal/external attendee rule (spec T3.3b, [PENDING]): external =
    Company or External ID present. DATA-QUALITY NOTE (P2b-1): on the real
    synthetic_data/ files, Company is null on every row and External ID is
    non-null on every row, so this rule classifies 100% of entries external and
    never exercises the $40 internal threshold. Recorded here rather than
    silently working around it -- see the P2b-1 report."""
    company_col = params.get("company_column", "Company")
    external_id_col = params.get("external_id_column", "External ID")
    is_external = df[company_col].notna() | df[external_id_col].notna()
    classification = is_external.map({True: "external", False: "internal"})
    return classification, {}


def _lookup_map(table, key_field: str, value_field: str) -> dict:
    if isinstance(table, pd.DataFrame):
        return dict(zip(table[key_field], table[value_field]))
    return {row[key_field]: row[value_field] for row in table}


def country_from_city_or_currency(df: pd.DataFrame, params: dict, ctx) -> tuple:
    city_col = params.get("city_column", "City/Location")
    currency_col = params.get("currency_column", "Transaction Currency")
    city_table = ctx.references[params.get("city_table", "city_country")]
    currency_table = ctx.references[params.get("currency_table", "currency_country")]

    city_map = _lookup_map(city_table, "city", "country")
    currency_map = _lookup_map(currency_table, "currency", "country")

    city_present = df[city_col].notna()
    country = pd.Series(pd.NA, index=df.index, dtype=object)
    country.loc[city_present] = df.loc[city_present, city_col].map(city_map)
    country.loc[~city_present] = df.loc[~city_present, currency_col].map(currency_map)

    unmapped = country.isna()
    counters = {"t61d_unmapped_rows": int(unmapped.sum())}
    return country, counters


CUSTOM_PRIMITIVES = {
    "t6_1a_approver_review": {
        "params_schema": T6_1A_PARAMS_SCHEMA,
        "run": t6_1a_approver_review,
        "produces_metrics": [
            "approver_total_reports",
            "approver_count",
            "approver_no_receipt_pct",
            "approver_instant_pct",
            "approver_worst_case_n",
            "approver_worst_case_pct",
            "approver_detail",
        ],
    },
}

CUSTOM_DERIVATIONS = {
    "cross_approver_numeric": cross_approver_numeric,
    "attendee_external_classification": attendee_external_classification,
    "country_from_city_or_currency": country_from_city_or_currency,
}
