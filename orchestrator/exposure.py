"""Per-run amount-at-risk computation (P6 WP N4, docs/specs/
P6_narration_design.md §5.3, item 6 of "Design in one page"): extracted out
of `orchestrator.nodes.fieldwork.prioritise`'s former inline body so a later
node (`finalise`, WP N10) can recompute the headline over a different
finding set -- rule findings plus accepted AI-proposed candidates -- from
already-persisted `test_line_values` rows, without re-reading source data.
`prioritise` remains the only node besides `execute` that reads raw source
data for this purpose (see `orchestrator.nodes.fieldwork`'s own module
docstring); nothing else in this module touches `ctx.data_source`.

CLAUDE.md §0.3 / independent review 2026-09-24 item 1 -- restated here since
every function in this module exists to enforce it: the run headline
("Potential exposure", the amount at risk) counts every DISTINCT flagged
transaction LINE exactly once, at the LARGEST amount at risk any finding
attributes to it. A 'spend' finding attributes a line's own full amount; an
'excess' finding attributes only the at-risk portion (the line(s) beyond the
first, deterministically ordered, in a duplicate_detection group, or a
group_by threshold_exceedance group's over-limit amount allocated pro rata
to that group's lines by their own amount). Line identity is row identity
for a row-grain source, or a declared repeats-grain `entry_key`
(contract.yaml `entry_key_repeats: true`) where a source's grain genuinely
repeats (e.g. attendee rows). A finding's own `exposure_amount` is a
SEPARATE figure -- always the sum of its own cited amount metric(s), never
re-derived from raw rows -- and legitimately overlaps with other findings'
`exposure_amount` and with the headline; only the headline de-duplicates.

Three layers:

  * `read_line_amounts` reads every bound source a population declares an
    `amount_column` for, or that declares an `entry_key` (contract.yaml), at
    the SAME pinned versions `execute()` used -- the one raw-data read this
    module performs (impure: calls `ctx.data_source`).
  * `build_test_line_values` derives the `test_line_values` rows `prioritise`
    persists: `{test_id, source, row_key, line_key, spend_amount,
    excess_amount|null}`, one row per (test_id, flagged row) for EVERY
    testable plan test with any flagged rows on an amount-bearing source --
    not only the tests a `findings.yaml` rule happens to cite, so an
    AI-proposed finding's producing test (never cited by any rule, §5.1
    C-2's anchor metric) still has its line values available at `finalise`
    time. A row is emitted under a given test_id only when it genuinely
    belongs to THAT test's own re-derived group -- two sibling sub-tests can
    share one flag NAME (T6.1d_dom/_int both write RF_CS_DailySpendOverLimit)
    while each one's own grouping contains at most one of them; a row that
    resolves to none of the tests sharing its flag is silently left out of
    this table (`finding_line_values` raises loudly instead, but only for a
    test a finding actually cites -- the same failure mode `prioritise`
    always had).
  * `finding_line_values` and `headline` are PURE reductions over already-
    resolved rows: no source read, no ctx. `finding_line_values` resolves
    ONE finding's own line contributions from its `monetary_basis` and
    producing test ids; `headline` reduces a list of such per-finding maps
    to the run's amount-at-risk figure (max per line, never summed across
    findings).
"""

from __future__ import annotations

import json

import pandas as pd

from orchestrator.contract import ContractViolation, validate_contract
from orchestrator.populations import PopulationContext, build_populations
from orchestrator.skills import plan_test_amount_metrics, plan_test_flags

_SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def line_key(source: str, row_key: str, entry_key_cols_by_source: dict, entry_key_by_row: dict) -> str:
    """The distinct flagged transaction LINE this (source, row_key) belongs
    to, as a stable JSON string (safe as both an in-memory dict key and a
    persisted `test_line_values.line_key` TEXT column): row identity for a
    row-grain source (the default), or the source's declared repeats-grain
    `entry_key` column values where several rows are genuinely one business
    entry (e.g. attendee rows of one entertainment claim)."""
    entry_key_cols = entry_key_cols_by_source.get(source)
    if entry_key_cols is None:
        return json.dumps([source, row_key], default=str)
    key_values = entry_key_by_row.get((source, row_key))
    if key_values is None:
        raise ContractViolation(
            [f"{source} row {row_key!r}: missing entry_key for headline de-duplication -- "
             f"this source's population should have been read above"]
        )
    return json.dumps([source, list(key_values)], default=str)


def read_line_amounts(ctx, skill, bindings: dict, amount_col_by_source: dict, entry_key_cols_by_source: dict):
    """Reads every bound source `amount_col_by_source`/`entry_key_cols_by_source`
    names, at the pinned versions `execute()` used, and returns
    `(row_amount, row_position, entry_key_by_row, source_frames)`:

      * `row_amount[(source, row_key)]` -- the row's own amount, float,
        never null (a contract violation for a missing/non-numeric amount
        the contract's own declaration should already have caught).
      * `row_position[(source, row_key)]` -- the row's position in this same
        deterministic read order, i.e. the same "first" a duplicate_detection
        primitive itself would pick (groupby preserves original row order
        within a group).
      * `entry_key_by_row[(source, row_key)]` -- the row's declared
        repeats-grain entry_key column values, where the source declares one.
        A declared entry_key WITHOUT `entry_key_repeats: true` asserts that
        key is unique per row -- checked here against the data actually
        read; not unique fails loudly.
      * `source_frames[source]` -- the raw DataFrame read, reused by
        `build_test_line_values`'s own `read_population` calls for a
        group_by threshold_exceedance test's other contract sources (e.g.
        a per_diem_rates lookup)."""
    row_amount: dict[tuple, float] = {}
    row_position: dict[tuple, int] = {}
    entry_key_by_row: dict[tuple, tuple] = {}
    source_frames: dict[str, pd.DataFrame] = {}
    entry_key_repeats_by_source = {
        name: bool(skill.contract.get("sources", {}).get(name, {}).get("entry_key_repeats"))
        for name in entry_key_cols_by_source
    }

    for source in sorted(set(amount_col_by_source) | set(entry_key_cols_by_source)):
        version = bindings.get(source)
        if version is None:
            raise ContractViolation(
                [f"source {source!r} declares amount_column and/or entry_key but "
                 f"state.data_assets has no binding for it -- discover() should "
                 f"have required one"]
            )
        # CLAUDE.md §0.5/NN14: same declared contract timezone execute()
        # already used for this (source, version) -- keeps this read
        # cache-consistent with (and numerically identical to) execute()'s.
        df = ctx.data_source.read_population(source, version=version, audit_timezone=skill.contract.get("timezone"))
        source_frames[source] = df

        col = amount_col_by_source.get(source)
        if col is not None:
            if col not in df.columns:
                raise ContractViolation(
                    [f"{source}: declared amount_column {col!r} is not a column in the data read "
                     f"at version {version!r} -- the contract's column declaration should have "
                     f"caught this before execute() ran"]
                )
            for pos, (row_key, amount) in enumerate(zip(df["__row_key"], df[col])):
                if pd.isna(amount):
                    raise ContractViolation(
                        [f"{source}.{col} row {row_key!r}: amount is null -- the contract's "
                         f"nullability declaration for this column should have caught this "
                         f"before execute() ran"]
                    )
                try:
                    value = float(amount)
                except (TypeError, ValueError) as exc:
                    raise ContractViolation(
                        [f"{source}.{col} row {row_key!r}: amount {amount!r} is not numeric ({exc}) "
                         f"-- the contract's type declaration for this column should have caught this"]
                    ) from exc
                row_amount[(source, row_key)] = value
                row_position[(source, row_key)] = pos

        entry_key_cols = entry_key_cols_by_source.get(source)
        if entry_key_cols is not None:
            missing_cols = [c for c in entry_key_cols if c not in df.columns]
            if missing_cols:
                raise ContractViolation(
                    [f"{source}: declared entry_key column(s) {missing_cols} not in the data read "
                     f"at version {version!r} -- the contract's entry_key declaration should have "
                     f"caught this before this run"]
                )
            repeats_ok = entry_key_repeats_by_source.get(source, False)
            seen_keys: dict[tuple, str] = {}
            duplicate_keys: dict[tuple, int] = {}
            for row_key, key_values in zip(df["__row_key"], df[entry_key_cols].itertuples(index=False, name=None)):
                entry_key_by_row[(source, row_key)] = key_values
                if key_values in seen_keys:
                    duplicate_keys[key_values] = duplicate_keys.get(key_values, 1) + 1
                else:
                    seen_keys[key_values] = row_key
                if col is not None and key_values in duplicate_keys:
                    prior_amt = row_amount.get((source, seen_keys[key_values]))
                    this_amt = row_amount.get((source, row_key))
                    if prior_amt is not None and this_amt is not None and round(prior_amt, 6) != round(this_amt, 6):
                        raise ContractViolation(
                            [f"{source} entry_key {key_values!r}: rows disagree on amount "
                             f"({prior_amt} vs {this_amt}) -- the same transaction entry cannot "
                             f"have two different amounts"]
                        )
            if not repeats_ok and duplicate_keys:
                example_key, example_count = next(iter(duplicate_keys.items()))
                raise ContractViolation(
                    [f"{source}: declared entry_key {entry_key_cols} is not unique in the data read "
                     f"at version {version!r} -- {len(duplicate_keys)} key value(s) are shared by more "
                     f"than one row (e.g. {example_key!r}, shared by {example_count} rows). Either "
                     f"this source's grain genuinely repeats (declare entry_key_repeats: true in "
                     f"contract.yaml) or line identity should be row identity (remove entry_key for "
                     f"this source)."]
                )

    return row_amount, row_position, entry_key_by_row, source_frames


