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
    group_id_from_key,
    resolve_limit_spec,
)

PARAMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "population": {"type": "string"},
        "numerator_column": {"type": "string"},
        "denominator_column": {"type": "string"},
        "group_by": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "direction": {"type": "string", "enum": ["above", "below", "at_or_above", "at_or_below"]},
        "limit": LIMIT_SCHEMA,
        "limit_selector_column": {"type": "string"},
        "limit_cases": {"type": "object", "additionalProperties": LIMIT_SCHEMA},
        "flag": {"type": "string"},
        "metrics": METRICS_PROPERTY_SCHEMA,
    },
    "required": ["population", "numerator_column", "denominator_column", "direction"],
    "additionalProperties": False,
    "oneOf": [
        {"required": ["limit"]},
        {"required": ["limit_selector_column", "limit_cases"]},
    ],
}


def _resolved_limit_value(spec: dict, row: pd.Series):
    if spec["kind"] == "threshold":
        return spec["value"]
    return row[spec["column"]]


def run(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    population = ctx.population(params["population"])
    df = population.df.copy()
    num_col = params["numerator_column"]
    den_col = params["denominator_column"]
    direction = params["direction"]
    flag = params.get("flag", "RF_RATIO_PER_GROUP")
    group_by = params.get("group_by")

    if group_by:
        agg_kwargs = {"__num": (num_col, "sum"), "__den": (den_col, "first")}
        selector_col = params.get("limit_selector_column")
        if selector_col:
            agg_kwargs["__selector"] = (selector_col, "first")
        work = df.groupby(group_by, dropna=False).agg(**agg_kwargs).reset_index()
    else:
        work = df.copy()
        work["__num"] = work[num_col]
        work["__den"] = work[den_col]
        if params.get("limit_selector_column"):
            work["__selector"] = work[params["limit_selector_column"]]

    work["__ratio"] = work["__num"] / work["__den"]

    if "limit_selector_column" in params:
        cases = {k: resolve_limit_spec(ctx, v, name=f"limit_cases.{k}") for k, v in params["limit_cases"].items()}
        limits = work.apply(
            lambda row: _resolved_limit_value(_case_spec(cases, row["__selector"]), row), axis=1
        )
    else:
        limit_spec = resolve_limit_spec(ctx, params["limit"])
        limits = (
            pd.Series(limit_spec["value"], index=work.index)
            if limit_spec["kind"] == "threshold"
            else work[limit_spec["column"]]
        )

    exc_mask = direction_mask(work["__ratio"], limits, direction)
    exceeded = work[exc_mask].copy()

    values = {"population_size": population.rows}

    if group_by:
        group_ids = exceeded[group_by].apply(lambda r: group_id_from_key(tuple(r)), axis=1)
        exceeded = exceeded.assign(group_id=group_ids.to_numpy())
        row_group_ids = df[group_by].apply(lambda r: group_id_from_key(tuple(r)), axis=1)
        row_mask = row_group_ids.isin(set(exceeded["group_id"]))
        row_df = df[row_mask].copy()
        flags = flags_from_rows(row_df, flag=flag, group_ids=row_group_ids[row_mask])
        scored_units = exceeded["group_id"].tolist()
        group_df = exceeded
        assessed = len(work)
    else:
        row_df = exceeded
        flags = flags_from_rows(row_df, flag=flag)
        scored_units = row_df["__row_key"].tolist()
        group_df = None
        assessed = len(work)

    values["assessed"] = assessed
    if "limit_selector_column" in params:
        for case in params["limit_cases"]:
            values[f"count_{case}"] = int((exc_mask & (work["__selector"] == case)).sum())

    metrics = build_metrics(
        params.get("metrics", {}),
        population=population,
        default_columns=[num_col, den_col],
        grain=",".join(group_by) if group_by else "row",
        row_df=row_df,
        group_df=group_df,
        scored_units=scored_units,
        values=values,
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)


def _case_spec(cases: dict, selector_value) -> dict:
    key = str(selector_value)
    if key not in cases:
        raise PrimitiveParamsError(f"no limit_case for selector value {key!r}")
    return cases[key]
