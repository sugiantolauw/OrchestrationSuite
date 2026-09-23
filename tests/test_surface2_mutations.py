from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from orchestrator.eval.surface2 import run_surface2
from orchestrator.primitives.common import (
    Metric,
    PrimitiveResult,
    build_metrics,
    flags_from_rows,
    group_id_from_key,
    keyed_unit_id,
    resolve_scalar_threshold,
)
from orchestrator.skills import load_skill

REPO_ROOT = Path(__file__).parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "tne_planted"
DATA_DIR = FIXTURE_DIR / "data"
PLANTS_PATH = FIXTURE_DIR / "plants.yaml"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

pytestmark = pytest.mark.skipif(
    not DATA_DIR.is_dir(), reason="tests/fixtures/tne_planted/data/ not present -- run generate.py first"
)


def _fresh_skill():
    skill = load_skill(SKILL_DIR)
    skill.validate()
    return skill


def _run(skill):
    return run_surface2(
        skill_dir=SKILL_DIR, plants_path=PLANTS_PATH, data_dir=DATA_DIR, audit_period=AUDIT_PERIOD, skill=skill
    )


def _assert_gate_fails(scores, test_id: str, *, min_precision: float = 0.98, min_recall: float = 0.95):
    score = scores[test_id]
    precision_ok = score.precision is not None and score.precision >= min_precision
    recall_ok = score.recall is not None and score.recall >= min_recall
    assert not (precision_ok and recall_ok), (
        f"{test_id}: Surface 2 did NOT fail under this mutation -- "
        f"precision={score.precision} recall={score.recall} (TP={score.true_positives} "
        f"FP={score.false_positives} FN={score.false_negatives}) -- the gate is not honest for this mutation"
    )


# ── M1: T3.1b anti-join keyed on name only (drops the class key) ───────────


def test_m1_t31b_join_on_name_only_fails_surface2(monkeypatch):
    skill = _fresh_skill()
    for test in skill.plan["tests"]:
        if test["test_id"] == "T3.1b":
            test["params"]["left_keys"] = ["Lead Traveller Name"]
            test["params"]["right_keys"] = ["Employee"]
    scores = _run(skill)
    _assert_gate_fails(scores, "T3.1b")


# ── M2: T5.1 sums the WHOLE employee/vendor/type group once any pair is
# within the window, instead of just the qualifying window's members
# (the historical computation.py:365 defect) ────────────────────────────────


