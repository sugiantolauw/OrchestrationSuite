from __future__ import annotations

import pandas as pd

from orchestrator.primitives.common import (
    METRICS_PROPERTY_SCHEMA,
    THRESHOLD_ONLY_SCHEMA,
    PrimitiveContext,
    PrimitiveResult,
    build_metrics,
    flags_from_rows,
    group_id_from_key,
    resolve_scalar_threshold,
)

PARAMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "population": {"type": "string"},
        "group_keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "date_column": {"type": "string"},
        "amount_column": {"type": "string"},
        "window_days": THRESHOLD_ONLY_SCHEMA,
        "aggregate_threshold": THRESHOLD_ONLY_SCHEMA,
        "max_line": THRESHOLD_ONLY_SCHEMA,
        "flag_same_day": {"type": "string"},
        "flag_window": {"type": "string"},
        "metrics": METRICS_PROPERTY_SCHEMA,
    },
    "required": ["population", "group_keys", "date_column", "amount_column", "window_days", "aggregate_threshold"],
    "additionalProperties": False,
}


def _find_union(parent: dict, x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union(parent: dict, a, b) -> None:
    ra, rb = _find_union(parent, a), _find_union(parent, b)
    if ra != rb:
        parent[ra] = rb


def run(ctx: PrimitiveContext, params: dict) -> PrimitiveResult:
    population = ctx.population(params["population"])
    df = population.df.copy()
    entity_keys = params["group_keys"]
    date_col = params["date_column"]
    amount_col = params["amount_column"]
    window_days = resolve_scalar_threshold(ctx, params["window_days"], name="window_days")
    agg_threshold = resolve_scalar_threshold(ctx, params["aggregate_threshold"], name="aggregate_threshold")
    max_line = resolve_scalar_threshold(ctx, params["max_line"], name="max_line") if "max_line" in params else None
    flag_same_day = params.get("flag_same_day", "RF_CS_SplitClaims_SameDay")
    flag_window = params.get("flag_window", "RF_CS_SplitClaims_Window")

    df[date_col] = pd.to_datetime(df[date_col])

    # --- same-day groups: exact (entity_keys + date) match ---
    same_day_row_group = pd.Series([None] * len(df), index=df.index, dtype=object)
    same_day_records: list[dict] = []
    for key, group in df.groupby(entity_keys + [date_col], dropna=False):
        if len(group) <= 1:
            continue
        total = group[amount_col].sum()
        if not (total > agg_threshold):
            continue
        if max_line is not None and (group[amount_col] > max_line).any():
            continue
        key_tuple = key if isinstance(key, tuple) else (key,)
        gid = group_id_from_key(key_tuple)
        same_day_row_group.loc[group.index] = gid
        same_day_records.append({"group_id": gid, "__lines": len(group), "__amount": float(total)})

    same_day_group_df = pd.DataFrame(same_day_records)
    same_day_row_df = df[same_day_row_group.notna()].copy()

    # --- sliding window groups: within entity_keys, every [d, d+window_days-1] window,
    # overlapping qualifying windows merged into one claim group (union-find over rows).
    window_row_group = pd.Series([None] * len(df), index=df.index, dtype=object)
    window_records: list[dict] = []
    for key, group in df.groupby(entity_keys, dropna=False):
        group_sorted = group.sort_values(date_col)
        idxs = group_sorted.index.tolist()
        dates = group_sorted[date_col].tolist()
        n = len(idxs)
        parent = {i: i for i in idxs}
        qualifying_members: set = set()

        for i in range(n):
            window_end = dates[i] + pd.Timedelta(days=window_days - 1)
            members = [idxs[j] for j in range(n) if dates[i] <= dates[j] <= window_end]
            if len(members) <= 1:
                continue
            sub = group_sorted.loc[members]
            total = sub[amount_col].sum()
            if not (total > agg_threshold):
                continue
            if max_line is not None and (sub[amount_col] > max_line).any():
                continue
            qualifying_members.update(members)
            first = members[0]
            for m in members[1:]:
                _union(parent, first, m)

        components: dict = {}
        for m in qualifying_members:
            components.setdefault(_find_union(parent, m), []).append(m)

        key_tuple = key if isinstance(key, tuple) else (key,)
        for members in components.values():
            member_rows = df.loc[members]
            start = member_rows[date_col].min().date().isoformat()
            gid = group_id_from_key(key_tuple + (start,))
            window_row_group.loc[members] = gid
            window_records.append(
                {"group_id": gid, "__lines": len(members), "__amount": float(member_rows[amount_col].sum())}
            )

    window_group_df = pd.DataFrame(window_records)
    window_row_df = df[window_row_group.notna()].copy()

    flags_same_day = flags_from_rows(
        same_day_row_df, flag=flag_same_day, group_ids=same_day_row_group[same_day_row_group.notna()]
    )
    flags_window = flags_from_rows(
        window_row_df, flag=flag_window, group_ids=window_row_group[window_row_group.notna()]
    )
    flags = pd.concat([flags_same_day, flags_window], ignore_index=True)

    flagged_idx = set(same_day_row_group.index[same_day_row_group.notna()]) | set(
        window_row_group.index[window_row_group.notna()]
    )
    split_amount_total = float(df.loc[list(flagged_idx), amount_col].sum()) if flagged_idx else 0.0

    scored_units = (
        (same_day_group_df["group_id"].tolist() if len(same_day_group_df) else [])
        + (window_group_df["group_id"].tolist() if len(window_group_df) else [])
    )

    metrics = build_metrics(
        params.get("metrics", {}),
        population=population,
        default_columns=[amount_col],
        grain="claim_group",
        row_df=None,
        group_df=None,
        scored_units=scored_units,
        values={
            "population_size": population.rows,
            "same_day_groups": len(same_day_group_df),
            "same_day_lines": len(same_day_row_df),
            "window_groups": len(window_group_df),
            "window_lines": len(window_row_df),
            "split_amount": split_amount_total,
        },
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)
