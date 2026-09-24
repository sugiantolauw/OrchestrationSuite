from __future__ import annotations

import pandas as pd

from orchestrator.primitives.common import (
    METRICS_PROPERTY_SCHEMA,
    PrimitiveContext,
    PrimitiveResult,
    build_metrics,
    flags_from_rows,
    keyed_unit_id,
)

DESCRIPTION = (
    "Groups rows by a key and flags every row in a group with more than one "
    "member -- exact-key duplicates, with an optional sub-flag for a group "
    "that mixes an out-of-pocket payment with a corporate-card payment."
)

PARAMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "population": {"type": "string"},
        "key_columns": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "amount_column": {"type": "string"},
        "exclude_within": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "payment_column": {"type": "string"},
        "oop_values": {"type": "array", "items": {"type": "string"}},
        "card_values": {"type": "array", "items": {"type": "string"}},
        "flag": {"type": "string"},
        "flag_oop_card": {"type": "string"},
        "metrics": METRICS_PROPERTY_SCHEMA,
    },
    "required": ["population", "key_columns"],
    "additionalProperties": False,
}


def run(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    population = ctx.population(params["population"])
    df = population.df.copy()
    key_cols = params["key_columns"]
    amount_col = params.get("amount_column")
    exclude_within = params.get("exclude_within")
    payment_col = params.get("payment_column")
    oop_values = {v.lower() for v in params.get("oop_values", [])}
    card_values = {v.lower() for v in params.get("card_values", [])}
    flag = params.get("flag", "RF_DUPLICATE")
    flag_oop_card = params.get("flag_oop_card")

    row_group_id = pd.Series([None] * len(df), index=df.index, dtype=object)
    row_oop_card_group_id = pd.Series([None] * len(df), index=df.index, dtype=object)
    group_records: list[dict] = []
    oop_card_groups = 0
    extra_amount_total = 0.0

    for key, group in df.groupby(key_cols, dropna=False):
        if len(group) <= 1:
            continue
        key_tuple = key if isinstance(key, tuple) else (key,)

        if exclude_within:
            distinct = group[exclude_within].drop_duplicates()
            if len(distinct) <= 1:
                # Every line shares the same exclude_within value (e.g. Parent Key):
                # these are itemisations of one entry, not independent duplicates.
                continue

        gid = keyed_unit_id("dup", key_tuple)
        row_group_id.loc[group.index] = gid

        rec = dict(zip(key_cols, key_tuple))
        rec["group_id"] = gid
        rec["__lines"] = len(group)
        if amount_col and amount_col in group.columns:
            amounts = group[amount_col].astype(float)
            extra = float(amounts.sum() - amounts.iloc[0])
            rec["__extra_amount"] = extra
            extra_amount_total += extra
        group_records.append(rec)

        if payment_col:
            payments = group[payment_col].astype(str).str.lower()
            has_oop = payments.isin(oop_values).any() if oop_values else False
            has_card = payments.isin(card_values).any() if card_values else False
            if has_oop and has_card:
                oop_card_groups += 1
                if flag_oop_card:
                    row_oop_card_group_id.loc[group.index] = gid

    group_df = pd.DataFrame(group_records)
    row_mask = row_group_id.notna()
    row_df = df[row_mask].copy()
    flags = flags_from_rows(row_df, flag=flag, group_ids=row_group_id[row_mask])

    if flag_oop_card:
        # A second, row-level flag column (CLAUDE.md §4.2 -- `execute` writes the
        # row-level RF_* flags the existing UI's BREACH_FLAG_GROUPS reads) marking
        # just the rows belonging to a duplicate group that mixes an
        # out-of-pocket line with a corporate-card line -- a strict subset of the
        # main duplicate flag, not a separate scoring unit.
        oop_row_mask = row_oop_card_group_id.notna()
        oop_row_df = df[oop_row_mask].copy()
        oop_flags = flags_from_rows(
            oop_row_df, flag=flag_oop_card, group_ids=row_oop_card_group_id[oop_row_mask]
        )
        flags = pd.concat([flags, oop_flags], ignore_index=True)

    scored_units = group_df["group_id"].tolist() if len(group_df) else []

    metrics = build_metrics(
        params.get("metrics", {}),
        population=population,
        default_columns=key_cols,
        grain="claim_group",
        row_df=row_df,
        group_df=group_df,
        scored_units=scored_units,
        values={
            "population_size": population.rows,
            "duplicate_amount": extra_amount_total,
            "oop_card_groups": oop_card_groups,
        },
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)