def _m2_run(ctx, params):
    import orchestrator.primitives.split_detection as sd

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
        claim_id = sd.member_group_id("claim", member_keys)
        same_day_row_group.loc[group.index] = claim_id
        same_day_records.append({"group_id": gid, "claim_id": claim_id, "__lines": len(group), "__amount": float(total)})
    same_day_group_df = pd.DataFrame(same_day_records)
    same_day_row_df = df[same_day_row_group.notna()].copy()

    # MUTATION: once ANY pair in the entity_keys group falls within
    # window_days of each other, sum and flag the WHOLE group -- not just the
    # members of the qualifying window.
    window_row_group = pd.Series([None] * len(df), index=df.index, dtype=object)
    window_records: list[dict] = []
    for key, group in df.groupby(entity_keys, dropna=False):
        group_sorted = group.sort_values(date_col)
        idxs = group_sorted.index.tolist()
        dates = group_sorted[date_col].tolist()
        n = len(idxs)
        any_pair_within = any(
            abs((dates[j] - dates[i]).days) <= (window_days - 1) for i in range(n) for j in range(i + 1, n)
        )
        if not any_pair_within or n <= 1:
            continue
        total = group_sorted[amount_col].sum()
        if not (total > agg_threshold):
            continue
        if max_line is not None and (group_sorted[amount_col] > max_line).any():
            continue
        key_tuple = key if isinstance(key, tuple) else (key,)
        member_keys = group_sorted["__row_key"].tolist()
        start = group_sorted[date_col].min().date().isoformat()
        gid = keyed_unit_id("win", key_tuple + (start,))
        claim_id = sd.member_group_id("claim", member_keys)
        window_row_group.loc[idxs] = claim_id
        window_records.append({"group_id": gid, "claim_id": claim_id, "__lines": n, "__amount": float(total)})
    window_group_df = pd.DataFrame(window_records)
    window_row_df = df[window_row_group.notna()].copy()

    flags_same_day = flags_from_rows(same_day_row_df, flag=flag_same_day, group_ids=same_day_row_group[same_day_row_group.notna()])
    flags_window = flags_from_rows(window_row_df, flag=flag_window, group_ids=window_row_group[window_row_group.notna()])
    flags = pd.concat([flags_same_day, flags_window], ignore_index=True)

    same_day_claim_ids = set(same_day_group_df["claim_id"]) if len(same_day_group_df) else set()
    window_claim_ids = set(window_group_df["claim_id"]) if len(window_group_df) else set()
    distinct_claim_ids = same_day_claim_ids | window_claim_ids
    flagged_idx = set(same_day_row_group.index[same_day_row_group.notna()]) | set(window_row_group.index[window_row_group.notna()])
    scored_units = sorted(distinct_claim_ids)

    metrics = build_metrics(
        params.get("metrics", {}), population=population, default_columns=[amount_col], grain="claim_group",
        row_df=None, group_df=None, scored_units=scored_units,
        values={
            "population_size": population.rows,
            "same_day_groups": len(same_day_group_df),
            "window_groups": len(window_group_df),
            "split_groups": len(distinct_claim_ids),
            "split_lines": len(flagged_idx),
            "split_amount": float(df.loc[list(flagged_idx), amount_col].sum()) if flagged_idx else 0.0,
        },
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)


def test_m2_t51_sums_whole_group_fails_surface2(monkeypatch):
    import orchestrator.primitives.split_detection as sd

    monkeypatch.setattr(sd, "run", _m2_run)
    scores = _run(_fresh_skill())
    _assert_gate_fails(scores, "T5.1")


# ── M3: T5.1 same-day detection disabled entirely ───────────────────────────


def _m3_run(ctx, params):
    import orchestrator.primitives.split_detection as sd

    # Same as the real primitive, but the same-day loop never runs.
    population = ctx.population(params["population"])
    df = population.df.copy()
    entity_keys = params["group_keys"]
    date_col = params["date_column"]
    amount_col = params["amount_column"]
    window_days = resolve_scalar_threshold(ctx, params["window_days"], name="window_days")
    agg_threshold = resolve_scalar_threshold(ctx, params["aggregate_threshold"], name="aggregate_threshold")
    max_line = resolve_scalar_threshold(ctx, params["max_line"], name="max_line") if "max_line" in params else None
    flag_window = params.get("flag_window", "RF_CS_SplitClaims_Window")
    df[date_col] = pd.to_datetime(df[date_col])

    window_row_group = pd.Series([None] * len(df), index=df.index, dtype=object)
    window_records: list[dict] = []
    for key, group in df.groupby(entity_keys, dropna=False):
        group_sorted = group.sort_values(date_col)
        idxs = group_sorted.index.tolist()
        dates = group_sorted[date_col].tolist()
        n = len(idxs)
        parent = {i: i for i in idxs}
        qualifying: set = set()
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
            qualifying.update(members)
            first = members[0]
            for m in members[1:]:
                ra, rb = sd._find_union(parent, first), sd._find_union(parent, m)
                if ra != rb:
                    parent[ra] = rb
        components: dict = {}
        for m in qualifying:
            components.setdefault(sd._find_union(parent, m), []).append(m)
        key_tuple = key if isinstance(key, tuple) else (key,)
        for members in components.values():
            member_rows = df.loc[members]
            member_keys = member_rows["__row_key"].tolist()
            start = member_rows[date_col].min().date().isoformat()
            gid = keyed_unit_id("win", key_tuple + (start,))
            claim_id = sd.member_group_id("claim", member_keys)
            window_row_group.loc[members] = claim_id
            window_records.append({"group_id": gid, "claim_id": claim_id, "__lines": len(members), "__amount": float(member_rows[amount_col].sum())})
    window_group_df = pd.DataFrame(window_records)
    window_row_df = df[window_row_group.notna()].copy()

    flags = flags_from_rows(window_row_df, flag=flag_window, group_ids=window_row_group[window_row_group.notna()])
    distinct_claim_ids = set(window_group_df["claim_id"]) if len(window_group_df) else set()
    flagged_idx = set(window_row_group.index[window_row_group.notna()])
    scored_units = sorted(distinct_claim_ids)

    metrics = build_metrics(
        params.get("metrics", {}), population=population, default_columns=[amount_col], grain="claim_group",
        row_df=None, group_df=None, scored_units=scored_units,
        values={
            "population_size": population.rows,
            "same_day_groups": 0,
            "window_groups": len(window_group_df),
            "split_groups": len(distinct_claim_ids),
            "split_lines": len(flagged_idx),
            "split_amount": float(df.loc[list(flagged_idx), amount_col].sum()) if flagged_idx else 0.0,
        },
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)