def dup_group_first_row(rows_for_flag: list[dict], row_position: dict) -> dict[str, str]:
    """group_id -> the row_key duplicate_detection itself would treat as
    "the first" of that group (lowest row_position, i.e. earliest in the
    deterministic read order), scored against `rows_for_flag` -- the FULL
    flagged population for one primary flag, never a single finding's own
    subset, so the choice is the same regardless of which finding is
    asking."""
    best: dict[str, tuple[int, str]] = {}
    for r in rows_for_flag:
        gid = r.get("group_id")
        pos = row_position.get((r["source"], r["row_key"]))
        if gid is None or pos is None:
            continue
        current = best.get(gid)
        if current is None or pos < current[0]:
            best[gid] = (pos, r["row_key"])
    return {gid: rk for gid, (_, rk) in best.items()}


def per_diem_row_excess(
    ctx, skill, state, test: dict, populations_cfg: dict, source_frames: dict,
    contract_validated_sources: set, bindings: dict,
) -> dict[str, float]:
    """row_key -> this row's pro-rata share of its group's over-limit
    excess, for a group_by threshold_exceedance test (T6.1d's shape).
    Rebuilds the ONE population this test reads via
    `orchestrator.populations.build_populations` -- the exact function
    `execute()` used, from the same raw sources (reusing `source_frames`
    where already read) -- so a derived limit column (e.g. a per-diem rate
    converted to AUD) is never re-implemented here. Allocation is
    proportional to each line's own amount within its group: a group $100
    over limit, with a $300 and a $100 line, allocates $75 and $25.
    `contract_validated_sources` is a caller-owned set mutated in place, so
    a raw source shared by several group_by tests in the same run (e.g.
    T6.1d_dom and T6.1d_int's shared per_diem_rates lookup) is validated
    once."""
    params = test["params"]
    pop_name = params["population"]
    pop_cfg = populations_cfg[pop_name]
    # Every DECLARED contract source, not just this population's own
    # `source:` -- a `derive` step's `lookup`/`fx_rate` table may itself be
    # another contract source read as-is (T6.1d's per_diem_rates,
    # orchestrator.populations._resolve_table), and this function has no
    # generic way to know which ones a given population's derive list will
    # ask for short of reading it the same way execute_skill() does: every
    # contract source, once, up front.
    raw_sources: dict[str, dict] = {}
    for src in skill.contract.get("sources", {}):
        raw_df = source_frames.get(src)
        if raw_df is None:
            version = bindings.get(src)
            if version is None:
                raise ContractViolation(
                    [f"source {src!r} declares a contract but state.data_assets has no "
                     f"binding for it -- discover() should have required one"]
                )
            # CLAUDE.md §0.5/NN14: the same declared contract timezone
            # execute_skill() threads through read_population -- never a
            # bare re-read left to fall back to a different (or no)
            # timezone, which would disagree with execute()'s own already-
            # normalised dates for this exact (source, version).
            raw_df = ctx.data_source.read_population(
                src, version=version, audit_timezone=skill.contract.get("timezone"),
            )
            source_frames[src] = raw_df
        if src not in contract_validated_sources:
            # build_populations' own filters/derives assume contract-typed
            # columns (e.g. a real datetime64 Transaction Date, not raw
            # object-dtype values) -- execute() gets this for free because
            # execute_skill() validates every raw source before building any
            # population; this function reads sources itself and must do the
            # same before reusing populations.build_populations, never
            # assume a plain read_population() already produced typed
            # columns. Mutates raw_df (and therefore source_frames[src]) in
            # place, once per source, idempotently.
            validate_contract(raw_df, skill.contract.get("sources", {}).get(src, {}))
            contract_validated_sources.add(src)
        raw_sources[src] = {"df": raw_df, "version": bindings.get(src)}
    pop_ctx = PopulationContext(
        sources=raw_sources,
        references=skill.references,
        audit_period=state.audit_period,
        thresholds=skill.thresholds,
        custom_derivations=skill.custom_derivations,
    )
    pop = build_populations({pop_name: pop_cfg}, pop_ctx)[pop_name]
    df = pop.df
    group_by = params["group_by"]
    column = params["column"]
    limit_spec = params["limit"]
    row_excess: dict[str, float] = {}
    if len(df):
        totals = df.groupby(group_by, dropna=False)[column].sum()
        if "column" in limit_spec:
            limits = df.groupby(group_by, dropna=False)[limit_spec["column"]].first()
        else:
            threshold_value = skill.thresholds[limit_spec["threshold"]]["value"]
            limits = pd.Series(threshold_value, index=totals.index)
        for gkey, group_df in df.groupby(group_by, dropna=False):
            total = float(totals.loc[gkey])
            limit = float(limits.loc[gkey])
            excess = max(0.0, total - limit)
            if excess <= 0.0 or total == 0.0:
                continue
            for row_key, amt in zip(group_df["__row_key"], group_df[column]):
                row_excess[row_key] = excess * (float(amt) / total)
    return row_excess


