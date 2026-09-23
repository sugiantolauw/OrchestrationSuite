from __future__ import annotations

import pandas as pd

from orchestrator.primitives.common import (
    LIMIT_SCHEMA,
    METRICS_PROPERTY_SCHEMA,
    PrimitiveContext,
    PrimitiveParamsError,
    PrimitiveResult,
    build_metrics,
    direction_mask,
    flags_from_rows,
    keyed_unit_id,
    resolve_limit_spec,
    row_condition_mask,
)

PARAMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "population": {"type": "string"},
        "column": {"type": "string"},
        "limit": LIMIT_SCHEMA,
        "direction": {"type": "string", "enum": ["above", "below", "at_or_above", "at_or_below"]},
        "group_by": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "aggregate": {"type": "string", "enum": ["sum"]},
        "exclude": {
            "type": "object",
            "properties": {
                "column": {"type": "string"},
                "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"]},
                "value": {},
            },
            "required": ["column", "op"],
            "additionalProperties": False,
        },
        "flag": {"type": "string"},
        "metrics": METRICS_PROPERTY_SCHEMA,
    },
    "required": ["population", "column", "limit", "direction"],
    "additionalProperties": False,
}


def run(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    population = ctx.population(params["population"])
    df = population.df.copy()
    column = params["column"]
    direction = params["direction"]
    limit_spec = resolve_limit_spec(ctx, params["limit"])
    flag = params.get("flag", "RF_THRESHOLD_EXCEEDANCE")

    excluded_count = 0
    if "exclude" in params:
        mask = row_condition_mask(df, params["exclude"])
        excluded_count = int(mask.sum())
        df = df[~mask].reset_index(drop=True)

    group_by = params.get("group_by")
    if group_by:
        if params.get("aggregate", "sum") != "sum":
            raise PrimitiveParamsError("threshold_exceedance: only 'aggregate: sum' is supported")

        grouped = df.groupby(group_by, dropna=False)[column].sum().reset_index()
        grouped = grouped.rename(columns={column: "__agg_value"})

        if limit_spec["kind"] == "column":
            limit_col = limit_spec["column"]
            first_limits = df.groupby(group_by, dropna=False)[limit_col].first().reset_index()
            grouped = grouped.merge(first_limits, on=group_by, how="left")
            limits = grouped[limit_col]
        else:
            limits = pd.Series(limit_spec["value"], index=grouped.index)

        exceed_mask = direction_mask(grouped["__agg_value"], limits, direction)
        excess = (grouped["__agg_value"] - limits).clip(lower=0)

        group_df = grouped[exceed_mask].copy()
        group_df["__excess"] = excess[exceed_mask]
        group_df[column] = group_df["__agg_value"]

        group_ids = group_df[group_by].apply(lambda r: keyed_unit_id("thr", tuple(r)), axis=1)
        group_df = group_df.assign(group_id=group_ids.to_numpy())

        row_group_ids = df[group_by].apply(lambda r: keyed_unit_id("thr", tuple(r)), axis=1)
        row_mask = row_group_ids.isin(set(group_df["group_id"]))
        row_df = df[row_mask].copy()
        row_group_ids_flagged = row_group_ids[row_mask]

        flags = flags_from_rows(row_df, flag=flag, group_ids=row_group_ids_flagged)
        scored_units = group_df["group_id"].tolist()
        excess_total = float(group_df["__excess"].sum())
    else:
        limits = pd.Series(limit_spec["value"], index=df.index) if limit_spec["kind"] == "threshold" else df[limit_spec["column"]]
        exceed_mask = direction_mask(df[column], limits, direction)
        row_df = df[exceed_mask].copy()
        excess_series = (df[column] - limits).clip(lower=0)
        row_df["__excess"] = excess_series[exceed_mask]
        group_df = None
        flags = flags_from_rows(row_df, flag=flag)
        scored_units = row_df["__row_key"].tolist()
        excess_total = float(row_df["__excess"].sum())

    metrics = build_metrics(
        params.get("metrics", {}),
        population=population,
        default_columns=[column],
        grain=",".join(group_by) if group_by else "row",
        row_df=row_df,
        group_df=group_df,
        scored_units=scored_units,
        values={
            "population_size": population.rows,
            "excess": excess_total,
            "excluded_count": excluded_count,
        },
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)
