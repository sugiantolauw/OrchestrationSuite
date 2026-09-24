from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd


class PopulationError(Exception):
    pass


@dataclass
class PopulationResult:
    name: str
    source: str
    source_version: str
    df: pd.DataFrame
    rows: int
    amount: float | None
    min_date: str | None
    max_date: str | None
    amount_column: str | None = None
    date_column: str | None = None
    excluded_counts: dict[str, int] = field(default_factory=dict)
    derivation_counters: dict[str, int] = field(default_factory=dict)

    def reconciliation_summary(self) -> dict:
        return {
            "population": self.name,
            "rows": self.rows,
            "amount": self.amount,
            "min_date": self.min_date,
            "max_date": self.max_date,
            "excluded_counts": dict(self.excluded_counts),
            "derivation_counters": dict(self.derivation_counters),
        }


@dataclass
class PopulationContext:
    sources: dict[str, dict]  # name -> {"df": DataFrame, "version": str}
    references: dict[str, Any]  # reference-id -> list | dict | DataFrame
    audit_period: tuple[str, str]
    thresholds: dict[str, dict]
    custom_derivations: dict[str, Callable] = field(default_factory=dict)


def _resolve_value(value: Any, ctx: PopulationContext) -> Any:
    if isinstance(value, dict) and "ref" in value:
        ref = value["ref"]
        if ref == "audit_period":
            return list(ctx.audit_period)
        if ref not in ctx.references:
            raise PopulationError(f"unknown reference: {ref!r}")
        return ctx.references[ref]
    return value


def _apply_filter(df: pd.DataFrame, flt: dict, ctx: PopulationContext) -> pd.Series:
    if "any_of" in flt:
        masks = [_apply_filter(df, f, ctx) for f in flt["any_of"]]
        mask = masks[0]
        for m in masks[1:]:
            mask = mask | m
        return mask
    if "all_of" in flt:
        masks = [_apply_filter(df, f, ctx) for f in flt["all_of"]]
        mask = masks[0]
        for m in masks[1:]:
            mask = mask & m
        return mask

    col = flt["column"]
    op = flt["op"]
    if col not in df.columns:
        raise PopulationError(f"filter references unknown column: {col!r}")
    series = df[col]

    if op == "is_null":
        return series.isna()
    if op == "not_null":
        return series.notna()

    value = _resolve_value(flt.get("value"), ctx)
    if op == "eq":
        return series == value
    if op == "ne":
        return series != value
    if op == "in":
        return series.isin(value)
    if op == "not_in":
        return ~series.isin(value)
    if op == "between":
        lo, hi = value
        if _looks_like_date(series):
            lo = pd.Timestamp(lo)
            # A bare upper-bound date means "through the end of that day", not
            # "through midnight at its start" -- otherwise every row on the last
            # day of the period with a non-midnight time-of-day (e.g. an
            # approval timestamp) is silently dropped. CLAUDE.md §0.5: an audit
            # period is a business-calendar concept.
            #
            # The timezone half (independent test-gap audit #13/H9) is
            # addressed upstream, not here: by the time a population is built,
            # `series` is a column DataSourceAdapter.read_population already
            # returned NAIVE -- and every adapter is responsible for that
            # naive value already representing local wall-clock time in the
            # contract's declared timezone (orchestrator.timeutil.
            # to_business_local, applied in
            # orchestrator.adapters.datasource_uc._normalise_datetime_dtypes
            # for a UC TIMESTAMP's tz-aware value, a no-op for a file/Volume
            # read's already-naive one). `lo`/`hi` are plain calendar-day
            # bounds in that same local, naive representation, so this
            # comparison needs no tz-aware arithmetic of its own -- and
            # correctly handles a DST-transition day for free, since a
            # calendar day here is always 24 naive hours regardless of what
            # UTC offset applied on either side of it.
            hi = pd.Timestamp(hi) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        return (series >= lo) & (series <= hi)
    if op == "gt":
        return series > value
    if op == "gte":
        return series >= value
    if op == "lt":
        return series < value
    if op == "lte":
        return series <= value
    raise PopulationError(f"unknown filter op: {op!r}")


def _looks_like_date(series: pd.Series) -> bool:
    return pd.api.types.is_datetime64_any_dtype(series)