def build_test_line_values(
    ctx, skill, state, bindings: dict, tests_by_id: dict, flags_by_test_id: dict,
    rows_by_flag: dict, amount_col_by_source: dict, entry_key_cols_by_source: dict,
    entry_key_by_row: dict, row_amount: dict, row_position: dict, populations_cfg: dict,
    source_frames: dict,
) -> list[dict]:
    """The `test_line_values` rows `prioritise` persists (§5.3):
    `{test_id, source, row_key, line_key, spend_amount, excess_amount|null}`,
    for every TESTABLE plan test (`tests_by_id` already excludes
    `not_testable` entries) with any flagged rows on a source
    `amount_col_by_source` declares an amount column for. `spend_amount` is
    always the row's own raw amount; `excess_amount` is populated only for a
    primitive that supports an at-risk allocation (duplicate_detection,
    group_by threshold_exceedance) AND only when the row genuinely belongs
    to THIS test's own re-derived group -- see the module docstring for why
    a row can legitimately be left out of one sibling test's rows here.

    Coordinator fix (2026-09-24, NN14): a row can legitimately be absent
    from ONE grouped sibling test's rows here -- it may belong to another
    sibling sharing the same flag NAME (T6.1d_dom/_int's shape) -- but a row
    that matches NEITHER sibling's re-derived group is a genuine data
    anomaly, and must raise loudly rather than be silently left out of every
    sibling's rows (which would silently under-count the headline with no
    signal). `claimed_by_test` tracks which (source, row_key) each grouped
    test actually claimed; after every test has run, any flag shared by two
    or more grouped tests is checked for a row none of its owners claimed."""
    contract_validated_sources: set[str] = set()
    rows_out: list[dict] = []
    claimed_by_test: dict[str, set[tuple]] = {}
    grouped_test_flags: dict[str, set[str]] = {}

    for test_id, test in tests_by_id.items():
        flags = flags_by_test_id.get(test_id, set())
        if not flags:
            continue
        primitive = test.get("primitive")

        candidate_rows: list[dict] = []
        seen: set[tuple] = set()
        for flag in sorted(flags):
            for r in rows_by_flag.get(flag, []):
                key = (r["source"], r["row_key"])
                if key in seen:
                    continue
                seen.add(key)
                candidate_rows.append(r)
        if not candidate_rows:
            continue

        dup_first_by_group: dict[str, str] | None = None
        if primitive == "duplicate_detection":
            primary_flag = test["params"].get("flag", test.get("flag"))
            dup_first_by_group = dup_group_first_row(rows_by_flag.get(primary_flag, []), row_position)

        excess_map: dict[str, float] | None = None
        if primitive == "threshold_exceedance" and test["params"].get("group_by"):
            excess_map = per_diem_row_excess(
                ctx, skill, state, test, populations_cfg, source_frames,
                contract_validated_sources, bindings,
            )

        if dup_first_by_group is not None or excess_map is not None:
            grouped_test_flags[test_id] = flags

        for r in candidate_rows:
            source, row_key = r["source"], r["row_key"]
            if source not in amount_col_by_source:
                continue
            spend_amount = row_amount.get((source, row_key))
            if spend_amount is None:
                continue

            excess_amount = None
            if dup_first_by_group is not None:
                gid = r.get("group_id")
                if gid is None or gid not in dup_first_by_group:
                    # Either a genuine data anomaly, or (when this flag is
                    # shared with a sibling test) this row is really the
                    # sibling's -- either way, not this test's row to claim.
                    continue
                excess_amount = 0.0 if dup_first_by_group[gid] == row_key else spend_amount
            elif excess_map is not None:
                if row_key not in excess_map:
                    continue
                excess_amount = excess_map[row_key]

            rows_out.append({
                "test_id": test_id,
                "source": source,
                "row_key": row_key,
                "line_key": line_key(source, row_key, entry_key_cols_by_source, entry_key_by_row),
                "spend_amount": spend_amount,
                "excess_amount": excess_amount,
            })
            claimed_by_test.setdefault(test_id, set()).add((source, row_key))

    # Every flag shared by two or more grouped tests (siblings): a row
    # flagged under it that amount_col_by_source can price, but that none of
    # those siblings claimed above, resolves to no sibling's re-derived
    # group at all -- never silently dropped (see the fix note above).
    flag_owner_tests: dict[str, list[str]] = {}
    for owner_test_id, owner_flags in grouped_test_flags.items():
        for flag in owner_flags:
            flag_owner_tests.setdefault(flag, []).append(owner_test_id)

    for flag, owner_tests in flag_owner_tests.items():
        if len(owner_tests) < 2:
            continue
        for r in rows_by_flag.get(flag, []):
            source, row_key = r["source"], r["row_key"]
            if source not in amount_col_by_source:
                continue
            claimed = any((source, row_key) in claimed_by_test.get(tid, set()) for tid in owner_tests)
            if not claimed:
                raise ContractViolation(
                    [f"{source} row {row_key!r}: flagged under {flag!r} but matches the "
                     f"re-derived group of none of its sibling sub-tests {sorted(owner_tests)} "
                     f"-- this row's amount-at-risk contribution would otherwise be silently "
                     f"dropped from the headline"]
                )

    return rows_out


