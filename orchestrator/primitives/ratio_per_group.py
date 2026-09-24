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
)

DESCRIPTION = (
    "Computes a ratio between two numeric columns -- optionally aggregated "
    "per group -- and flags where it is above, below, at-or-above or "
    "at-or-below a limit: a fixed value, or one of several limits selected "
    "by another column's value."
)

PARAMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "population": {"type": "string"},
        "numerator_column": {"type": "string"},
        "denominator_column": {"type": "string"},
        "group_by": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "numerator_aggregate": {"type": "string", "enum": ["sum", "first"]},
        "direction": {"type": "string", "enum": ["above", "below", "at_or_above", "at_or_below"]},
        "limit": LIMIT_SCHEMA,
        "limit_selector_column": {"type": "string"},
        "limit_selector_aggregate": {"type": "string", "enum": ["any", "all", "first"]},
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


def _aggregate_selector(values: pd.Series, cases_order: list[str], aggregate: str):
    """Resolves ONE group's per-row `limit_selector_column` values down to the
    single case that decides its limit (B5). `cases_order` is `limit_cases`'
    declaration order, read as ascending precedence. 'first' (default,
    unchanged): the group's first row's raw value -- one attendee's status
    stands for the whole group, wrong for T3.3b (spec: "$80 if ANY attendee is
    external"). 'any': the highest-precedence declared case present anywhere in
    the group (declaring `external` after `internal` in limit_cases makes one
    external attendee select the external limit for the whole entry). 'all':
    the dual -- the lower-precedence case only if literally every row agrees;
    otherwise the highest-precedence case present, same as 'any'."""
    if aggregate == "first":
        return values.iloc[0]
    present = set(values.astype(str))
    ordered_present = [c for c in cases_order if c in present]
    if not ordered_present:
        return values.iloc[0]
    if aggregate == "any":
        return ordered_present[-1]
    if aggregate == "all":
        return ordered_present[0] if len(ordered_present) == 1 else ordered_present[-1]
    raise PrimitiveParamsError(f"unknown limit_selector_aggregate: {aggregate!r}")


def run(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    population = ctx.population(params["population"])
    df = population.df.copy()
    num_col = params["numerator_column"]
    den_col = params["denominator_column"]
    direction = params["direction"]
    flag = params.get("flag", "RF_RATIO_PER_GROUP")
    group_by = params.get("group_by")

    if len(df) == 0:
        # CLAUDE.md P2/P3 gate review item 6 (G10): `work.apply(..., axis=1)`
        # below, on an empty frame, returns an empty DataFrame rather than an
        # empty Series (pandas cannot infer the output shape without calling
        # the function at least once) -- the later `values > limits`
        # comparison then raises "Operands are not aligned" instead of
        # zero exceptions. Short-circuit before groupby/apply ever runs: an
        # empty population has zero exceptions, definitionally, on every
        # code path below.
        values = {"population_size": 0, "assessed": 0}
        for case in params.get("limit_cases", {}):
            values[f"count_{case}"] = 0
        metrics = build_metrics(
            params.get("metrics", {}), population=population,
            default_columns=[num_col, den_col], grain=",".join(group_by) if group_by else "row",
            row_df=df, group_df=None, scored_units=[], values=values,
        )
        return PrimitiveResult(metrics=metrics, flags=flags_from_rows(df, flag=flag), scored_units=[])

    if group_by:
        num_agg = params.get("numerator_aggregate", "sum")
        agg_kwargs = {"__num": (num_col, num_agg), "__den": (den_col, "first")}
        selector_col = params.get("limit_selector_column")
        work = df.groupby(group_by, dropna=False).agg(**agg_kwargs).reset_index()
        if selector_col:
            selector_aggregate = params.get("limit_selector_aggregate", "first")
            cases_order = list(params.get("limit_cases", {}).keys())
            selector_series = (
                df.groupby(group_by, dropna=False)[selector_col]
                .apply(lambda s: _aggregate_selector(s, cases_order, selector_aggregate))
                .reset_index(name="__selector")
            )
            work = work.merge(selector_series, on=group_by, how="left")
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
        group_ids = exceeded[group_by].apply(lambda r: keyed_unit_id("ratio", tuple(r)), axis=1)
        exceeded = exceeded.assign(group_id=group_ids.to_numpy())
        row_group_ids = df[group_by].apply(lambda r: keyed_unit_id("ratio", tuple(r)), axis=1)
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