def _describe_filter(flt: dict, index: int, *, stage: str = "filter") -> str:
    if "label" in flt:
        return flt["label"]
    if "any_of" in flt:
        return f"{stage}[{index}]:any_of"
    if "all_of" in flt:
        return f"{stage}[{index}]:all_of"
    return f"{stage}[{index}]:{flt.get('column')}:{flt.get('op')}"


def _operand_series(df: pd.DataFrame, spec: Any, ctx: PopulationContext) -> Any:
    if isinstance(spec, dict):
        if "column" in spec:
            return df[spec["column"]]
        if "threshold" in spec:
            tid = spec["threshold"]
            if tid not in ctx.thresholds:
                raise PopulationError(f"unknown threshold id: {tid!r}")
            return ctx.thresholds[tid]["value"]
        raise PopulationError(f"operand must have 'column' or 'threshold': {spec!r}")
    return spec


def _as_ref_frame(table: Any) -> pd.DataFrame:
    return table if isinstance(table, pd.DataFrame) else pd.DataFrame(table)


def _resolve_table(ctx: "PopulationContext", name: str) -> pd.DataFrame:
    """A `lookup`/`fx_rate` table is usually a Skill-authored reference/ file, but
    may instead be another declared contract source read as-is (e.g. a rate table
    that is itself audited, contract-validated data rather than Skill-authored
    reference data) -- references are checked first since that is the common case."""
    if name in ctx.references:
        return _as_ref_frame(ctx.references[name])
    if name in ctx.sources:
        return ctx.sources[name]["df"]
    raise PopulationError(f"unknown lookup table: {name!r} (not a reference or a source)")