def finding_line_values(
    flagged_rows: list[dict], basis: str, producing_test_ids, rows_by_test_id: dict,
    entry_key_cols_by_source: dict, entry_key_by_row: dict, row_amount: dict,
) -> dict[str, float]:
    """ONE finding's own line contributions: `line_key -> amount at risk`,
    for `basis` 'spend' (each line's own full amount) or 'excess' (each
    line's at-risk portion, read from the pre-built `test_line_values` rows
    grouped by test_id in `rows_by_test_id`). Pure over its inputs -- no
    source read.

    Raises `ContractViolation` for a 'excess' row that no producing test's
    own `test_line_values` rows resolved an excess amount for (the same
    "row not in an exceeding group on re-derivation" failure `prioritise`
    always raised) -- a rule finding's exposure computation always calls
    this and lets it propagate; §5.1's AI-proposed candidates instead catch
    it and mark the candidate `headline_eligible = false` (never fails the
    run)."""
    line_map: dict[str, float] = {}
    lookup: dict[tuple, dict] = {}
    for tid in producing_test_ids:
        for row in rows_by_test_id.get(tid, []):
            lookup[(tid, row["source"], row["row_key"])] = row

    for r in flagged_rows:
        source, row_key = r["source"], r["row_key"]
        lkey = line_key(source, row_key, entry_key_cols_by_source, entry_key_by_row)
        if basis == "spend":
            amt = row_amount.get((source, row_key))
            if amt is None:
                raise ContractViolation(
                    [f"{source} row {row_key!r}: no amount available for a 'spend' finding's "
                     f"headline contribution"]
                )
        else:
            amt = None
            for tid in sorted(producing_test_ids):
                row = lookup.get((tid, source, row_key))
                if row is not None and row["excess_amount"] is not None:
                    amt = row["excess_amount"]
                    break
            if amt is None:
                raise ContractViolation(
                    [f"{source} row {row_key!r}: no at-risk amount computed by any of this "
                     f"finding's producing tests ({sorted(producing_test_ids)}) -- row not "
                     f"in an exceeding group on re-derivation"]
                )
        current = line_map.get(lkey)
        if current is None or amt > current:
            line_map[lkey] = amt
    return line_map