def test_m3_t51_same_day_disabled_fails_surface2(monkeypatch):
    import orchestrator.primitives.split_detection as sd

    monkeypatch.setattr(sd, "run", _m3_run)
    scores = _run(_fresh_skill())
    _assert_gate_fails(scores, "T5.1")


# ── M4: T6.1d aggregates a day with max() instead of sum() ─────────────────


def _m4_run(ctx, params):
    import orchestrator.primitives.threshold_exceedance as te

    population = ctx.population(params["population"])
    df = population.df.copy()
    column = params["column"]
    direction = params["direction"]
    limit_spec = te.resolve_limit_spec(ctx, params["limit"])
    flag = params.get("flag", "RF_THRESHOLD_EXCEEDANCE")

    excluded_count = 0
    if "exclude" in params:
        mask = te.row_condition_mask(df, params["exclude"])
        excluded_count = int(mask.sum())
        df = df[~mask].reset_index(drop=True)

    group_by = params.get("group_by")
    if group_by:
        # MUTATION: max() instead of sum() -- the M4 defect under test.
        grouped = df.groupby(group_by, dropna=False)[column].max().reset_index()
        grouped = grouped.rename(columns={column: "__agg_value"})

        if limit_spec["kind"] == "column":
            limit_col = limit_spec["column"]
            first_limits = df.groupby(group_by, dropna=False)[limit_col].first().reset_index()
            grouped = grouped.merge(first_limits, on=group_by, how="left")
            limits = grouped[limit_col]
        else:
            limits = pd.Series(limit_spec["value"], index=grouped.index)

        exceed_mask = te.direction_mask(grouped["__agg_value"], limits, direction)
        excess = (grouped["__agg_value"] - limits).clip(lower=0)
        group_df = grouped[exceed_mask].copy()
        group_df["__excess"] = excess[exceed_mask]
        group_df[column] = group_df["__agg_value"]

        group_ids = group_df[group_by].apply(lambda r: keyed_unit_id("thr", tuple(r)), axis=1)
        group_df = group_df.assign(group_id=group_ids.to_numpy())
        row_group_ids = df[group_by].apply(lambda r: keyed_unit_id("thr", tuple(r)), axis=1)
        row_mask = row_group_ids.isin(set(group_df["group_id"]))
        row_df = df[row_mask].copy()
        flags = flags_from_rows(row_df, flag=flag, group_ids=row_group_ids[row_mask])
        scored_units = group_df["group_id"].tolist()
        excess_total = float(group_df["__excess"].sum())
    else:
        limits = pd.Series(limit_spec["value"], index=df.index) if limit_spec["kind"] == "threshold" else df[limit_spec["column"]]
        exceed_mask = te.direction_mask(df[column], limits, direction)
        row_df = df[exceed_mask].copy()
        excess_series = (df[column] - limits).clip(lower=0)
        row_df["__excess"] = excess_series[exceed_mask]
        group_df = None
        flags = flags_from_rows(row_df, flag=flag)
        scored_units = row_df["__row_key"].tolist()
        excess_total = float(row_df["__excess"].sum())

    metrics = build_metrics(
        params.get("metrics", {}), population=population, default_columns=[column],
        grain=",".join(group_by) if group_by else "row", row_df=row_df, group_df=group_df, scored_units=scored_units,
        values={"population_size": population.rows, "excess": excess_total, "excluded_count": excluded_count},
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)