def apply_derivations(
    df: pd.DataFrame,
    derive_list: list[dict] | None,
    ctx: PopulationContext,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Applies each declared derivation in order. Every derivation is an explicit,
    named op -- never silently defaults an unmapped value; instead it is left null
    and counted (CLAUDE.md §4 populations, NN14)."""
    df = df.copy()
    counters: dict[str, int] = {}

    for d in derive_list or []:
        op = d["op"]
        out_col = d["as"]

        if op == "map":
            src_col = d["column"]
            mapping = _resolve_value(d["mapping"], ctx)
            mapped = df[src_col].map(mapping)
            unmapped = mapped.isna() & df[src_col].notna()
            counters[d.get("unmapped_counter", f"{out_col}_unmapped_rows")] = int(unmapped.sum())
            df[out_col] = mapped

        elif op == "lookup":
            ref_df = _resolve_table(ctx, d["table"])
            left_keys = d["on"] if isinstance(d["on"], list) else [d["on"]]
            right_keys = d.get("ref_on", left_keys)
            right_keys = right_keys if isinstance(right_keys, list) else [right_keys]
            value_col = d["value_column"]
            ref_cols = list(dict.fromkeys(right_keys + [value_col]))
            merged = df.merge(
                ref_df[ref_cols],
                how="left",
                left_on=left_keys,
                right_on=right_keys,
                suffixes=("", "__ref"),
            )
            unmapped = merged[value_col].isna()
            counters[d.get("unmapped_counter", f"{out_col}_unmapped_rows")] = int(unmapped.sum())
            df[out_col] = merged[value_col].to_numpy()

        elif op == "coalesce":
            cols = d["columns"]
            df[out_col] = df[cols].bfill(axis=1).iloc[:, 0]

        elif op == "month":
            src_col = d["column"]
            df[out_col] = pd.to_datetime(df[src_col]).dt.strftime("%Y-%m")

        elif op == "fx_rate":
            ref_df = _resolve_table(ctx, d["table"])
            month_col = d["month_column"]
            key_col = d.get("key_column", "month")
            value_col = d.get("value_column", "rate")
            merged = df.merge(
                ref_df[[key_col, value_col]],
                how="left",
                left_on=month_col,
                right_on=key_col,
            )
            unmapped = merged[value_col].isna()
            counters[d.get("unmapped_counter", f"{out_col}_unmapped_rows")] = int(unmapped.sum())
            df[out_col] = merged[value_col].to_numpy()

        elif op == "multiply":
            left = _operand_series(df, d["left"], ctx)
            right = _operand_series(df, d["right"], ctx)
            df[out_col] = left * right

        elif op == "divide":
            left = _operand_series(df, d["left"], ctx)
            right = _operand_series(df, d["right"], ctx)
            df[out_col] = left / right

        elif op == "membership_role":
            prepared = df[d["prepared_column"]].astype(bool)
            approved = df[d["approved_column"]].astype(bool)
            df[out_col] = [
                "both" if p and a else "prepared" if p else "approved" if a else None
                for p, a in zip(prepared, approved)
            ]

        elif op == "custom":
            fn = ctx.custom_derivations.get(d["name"])
            if fn is None:
                raise PopulationError(f"unknown custom derivation: {d['name']!r}")
            values, extra_counters = fn(df, d.get("params", {}), ctx)
            df[out_col] = values
            counters.update(extra_counters or {})

        else:
            raise PopulationError(f"unknown derivation op: {op!r}")

    return df, counters


def build_population(name: str, pop_config: dict, ctx: PopulationContext) -> PopulationResult:
    source = pop_config["source"]
    if source not in ctx.sources:
        raise PopulationError(f"population {name!r} references unknown source: {source!r}")
    src = ctx.sources[source]
    df = src["df"].copy()
    version = src["version"]

    # Two-phase build: filter -> derive -> post_filter (P2b-1). `pre_filter_derive`
    # still exists for the rare case a `filters` entry itself must key on a
    # cleaned-up value (e.g. coercing a column that mixes numeric ids with a
    # non-numeric sentinel before an `in` filter) -- it runs before ANY filter and
    # therefore still sees the whole raw source. But a filter that instead depends
    # on a value `derive` computes (e.g. a looked-up country) no longer has to pay
    # that same price: it belongs in `post_filter`, which runs AFTER `derive`, so
    # `derive`'s own unmapped/not-found counters are scoped to the population
    # `filters` already narrowed -- not to the whole raw source.
    df, pre_counters = apply_derivations(df, pop_config.get("pre_filter_derive"), ctx)

    excluded_counts: dict[str, int] = {}
    mask = pd.Series(True, index=df.index)
    for i, flt in enumerate(pop_config.get("filters", [])):
        fmask = _apply_filter(df, flt, ctx)
        newly_excluded = int((mask & ~fmask).sum())
        excluded_counts[_describe_filter(flt, i)] = newly_excluded
        mask = mask & fmask

    df = df[mask].reset_index(drop=True)
    df, counters = apply_derivations(df, pop_config.get("derive"), ctx)
    counters = {**pre_counters, **counters}

    post_mask = pd.Series(True, index=df.index)
    for i, flt in enumerate(pop_config.get("post_filter", [])):
        fmask = _apply_filter(df, flt, ctx)
        newly_excluded = int((post_mask & ~fmask).sum())
        excluded_counts[_describe_filter(flt, i, stage="post_filter")] = newly_excluded
        post_mask = post_mask & fmask
    df = df[post_mask].reset_index(drop=True)

    amount_col = pop_config.get("amount_column")
    date_col = pop_config.get("date_column")

    # Item 3 (CLAUDE.md NN14, P2/P3 gate review): a DECLARED amount_column/
    # date_column absent from the population's own columns (after
    # filter/derive) is a contract mismatch -- fail loudly. This is distinct
    # from an amount_column that IS present on a genuinely empty population,
    # whose amount legitimately reconciles to 0.0/no dates -- that is real
    # data, not a missing column, and must not raise.
    if amount_col and amount_col not in df.columns:
        raise PopulationError(
            f"population {name!r}: declared amount_column {amount_col!r} is not a column in "
            f"this population after filtering/deriving"
        )
    if date_col and date_col not in df.columns:
        raise PopulationError(
            f"population {name!r}: declared date_column {date_col!r} is not a column in "
            f"this population after filtering/deriving"
        )

    amount = None
    if amount_col:
        amount = float(df[amount_col].sum()) if len(df) else 0.0

    min_date = max_date = None
    if date_col and len(df):
        dates = pd.to_datetime(df[date_col])
        if dates.notna().any():
            min_date = dates.min().date().isoformat()
            max_date = dates.max().date().isoformat()

    return PopulationResult(
        name=name,
        source=source,
        source_version=version,
        df=df,
        rows=len(df),
        amount=amount,
        min_date=min_date,
        max_date=max_date,
        amount_column=amount_col,
        date_column=date_col,
        excluded_counts=excluded_counts,
        derivation_counters=counters,
    )


def build_populations(
    populations_config: dict[str, dict], ctx: PopulationContext
) -> dict[str, PopulationResult]:
    return {name: build_population(name, cfg, ctx) for name, cfg in populations_config.items()}