def line_map_from_test_line_values(
    basis: str, producing_test_ids, rows_by_test_id: dict[str, list[dict]],
) -> dict[str, float]:
    """P6 WP N10 (docs/specs/P6_narration_design.md §5.3): the `finalise`
    node's own counterpart to `finding_line_values` above -- resolves ONE
    finding's (or accepted candidate's) line contributions PURELY from
    already-PERSISTED `test_line_values` rows (`rows_by_test_id`, from
    `persistence.list_test_line_values`), never reading source data and
    never re-deriving `line_key` from raw entry-key columns the way
    `finding_line_values`/`line_key` above do. This is deliberate, not a
    shortcut: every `test_line_values` row already carries its OWN
    `line_key` (computed once, correctly, when `prioritise` built the table)
    alongside its `spend_amount`/`excess_amount` -- re-deriving `line_key`
    here from an INCOMPLETE `entry_key_cols_by_source` (finalise has no
    source read, so it would have none) would silently compute the WRONG
    line identity for any source that declares a repeats-grain `entry_key`
    (T3.3b's attendee-grain shape), double-counting or under-counting the
    headline for a Skill that uses one -- exactly the class of bug CLAUDE.md
    §0.3 exists to prevent.

    A `basis` outside ('spend', 'excess') -- 'approved_not_spent' or 'none'
    -- returns an empty map, the same "never contributes to the headline"
    rule `compute_run_exposure` applies. A row whose relevant amount is
    absent (an 'excess' row belonging to a producing test whose primitive
    genuinely computed none) is silently skipped, never raised: unlike
    `finding_line_values` (called by `prioritise`, which is entitled to
    treat that as a genuine data anomaly because it JUST derived these rows
    itself), `finalise` is re-deriving over rows a PRIOR, already-successful
    `prioritise` pass wrote -- if that pass raised, `test_line_values` would
    never have been persisted at all, so reaching `finalise` already proves
    every row here is consistent; an AI-proposed candidate's own C-2/
    `_headline_eligibility` gate (§5.1) is what decides whether IT gets to
    call this at all, and never raises either."""
    if basis not in ("spend", "excess"):
        return {}
    line_map: dict[str, float] = {}
    for tid in producing_test_ids:
        for row in rows_by_test_id.get(tid, []):
            amount = row["spend_amount"] if basis == "spend" else row.get("excess_amount")
            if amount is None:
                continue
            current = line_map.get(row["line_key"])
            if current is None or amount > current:
                line_map[row["line_key"]] = amount
    return line_map


