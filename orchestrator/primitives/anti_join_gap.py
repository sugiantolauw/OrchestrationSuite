from __future__ import annotations

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
        "left_population": {"type": "string"},
        "right_population": {"type": "string"},
        "left_keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "right_keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "mode": {"type": "string", "enum": ["anti", "semi"]},
        "flag": {"type": "string"},
        "metrics": METRICS_PROPERTY_SCHEMA,
    },
    "required": ["left_population", "right_population", "left_keys", "right_keys", "mode"],
    "additionalProperties": False,
}


def run(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    left = ctx.population(params["left_population"])
    right = ctx.population(params["right_population"])
    left_df = left.df.reset_index(drop=True)
    right_df = right.df.reset_index(drop=True)
    left_keys = params["left_keys"]
    right_keys = params["right_keys"]
    if len(left_keys) != len(right_keys):
        raise PrimitiveParamsError("anti_join_gap: left_keys and right_keys must have the same length")
    mode = params["mode"]
    flag = params.get("flag", "RF_ANTI_JOIN_GAP")

    right_key_df = right_df[right_keys].drop_duplicates()
    merged = left_df.merge(
        right_key_df, left_on=left_keys, right_on=right_keys, how="left", indicator="__merge"
    ).reset_index(drop=True)
    matched_mask = merged["__merge"] == "both"

    match_count = int(matched_mask.sum())
    match_rate = round(match_count / len(left_df) * 100, 1) if len(left_df) else None

    if mode == "anti":
        exc_mask = ~matched_mask
    else:
        exc_mask = matched_mask

    row_df = left_df[exc_mask.to_numpy()].copy()
    flags = flags_from_rows(row_df, flag=flag)
    scored_units = row_df["__row_key"].tolist()

    left_key_df = left_df[left_keys].drop_duplicates()
    right_merged = right_df.merge(
        left_key_df, left_on=right_keys, right_on=left_keys, how="left", indicator="__rmerge"
    ).reset_index(drop=True)
    right_unmatched_count = int((right_merged["__rmerge"] == "left_only").sum())

    metrics = build_metrics(
        params.get("metrics", {}),
        population=left,
        default_columns=left_keys,
        grain="row",
        row_df=row_df,
        group_df=None,
        scored_units=scored_units,
        values={
            "population_size": len(left_df),
            "match_rate": match_rate,
            "right_unmatched_count": right_unmatched_count,
        },
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)
