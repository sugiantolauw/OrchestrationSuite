from __future__ import annotations

import pandas as pd

from orchestrator.primitives.common import (
    METRICS_PROPERTY_SCHEMA,
    PrimitiveContext,
    PrimitiveParamsError,
    PrimitiveResult,
    build_metrics,
    flags_from_rows,
)

PARAMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "population": {"type": "string"},
        "column": {"type": "string"},
        "condition": {"type": "string", "enum": ["is_null", "is_blank", "not_equals"]},
        "value": {},
        "flag": {"type": "string"},
        "metrics": METRICS_PROPERTY_SCHEMA,
    },
    "required": ["population", "column", "condition"],
    "additionalProperties": False,
}


def _missing_mask(series: pd.Series, condition: str, value) -> pd.Series:
    if condition == "is_null":
        return series.isna()
    if condition == "is_blank":
        return series.isna() | series.map(lambda v: isinstance(v, str) and v.strip() == "")
    if condition == "not_equals":
        return series != value
    raise PrimitiveParamsError(f"unknown condition: {condition!r}")


def run(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    population = ctx.population(params["population"])
    df = population.df.copy()
    column = params["column"]
    condition = params["condition"]
    flag = params.get("flag", "RF_ATTRIBUTE_MISSING")

    exc_mask = _missing_mask(df[column], condition, params.get("value"))
    row_df = df[exc_mask].copy()
    flags = flags_from_rows(row_df, flag=flag)
    scored_units = row_df["__row_key"].tolist()

    metrics = build_metrics(
        params.get("metrics", {}),
        population=population,
        default_columns=[column],
        grain="row",
        row_df=row_df,
        group_df=None,
        scored_units=scored_units,
        values={"population_size": population.rows},
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)