def headline(line_maps: list[dict[str, float]]) -> float:
    """The run's amount-at-risk figure: every distinct line across every
    'spend'/'excess' finding's own `finding_line_values` map, at the
    LARGEST amount any of those maps attributes to it -- never summed
    across findings (CLAUDE.md §0.3)."""
    merged: dict[str, float] = {}
    for m in line_maps:
        for k, v in m.items():
            current = merged.get(k)
            if current is None or v > current:
                merged[k] = v
    return round(sum(merged.values()), 2)


def compute_run_exposure(ctx, state, skill, persisted_findings: list[dict], existing_metrics: dict, rows_by_flag: dict) -> dict:
    """The whole of `prioritise`'s exposure computation (P6 WP N4): reads
    bound source amounts once, resolves every persisted finding's
    `exposure_amount` (unchanged: the sum of its own cited amount metric(s))
    and its headline line contributions, builds `test_line_values` for
    every testable plan test, and reduces to the run headline. Returns a
    dict `prioritise` persists directly:

      * `updated_findings` -- `persisted_findings`, sorted, each with
        `exposure_amount`/`exposure_basis` set;
      * `headline_exposure` -- the run's "Potential exposure" figure;
      * `headline_provenance` -- `{name, version, amount_column, entry_key}`
        per source that actually contributed a line to the headline;
      * `approved_not_spent_total` -- sum of every 'approved_not_spent'
        finding's own `exposure_amount`, or None if there were none;
      * `test_line_values` -- the rows to persist to the `test_line_values`
        table."""
    bindings = {b["source"]: b["version"] for b in state.data_assets}
    populations_cfg = skill.plan.get("populations", {})
    plan_sources = {pop_cfg.get("source") for pop_cfg in populations_cfg.values() if pop_cfg.get("source")}
    amount_col_by_source: dict[str, str] = {}
    for pop_cfg in populations_cfg.values():
        src = pop_cfg.get("source")
        col = pop_cfg.get("amount_column")
        if col and src and src not in amount_col_by_source:
            amount_col_by_source[src] = col

    # B2 (CLAUDE.md P2/P3 gate review): each source's declared entry_key
    # (contract.yaml), scoped to sources plan.yaml actually reads.
    entry_key_cols_by_source: dict[str, list[str]] = {
        name: cfg["entry_key"] for name, cfg in skill.contract.get("sources", {}).items()
        if cfg.get("entry_key") and name in plan_sources
    }

    row_amount, row_position, entry_key_by_row, source_frames = read_line_amounts(
        ctx, skill, bindings, amount_col_by_source, entry_key_cols_by_source,
    )

    plan_tests = skill.plan.get("tests", [])
    tests_by_id = {t["test_id"]: t for t in plan_tests if "not_testable" not in t}
    flags_by_test_id: dict[str, set[str]] = {}
    for e in plan_test_flags(plan_tests):
        flags_by_test_id.setdefault(e["test_id"], set()).add(e["flag"])
    amount_metrics_by_test_id = plan_test_amount_metrics(plan_tests)
    all_additive_amount_names: set[str] = set()
    for names in amount_metrics_by_test_id.values():
        all_additive_amount_names |= names

    test_line_value_rows = build_test_line_values(
        ctx, skill, state, bindings, tests_by_id, flags_by_test_id, rows_by_flag,
        amount_col_by_source, entry_key_cols_by_source, entry_key_by_row,
        row_amount, row_position, populations_cfg, source_frames,
    )
    rows_by_test_id: dict[str, list[dict]] = {}
    for row in test_line_value_rows:
        rows_by_test_id.setdefault(row["test_id"], []).append(row)

    updated_findings: list[dict] = []
    line_maps: list[dict[str, float]] = []
    approved_not_spent_total = None
    contributing_sources: set[str] = set()

    for f in persisted_findings:
        cited_metrics = f.get("metrics_cited") or {}

        # B1: which plan.yaml test(s) actually produced each of this
        # finding's cited metrics -- read from the RECORDED metric->test
        # mapping this run's own execute() persisted, never re-derived by
        # matching the finding's single `test_id` against plan.yaml ids by
        # string prefix (see this module's callers for the full rationale).
        producing_test_ids = {
            existing_metrics[name]["test_id"]
            for name in cited_metrics
            if name in existing_metrics and existing_metrics[name].get("test_id")
        }
        flags = {flag for tid in producing_test_ids for flag in flags_by_test_id.get(tid, set())}
        rows = [r for flag in flags for r in rows_by_flag.get(flag, [])]

        amount_metric_names = all_additive_amount_names & set(cited_metrics)

        if not amount_metric_names:
            updated_findings.append(
                {**f, "exposure_amount": None, "exposure_basis": "non-monetary finding"}
            )
            continue

        exposure = round(
            sum(
                cited_metrics[name]["value"]
                for name in amount_metric_names
                if cited_metrics[name].get("value") is not None
            ),
            2,
        )

        monetary_basis = f.get("monetary_basis")
        if monetary_basis in ("spend", "excess"):
            line_map = finding_line_values(
                rows, monetary_basis, producing_test_ids, rows_by_test_id,
                entry_key_cols_by_source, entry_key_by_row, row_amount,
            )
            line_maps.append(line_map)
            for r in rows:
                contributing_sources.add(r["source"])
        elif monetary_basis == "approved_not_spent":
            approved_not_spent_total = round((approved_not_spent_total or 0.0) + exposure, 2)

        updated_findings.append(
            {
                **f,
                "exposure_amount": exposure,
                "exposure_basis": (
                    f"Sum of this finding's own cited amount metric(s) {sorted(amount_metric_names)} "
                    f"-- the same figure(s) rendered into its observation text, never re-derived from "
                    f"raw rows."
                ),
            }
        )

    updated_findings.sort(
        key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 3), -(f["exposure_amount"] or 0.0))
    )

    headline_exposure = headline(line_maps)
    headline_provenance = [
        {
            "name": src,
            "version": bindings.get(src),
            "amount_column": amount_col_by_source.get(src),
            "entry_key": entry_key_cols_by_source.get(src),
        }
        for src in sorted(contributing_sources)
    ]

    return {
        "updated_findings": updated_findings,
        "headline_exposure": headline_exposure,
        "headline_provenance": headline_provenance,
        "approved_not_spent_total": approved_not_spent_total,
        "test_line_values": test_line_value_rows,
    }
