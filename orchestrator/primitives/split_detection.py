from __future__ import annotations

import pandas as pd

from orchestrator.primitives.common import (
    METRICS_PROPERTY_SCHEMA,
    THRESHOLD_ONLY_SCHEMA,
    PrimitiveContext,
    PrimitiveResult,
    build_metrics,
    flags_from_rows,
    keyed_unit_id,
    member_group_id,
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
    # B3: `group_id` here is the per-detection id ("sd:" + a canonical hash of
    # the key tuple, N1) -- it identifies THIS detection, not the claim.
    # `claim_id` ("claim:" + a hash of the member row-key SET, N1) is the
    # claim's true identity: a window group whose members are the identical
    # row set as a same-day group is the same claim, however each was found.
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
        member_keys = group["__row_key"].tolist()
        gid = keyed_unit_id("sd", key_tuple)
        claim_id = member_group_id("claim", member_keys)
        same_day_row_group.loc[group.index] = claim_id
        same_day_records.append(
            {"group_id": gid, "claim_id": claim_id, "__lines": len(group), "__amount": float(total)}
        )

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
            member_keys = member_rows["__row_key"].tolist()
            start = member_rows[date_col].min().date().isoformat()
            gid = keyed_unit_id("win", key_tuple + (start,))
            claim_id = member_group_id("claim", member_keys)
            window_row_group.loc[members] = claim_id
            window_records.append(
                {
                    "group_id": gid,
                    "claim_id": claim_id,
                    "__lines": len(members),
                    "__amount": float(member_rows[amount_col].sum()),
                }
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

    # B3: the claim-group POPULATION is distinct claim ids -- a window
    # detection whose member set equals a same-day detection's is the same
    # claim, counted once. split_same_day_groups/split_window_groups (below)
    # stay kind-specific (how many detections each method made, undeduplicated
    # against each other); split_groups is this distinct set.
    same_day_claim_ids = set(same_day_group_df["claim_id"]) if len(same_day_group_df) else set()
    window_claim_ids = set(window_group_df["claim_id"]) if len(window_group_df) else set()
    distinct_claim_ids = same_day_claim_ids | window_claim_ids

    flagged_idx = set(same_day_row_group.index[same_day_row_group.notna()]) | set(
        window_row_group.index[window_row_group.notna()]
    )
    split_lines = len(flagged_idx)
    split_amount_total = float(df.loc[list(flagged_idx), amount_col].sum()) if flagged_idx else 0.0

    scored_units = sorted(distinct_claim_ids)

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
            "window_groups": len(window_group_df),
            "split_groups": len(distinct_claim_ids),
            "split_lines": split_lines,
            "split_amount": split_amount_total,
        },
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)
