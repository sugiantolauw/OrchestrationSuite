from __future__ import annotations

import re

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
        "allowed_values": {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}, "minItems": 1},
                {
                    "type": "object",
                    "properties": {"ref": {"type": "string"}},
                    "required": ["ref"],
                    "additionalProperties": False,
                },
            ]
        },
        "negate": {"type": "boolean"},
        "match": {"type": "string", "enum": ["exact", "whole_word_upper"]},
        "flag": {"type": "string"},
        "metrics": METRICS_PROPERTY_SCHEMA,
    },
    "required": ["population", "column", "allowed_values", "negate", "match"],
    "additionalProperties": False,
}


def _resolve_allowed(ctx: PrimitiveContext, spec) -> list[str]:
    if isinstance(spec, dict) and "ref" in spec:
        ref = spec["ref"]
        if ref not in ctx.references:
            raise PrimitiveParamsError(f"unknown reference: {ref!r}")
        return list(ctx.references[ref])
    return list(spec)


def _whole_word_match(value, terms_upper: list[str]) -> bool:
    if pd.isna(value):
        return False
    up = str(value).upper()
    return any(re.search(rf"\b{re.escape(t)}\b", up) for t in terms_upper)


def run(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    population = ctx.population(params["population"])
    df = population.df.copy()
    column = params["column"]
    allowed = _resolve_allowed(ctx, params["allowed_values"])
    negate = params["negate"]
    match = params["match"]
    flag = params.get("flag", "RF_LIST_MEMBERSHIP")

    if match == "exact":
        member_mask = df[column].isin(allowed)
    elif match == "whole_word_upper":
        allowed_upper = [a.upper() for a in allowed]
        # .astype(bool): .apply() over an empty Series infers dtype=object (there
        # are no results to infer bool from), and indexing a DataFrame with an
        # empty object-dtype mask silently drops every column rather than every
        # row -- an empty population must produce an empty *result*, not a
        # frame missing __row_key.
        member_mask = df[column].apply(lambda v: _whole_word_match(v, allowed_upper)).astype(bool)
    else:
        raise PrimitiveParamsError(f"unknown match mode: {match!r}")

    exc_mask = ~member_mask if negate else member_mask
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
