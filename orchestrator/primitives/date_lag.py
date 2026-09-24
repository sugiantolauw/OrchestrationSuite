from __future__ import annotations

import pandas as pd

from orchestrator.primitives.common import (
    LIMIT_SCHEMA,
    METRICS_PROPERTY_SCHEMA,
    PrimitiveContext,
    PrimitiveResult,
    build_metrics,
    direction_mask,
    flags_from_rows,
    resolve_limit_spec,
)

DESCRIPTION = (
    "Flags rows where the number of days between two date columns is above, "
    "below, at-or-above or at-or-below a threshold."
)

PARAMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "population": {"type": "string"},
        "start_column": {"type": "string"},
        "end_column": {"type": "string"},
        "threshold": LIMIT_SCHEMA,
        "direction": {"type": "string", "enum": ["above", "below", "at_or_above", "at_or_below"]},
        "flag": {"type": "string"},
        "metrics": METRICS_PROPERTY_SCHEMA,
    },
    "required": ["population", "start_column", "end_column", "threshold", "direction"],
    "additionalProperties": False,
}


def run(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    population = ctx.population(params["population"])
    df = population.df.copy()
    start_col = params["start_column"]
    end_col = params["end_column"]
    direction = params["direction"]
    limit_spec = resolve_limit_spec(ctx, params["threshold"], name="threshold")
    flag = params.get("flag", "RF_DATE_LAG")

    lag_days = (pd.to_datetime(df[end_col]) - pd.to_datetime(df[start_col])).dt.days
    df["__lag_days"] = lag_days

    limits = pd.Series(limit_spec["value"], index=df.index) if limit_spec["kind"] == "threshold" else df[limit_spec["column"]]
    exc_mask = direction_mask(df["__lag_days"], limits, direction)

    row_df = df[exc_mask].copy()
    flags = flags_from_rows(row_df, flag=flag)
    scored_units = row_df["__row_key"].tolist()

    metrics = build_metrics(
        params.get("metrics", {}),
        population=population,
        default_columns=["__lag_days"],
        grain="row",
        row_df=row_df,
        group_df=None,
        scored_units=scored_units,
        values={"population_size": population.rows},
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)