def test_m4_t61d_max_instead_of_sum_fails_surface2(monkeypatch):
    import orchestrator.primitives.threshold_exceedance as te

    monkeypatch.setattr(te, "run", _m4_run)
    scores = _run(_fresh_skill())
    _assert_gate_fails(scores, "T6.1d_dom")


# ── M5: T4.4 uses >= instead of > (regression pin -- already caught) ───────


def test_m5_t44_gte_instead_of_gt_fails_surface2(monkeypatch):
    import orchestrator.primitives.common as common

    monkeypatch.setitem(common._DIRECTION_OPS, "above", lambda values, limits: values >= limits)
    scores = _run(_fresh_skill())
    _assert_gate_fails(scores, "T4.4")


# ── M8: the ExCo membership filter is bypassed everywhere (simulated by
# widening the exco_employee_ids reference to admit a NON_EXCO identity) ────


def test_m8_exco_filter_removed_fails_surface2():
    skill = _fresh_skill()
    # 90004 = "Novak, Petra" (generate.py NON_EXCO) -- admitting her id
    # simulates the ExCo membership filter being bypassed for her rows,
    # exactly as if the filter were dropped from every population.
    skill.references["exco_employee_ids"] = list(skill.references["exco_employee_ids"]) + [90004]
    scores = _run(skill)
    _assert_gate_fails(scores, "T4.4")


# ── B2: T6.1a reverted to the pre-fix worst_case-only exception rule ───────


def _b2_old_worst_case_only_run(ctx, params):
    population = ctx.population(params["population"])
    df = population.df.copy()
    viewed_cols = params["receipt_viewed_columns"]
    receipt_viewed = pd.Series(False, index=df.index)
    for col in viewed_cols:
        receipt_viewed = receipt_viewed | (df[col].astype(str).str.strip().str.upper() == "YES")
    instant_threshold = ctx.thresholds[params["instant_threshold"]["threshold"]]["value"]
    received_col = params["approver_received_date_column"]
    approved_col = params["approved_datetime_column"]
    minutes_since_received = (df[approved_col] - df[received_col]).dt.total_seconds() / 60.0
    worst_case = (~receipt_viewed) & (minutes_since_received < instant_threshold)

    flag = params.get("flag", "RF_APR_InsufficientReview")
    row_df = df[worst_case].copy()
    flags = flags_from_rows(row_df, flag=flag)
    scored_units = row_df["__row_key"].tolist()
    source_ref = {
        "sources": [{"name": population.source, "version": population.source_version}],
        "columns": list(viewed_cols),
        "grain": "approval_step",
        "population": population.name,
    }
    metrics = {"approver_worst_case_n": Metric(value=int(worst_case.sum()), unit="count", source_ref=source_ref).to_dict()}
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)


def test_b2_t61a_reverted_to_worst_case_only_fails_surface2():
    skill = _fresh_skill()
    skill.custom_primitives["t6_1a_approver_review"]["run"] = _b2_old_worst_case_only_run
    scores = _run(skill)
    _assert_gate_fails(scores, "T6.1a")


# ── B3: T5.1 reverted to the pre-fix per-kind id scheme (no member-set
# dedup between a same-day and a window detection of the identical rows) ───


def _b3_old_ids_run(ctx, params):
    import orchestrator.primitives.split_detection as sd

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
        # PRE-B3: the per-detection key-tuple id was ALSO the scored unit --
        # no member-set dedup against an overlapping window detection.
        gid = group_id_from_key(key_tuple)
        same_day_row_group.loc[group.index] = gid
        same_day_records.append({"group_id": gid, "__lines": len(group), "__amount": float(total)})
    same_day_group_df = pd.DataFrame(same_day_records)
    same_day_row_df = df[same_day_row_group.notna()].copy()

    window_row_group = pd.Series([None] * len(df), index=df.index, dtype=object)
    window_records: list[dict] = []
    for key, group in df.groupby(entity_keys, dropna=False):
        group_sorted = group.sort_values(date_col)
        idxs = group_sorted.index.tolist()
        dates = group_sorted[date_col].tolist()
        n = len(idxs)
        parent = {i: i for i in idxs}
        qualifying: set = set()
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
            qualifying.update(members)
            first = members[0]
            for m in members[1:]:
                ra, rb = sd._find_union(parent, first), sd._find_union(parent, m)
                if ra != rb:
                    parent[ra] = rb
        components: dict = {}
        for m in qualifying:
            components.setdefault(sd._find_union(parent, m), []).append(m)
        key_tuple = key if isinstance(key, tuple) else (key,)
        for members in components.values():
            member_rows = df.loc[members]
            start = member_rows[date_col].min().date().isoformat()
            gid = group_id_from_key(key_tuple + (start,))
            window_row_group.loc[members] = gid
            window_records.append({"group_id": gid, "__lines": len(members), "__amount": float(member_rows[amount_col].sum())})
    window_group_df = pd.DataFrame(window_records)
    window_row_df = df[window_row_group.notna()].copy()

    flags_same_day = flags_from_rows(same_day_row_df, flag=flag_same_day, group_ids=same_day_row_group[same_day_row_group.notna()])
    flags_window = flags_from_rows(window_row_df, flag=flag_window, group_ids=window_row_group[window_row_group.notna()])
    flags = pd.concat([flags_same_day, flags_window], ignore_index=True)

    flagged_idx = set(same_day_row_group.index[same_day_row_group.notna()]) | set(window_row_group.index[window_row_group.notna()])
    scored_units = (
        (same_day_group_df["group_id"].tolist() if len(same_day_group_df) else [])
        + (window_group_df["group_id"].tolist() if len(window_group_df) else [])
    )

    metrics = build_metrics(
        params.get("metrics", {}), population=population, default_columns=[amount_col], grain="claim_group",
        row_df=None, group_df=None, scored_units=scored_units,
        values={
            "population_size": population.rows,
            "same_day_groups": len(same_day_group_df),
            "window_groups": len(window_group_df),
            "split_groups": len(scored_units),
            "split_lines": len(flagged_idx),
            "split_amount": float(df.loc[list(flagged_idx), amount_col].sum()) if flagged_idx else 0.0,
        },
    )
    return PrimitiveResult(metrics=metrics, flags=flags, scored_units=scored_units)


def test_b3_t51_reverted_to_per_kind_ids_fails_surface2(monkeypatch):
    import orchestrator.primitives.split_detection as sd

    monkeypatch.setattr(sd, "run", _b3_old_ids_run)
    scores = _run(_fresh_skill())
    # Every claim id this reverted primitive emits is in the OLD (delimiter-
    # joined key tuple) format -- plants.yaml's expected ids are all computed
    # in the NEW member-set-hash format (B3/N1), so essentially every plant
    # is missed: a blunt but sufficient demonstration that the id scheme is
    # load-bearing for the gate, not merely cosmetic.
    _assert_gate_fails(scores, "T5.1")


# B5 (limit_selector_aggregate: any) cannot be exercised through Surface 2 on
# this data: External ID is non-null on every attendee_validity row (a
# documented data-quality fact, custom.py's P2b-1 note), so
# attendee_external_classification classifies every attendee external and
# "any" vs "first" never disagree on a real T3.3b group here. B5's fix is
# covered instead by a direct primitive-level test
# (tests/test_p2a_primitives.py::test_ratio_per_group_limit_selector_aggregate_any_vs_first)
# against a synthetic population that is not constrained by that contract.
