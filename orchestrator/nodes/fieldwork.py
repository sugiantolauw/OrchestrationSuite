"""The `fieldwork` run_kind's nodes (CLAUDE.md §4.2): discover -> profile -> plan
(phase `plan`), execute -> classify -> find -> prioritise -> act (phase
`execute`), export (phase `export`). Every node is `fn(ctx: NodeContext, state:
RunState) -> RunState`, returns only the NODE_OWNED fields it changed (the
pipeline loop in orchestrator/pipeline.py enforces the rest), and is idempotent
per run_id: every persistence write here either replaces this run's whole prior
output for that table (write_flagged_rows, write_run_metrics,
write_management_actions) or upserts by a deterministic id (write_findings,
write_issues_for_findings), so a re-executed node overwrites, never
duplicates (CLAUDE.md §2.3 rule 1).

`execute` is the only node that reads source data and it NEVER calls an LLM
(CLAUDE.md §3 non-negotiable 2); `classify`/`find`/`prioritise`/`act` work only
from what `execute` already persisted (run_metrics, flagged_rows), so a
re-execution of any later node is a pure re-derivation, not a re-read of raw
data -- except `prioritise`, which re-reads bound sources once to look up each
row's amount for exposure de-duplication (no primitive or population exposes a
row_key -> amount map, so this is the one place outside `execute` that touches
raw data; see the function's own docstring).
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
from decimal import Decimal

import pandas as pd
import xlsxwriter

from orchestrator.adapters.issue_tracker_preview import PreviewOnlyIssueTracker, TICKET_PREVIEW_STATUS
from orchestrator.contract import ContractViolation, validate_contract
from orchestrator.errors import MissingSeverityProvenance, ReconciliationError
from orchestrator.engine import execute_skill
from orchestrator.findings import build_findings
from orchestrator.frames import build_row_snapshots, frame_parquet_bytes, sha256_bytes
from orchestrator.nodes.context import NodeContext
from orchestrator.populations import PopulationContext, build_populations
from orchestrator.pptx_export import generate_pptx, load_catalogue_rows
from orchestrator.signoff_policy import SELF_APPROVED_LABEL
from orchestrator.skills import plan_test_amount_metrics, plan_test_flags
from orchestrator.state import RunState

_SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def _event(node: str, message: str, now: str) -> dict:
    return {"node": node, "message": message, "at": now}


# ── plan phase ──────────────────────────────────────────────────────────────


def discover(ctx: NodeContext, state: RunState) -> RunState:
    """Validates every contract source has a binding in state.data_assets
    ([{source, table_fqn, version}], resolved and pinned at run creation by
    orchestrator.service.start_audit_run) and that the bound table is still
    reachable at that pinned version. A source with no binding, or one that no
    longer resolves, is a contract violation: the run fails rather than
    proceeding on a guess (CLAUDE.md NN14/G7)."""
    contract_sources = ctx.skill.contract.get("sources", {})
    bindings = {b["source"]: b for b in state.data_assets}

    missing = sorted(set(contract_sources) - set(bindings))
    if missing:
        raise ContractViolation([f"no binding for contract source {s!r}" for s in missing])

    # Re-resolve_version (cheap -- a file hash or DESCRIBE HISTORY, never a
    # full read/parse; row_count() would BE a full read for a local xlsx
    # source, redundant with profile()/execute() reading the same bytes
    # again) rather than row_count(): this both confirms the bound table is
    # still reachable and, as a bonus, catches the table having moved on to a
    # different version since it was pinned at run creation -- a case
    # row_count() alone would not distinguish from "unreachable".
    violations: list[str] = []
    for source, binding in bindings.items():
        if source not in contract_sources:
            continue
        try:
            current_version = ctx.data_source.resolve_version(source)
        except Exception as exc:  # noqa: BLE001 - surfaced as a named contract violation
            violations.append(f"{source}: bound table unreachable: {exc!r}")
            continue
        if current_version != binding["version"]:
            violations.append(
                f"{source}: bound at version {binding['version']!r} but now resolves to "
                f"{current_version!r} -- the source changed after it was pinned"
            )
    if violations:
        raise ContractViolation(violations)

    now = ctx.clock()
    message = f"{len(bindings)} source(s) bound: {', '.join(sorted(bindings))}"
    return dataclasses.replace(state, events=state.events + [_event("discover", message, now)])


def profile(ctx: NodeContext, state: RunState) -> RunState:
    """Per contract source: row count and null count per contract column. Read
    via ctx.data_source.read_population + computed in pandas (CLAUDE.md build
    brief P3 §1) -- UCTableDataSource does not yet expose a SQL-pushdown
    `profile()` entry point, so this reads the whole bound population once per
    source rather than aggregating server-side. Fine at today's ~150K-row
    scale (§2.3 rule 4); a future SQL-pushdown profile() would replace this
    node's body only, not its output shape."""
    contract_sources = ctx.skill.contract.get("sources", {})
    bindings = {b["source"]: b["version"] for b in state.data_assets}

    profile_result: dict[str, dict] = {}
    for source, source_cfg in contract_sources.items():
        version = bindings[source]
        df = ctx.data_source.read_population(source, version=version)
        columns_cfg = source_cfg.get("columns", {})
        null_counts = {
            col: int(df[col].isna().sum()) for col in columns_cfg if col in df.columns
        }
        profile_result[source] = {
            "row_count": int(len(df)),
            "null_counts": null_counts,
        }

    now = ctx.clock()
    total_rows = sum(p["row_count"] for p in profile_result.values())
    message = f"{len(profile_result)} source(s) profiled, {total_rows} row(s) total"
    return dataclasses.replace(
        state, profile_result=profile_result, events=state.events + [_event("profile", message, now)]
    )


def plan(ctx: NodeContext, state: RunState) -> RunState:
    """Playbook: the resolved plan IS the Skill's plan.yaml tests, unchanged
    (CLAUDE.md §4.4 -- building the test plan is generic pipeline code reading
    plan.yaml, never a per-Skill method). Explorer's authoring loop (§4.5) is
    not built in P3 (P8)."""
    if state.mode != "playbook":
        raise ValueError(
            f"plan node: mode={state.mode!r} is not supported yet -- Explorer plan "
            f"authoring (CLAUDE.md §4.5) is a P8 deliverable"
        )

    tests: list[dict] = []
    for t in ctx.skill.plan.get("tests", []):
        entry = {
            "test_id": t["test_id"],
            "control_id": t.get("control_id"),
            "risk_id": t.get("risk_id"),
            "assertion": t.get("assertion"),
        }
        if "not_testable" in t:
            entry["not_testable"] = t["not_testable"]
        else:
            entry["primitive"] = t.get("primitive")
            entry["flag"] = t.get("flag")
        tests.append(entry)

    plan_payload = {
        "skill_id": ctx.skill.skill_id,
        "skill_version": ctx.skill.version,
        "tests": tests,
    }
    now = ctx.clock()
    message = f"playbook plan: {len(tests)} test(s) from {ctx.skill.skill_id} v{ctx.skill.version}"
    return dataclasses.replace(state, plan=plan_payload, events=state.events + [_event("plan", message, now)])


# ── execute phase ────────────────────────────────────────────────────────────


def _decimal_variance(engine_value: float | None, independent_value: float | None) -> float | None:
    """Exact, Decimal-safe difference for G6's amount reconciliation (CLAUDE.md
    §5 G6, P2/P3 gate review item 2) -- built from each float's own repr
    string, never from the float itself, so summation-order artefacts below a
    cent (e.g. 1959.9999999999998 vs 1960.0) never register as a variance
    while a genuine cent-level mismatch always does. Returns None only when
    either side is None (no amount_column declared, or nothing to sum)."""
    if engine_value is None or independent_value is None:
        return None
    diff = round(Decimal(str(engine_value)) - Decimal(str(independent_value)), 2)
    return float(diff)


def execute(ctx: NodeContext, state: RunState) -> RunState:
    """Runs every test in plan order via orchestrator.engine.execute_skill
    (CLAUDE.md §4.2) and persists its two durable outputs: run_metrics (every
    metric any test produced) and flagged_rows (the row-level RF_* evidence,
    long-format). NEVER calls an LLM.

    G6 reconciliation compares each contract source's row count, Sigma(amount)
    (where the source's `raw_<source>` population declares an `amount_column`)
    and min/max date (where it declares a `date_column`) as the engine saw
    them (via that unfiltered `raw_<source>` population in plan.yaml) against
    INDEPENDENTLY obtained figures from DataSourceAdapter.row_count() /
    .column_stats() at the same pinned version -- never a number re-derived
    from the same in-memory frame. The amount comparison is Decimal-safe
    (`_decimal_variance`), never a raw float `==`. A non-zero variance in ANY
    of the four -- rows, amount, min date, max date -- fails the run outright
    (CLAUDE.md §5 G6, P2/P3 gate review item 2); it is never silently reported
    and ignored. A source whose `raw_<source>` population declares neither
    column (no single natural amount/date column for that source) is
    reconciled on rows alone, same as before this fix.

    Passes state.data_assets' versions (resolved once, at start_audit_run, per
    source) straight through as execute_skill's pinned_versions -- a source
    that changes between run creation and this node running does not change
    what gets read (CLAUDE.md §4.1 TOCTOU ordering)."""
    pinned_versions = {b["source"]: b["version"] for b in state.data_assets}
    result = execute_skill(
        ctx.skill,
        data_source=ctx.data_source,
        audit_period=state.audit_period,
        run_context={"run_id": state.run_id},
        pinned_versions=pinned_versions,
    )

    metric_test_id: dict[str, str] = {}
    for t in result.test_results:
        for m in t.get("metric_names", []):
            metric_test_id[m] = t["test_id"]

    metrics_rows = [
        {
            "metric_name": name,
            "value": m["value"],
            "unit": m["unit"],
            "source_ref": m["source_ref"],
            "test_id": metric_test_id.get(name),
        }
        for name, m in result.metrics.items()
    ]
    ctx.persistence.write_run_metrics(state.run_id, metrics_rows)

    # Long-format flagged rows, straight from the engine's own long frame
    # (result.flags_long: __source, __row_key, flag, group_id -- one row per
    # exception instance, exactly as each primitive produced it) rather than
    # un-pivoting result.flags (the wide RF_* frame): that pivot never
    # carried group_id through, so reconstructing from it could only ever
    # persist a null group_id (CLAUDE.md NN14 -- never guess what was never
    # computed, when the real value is available one step earlier).
    flagged_rows: list[dict] = [
        {"source": r["__source"], "row_key": r["__row_key"], "flag": r["flag"], "group_id": r["group_id"]}
        for r in result.flags_long.to_dict("records")
    ]
    ctx.persistence.write_flagged_rows(state.run_id, flagged_rows)

    if ctx.settings.catalog and ctx.settings.schema:
        table_ref = f"{ctx.settings.catalog}.{ctx.settings.schema}.flagged_rows"
    else:
        table_ref = "flagged_rows"

    populations_cfg = ctx.skill.plan.get("populations", {})
    reconciliation: dict[str, dict] = {}
    differences: list[str] = []
    for source, version in result.source_versions.items():
        raw_pop = result.populations.get(f"raw_{source}")
        raw_pop_cfg = populations_cfg.get(f"raw_{source}") or {}
        amount_col = raw_pop_cfg.get("amount_column")
        date_col = raw_pop_cfg.get("date_column")

        engine_rows = raw_pop["rows"] if raw_pop is not None else None
        engine_amount = raw_pop["amount"] if raw_pop is not None else None
        engine_min_date = raw_pop["min_date"] if raw_pop is not None else None
        engine_max_date = raw_pop["max_date"] if raw_pop is not None else None

        independent_rows = ctx.data_source.row_count(source, version=version)
        row_variance = None if engine_rows is None else engine_rows - independent_rows

        if amount_col or date_col:
            independent = ctx.data_source.column_stats(
                source, version=version, amount_column=amount_col, date_column=date_col
            )
        else:
            independent = {"amount": None, "min_date": None, "max_date": None}

        amount_variance = _decimal_variance(engine_amount, independent["amount"]) if amount_col else None
        min_date_match = engine_min_date == independent["min_date"] if date_col else None
        max_date_match = engine_max_date == independent["max_date"] if date_col else None

        reconciliation[source] = {
            "engine_rows": engine_rows,
            "independent_rows": independent_rows,
            "variance": row_variance,
            "amount": engine_amount,
            "independent_amount": independent["amount"] if amount_col else None,
            "amount_variance": amount_variance,
            "min_date": engine_min_date,
            "max_date": engine_max_date,
            "independent_min_date": independent["min_date"] if date_col else None,
            "independent_max_date": independent["max_date"] if date_col else None,
            "min_date_match": min_date_match,
            "max_date_match": max_date_match,
        }
        if row_variance not in (None, 0):
            differences.append(f"{source}: engine_rows={engine_rows} independent_rows={independent_rows}")
        if amount_variance not in (None, 0.0):
            differences.append(
                f"{source}: engine_amount={engine_amount} independent_amount={independent['amount']} "
                f"variance={amount_variance}"
            )
        if min_date_match is False:
            differences.append(f"{source}: engine_min_date={engine_min_date} independent_min_date={independent['min_date']}")
        if max_date_match is False:
            differences.append(f"{source}: engine_max_date={engine_max_date} independent_max_date={independent['max_date']}")

    # Item 8 (CLAUDE.md §5 G6 caveat, P2/P3 gate review): the source-level
    # check above only catches a whole SOURCE'S row count drifting from an
    # independent count -- it says nothing about a single TESTED population
    # (a filtered/derived subset of that source) silently losing rows a
    # filter never declared. Every population's own accounting must close:
    # its final row count plus every filter/post_filter's own declared
    # excluded_counts must sum back to the source's independently-verified
    # row count (from the per-source loop above, never re-queried). A
    # mismatch means some row-count-changing step -- a filter whose exclusion
    # was not counted, a derive/lookup that fanned rows out via a
    # non-unique-keyed merge, anything -- happened without being accounted
    # for, and the run fails outright rather than reporting a population
    # whose own numbers do not add up.
    independent_rows_by_source = {src: rec["independent_rows"] for src, rec in reconciliation.items()}
    for pop_name, pop in sorted(result.populations.items()):
        pop_source = (populations_cfg.get(pop_name) or {}).get("source")
        independent = independent_rows_by_source.get(pop_source)
        if independent is None:
            differences.append(
                f"population {pop_name!r}: source {pop_source!r} was never independently "
                f"reconciled -- cannot verify this population's row accounting"
            )
            continue
        excluded_total = sum(pop["excluded_counts"].values())
        accounted = pop["rows"] + excluded_total
        if accounted != independent:
            differences.append(
                f"population {pop_name!r} (source {pop_source!r}): {pop['rows']} tested row(s) + "
                f"{excluded_total} declared exclusion(s) = {accounted}, but the source "
                f"independently reconciles to {independent} row(s) -- some row-count-changing "
                f"step was not accounted for in excluded_counts"
            )

    if differences:
        raise ReconciliationError(state.run_id, differences)

    now = ctx.clock()

    # Per-run row snapshots for /workspace/tne (CLAUDE.md build brief P4 perf
    # fix, orchestrator/frames.py): only the rows the run's populations
    # actually tested, per contract source, written once here so the
    # workspace's first callback reads a few small Parquet files from the
    # Volume instead of re-reading every full bound source (the ~39s/~64s
    # get_run_frames regression this fixes). Idempotent by construction: the
    # path is deterministic per (run_id, source) and export_storage.write()
    # overwrites, and record_export() upserts by (run_id, kind) -- a
    # re-executed `execute` node replaces its own prior snapshot, never
    # duplicates it (CLAUDE.md §2.3 rule 1).
    snapshots = build_row_snapshots(ctx.skill, result, flagged_rows)
    frame_exports: dict[str, dict] = {}
    for source in sorted(snapshots):
        content = frame_parquet_bytes(snapshots[source])
        sha256 = sha256_bytes(content)
        rel_path = f"runs/{state.run_id}/frames/{source}.parquet"
        written_path = ctx.export_storage.write(rel_path, content)
        kind = f"frames:{source}"
        ctx.persistence.record_export(
            state.run_id, kind, path=written_path, sha256=sha256, created_by=state.run_owner, now=now,
        )
        frame_exports[source] = {
            "path": written_path, "sha256": sha256, "row_count": int(len(snapshots[source])), "kind": kind,
        }

    message = (
        f"{len(result.test_results)} test(s) executed, {len(metrics_rows)} metric(s), "
        f"{len(flagged_rows)} flagged row(s), {len(frame_exports)} row snapshot(s) written"
    )
    return dataclasses.replace(
        state,
        test_results=result.test_results,
        flagged_table=table_ref,
        reconciliation=reconciliation,
        exports={**(state.exports or {}), "frames": frame_exports},
        events=state.events + [_event("execute", message, now)],
    )


def classify(ctx: NodeContext, state: RunState) -> RunState:
    """No LLM endpoint is wired yet (CLAUDE.md build brief P3 §2): T4.3 stays
    `not_testable` (its `classify` GenAI residual is a P6 deliverable, §4.2's
    node table). This node's job today is the deterministic part only --
    per-test exception summaries from what `execute` already persisted."""
    exceptions = [
        {
            "test_id": t["test_id"],
            "status": t["status"],
            "exception_units": t["exception_units"],
            "reason": t.get("reason"),
        }
        for t in state.test_results
    ]
    now = ctx.clock()
    n_exceptions = sum(1 for e in exceptions if e["status"] == "exception")
    message = f"{n_exceptions} test(s) with exceptions, {len(exceptions)} test(s) total"
    return dataclasses.replace(state, exceptions=exceptions, events=state.events + [_event("classify", message, now)])


def find(ctx: NodeContext, state: RunState) -> RunState:
    """Findings from `execute`'s already-persisted metrics, rule-evaluated
    against skill.findings.yaml (CLAUDE.md §4.6) -- re-derived from
    run_metrics rather than recomputed from raw data, so a re-executed `find`
    node is a pure function of what `execute` wrote, not a second read of
    source data. Persists via write_findings (upsert by finding_id) and
    write_issues_for_findings (one draft issue per finding, §4.9)."""
    metrics = ctx.persistence.get_run_metrics(state.run_id)
    findings = build_findings(ctx.skill, run_id=state.run_id, metrics=metrics)

    now = ctx.clock()
    persisted = ctx.persistence.write_findings(
        state.run_id,
        findings,
        engagement_id=state.engagement_id,
        skill_id=state.skill_id,
        skill_version=state.skill_version,
        now=now,
    )
    ctx.persistence.write_issues_for_findings(
        state.run_id, persisted, engagement_id=state.engagement_id, now=now,
    )

    compact = [
        {
            "finding_id": f["finding_id"],
            "rule_id": f["rule_id"],
            "test_id": f.get("test_id"),
            "severity": f["severity"],
            "title": f["title"],
            "analyst_set_severity": f.get("analyst_set_severity"),
        }
        for f in persisted
    ]
    message = f"{len(persisted)} finding(s) generated"
    return dataclasses.replace(state, findings=compact, events=state.events + [_event("find", message, now)])


def prioritise(ctx: NodeContext, state: RunState) -> RunState:
    """Deterministic ordering: severity, then de-duplicated exposure (CLAUDE.md
    §0.3 -- fixes app.py's double count, where the same row could be summed
    into more than one finding's `max(amount over cited metrics)`).

    A finding's `exposure_amount` is always the sum of its OWN cited amount
    metric(s) -- exactly the number(s) `metrics_cited` already rendered into
    its `observation` text (CLAUDE.md P2/P3 gate review item 1). It is never
    re-derived from raw rows: `duplicate_amount` (T5.2) is "sum of extra lines
    beyond the first in each group" and `daily_over_amount_*` (T6.1d) is an
    excess-over-limit, not a sum of full amounts -- shapes a generic row/group
    summation cannot reconstruct without reproducing each primitive's own
    rule. A cited metric counts as an amount metric when it is an additive-AUD
    metric declared by ANY plan.yaml test (`orchestrator.skills.
    plan_test_amount_metrics`, flattened across the whole plan) -- NOT
    filtered to "the tests whose id equals or is prefixed by this finding's
    own test_id" (CLAUDE.md P2/P3 gate review item B1, fixed here). That
    prefix filter silently dropped a cited metric whenever the T&E Skill
    split one catalogue test into sibling plan.yaml sub-tests that do not
    share a prefix with each other -- T6.1d_dom/T6.1d_int, T3.2a_air_dom/
    _air_int/_car_dom/_car_int, T3.3a_dom/_int/_very_late: a finding cites one
    of those test_ids but its metrics_cited legitimately spans several, so
    "starts with this finding's test_id + '_'" matched none of the true
    siblings and under-stated exposure (T6_1d: $446.40 counted, $998.53
    dropped). Reading the metric the primitive already computed is correct by
    construction for every primitive, and this fix is what actually
    guarantees the observation prose and `exposure_amount` can never
    disagree -- the claim the pre-fix docstring made without it being true. A
    test with no declared amount metric gets `exposure_amount: None` /
    `exposure_basis: "non-monetary finding"` -- never a fabricated 0.0
    (CLAUDE.md NN14).

    The run headline is a SEPARATE figure from any finding's exposure_amount:
    it is the amount at risk (independent review 2026-09-24, item 1 --
    replaces an earlier "gross flagged spend" design whose entry-grain
    de-duplication silently collapsed real, distinct expense_report rows
    that happened to share a mis-declared "unique" entry_key). Every
    DISTINCT flagged transaction LINE counts exactly once, at the LARGEST
    amount at risk any finding attributes to it:

    1. Line identity. A row-grain source's line is its own row identity
       (source, row_key) -- the default, and now expense_report's shape.
       A source may instead declare a repeats-grain `entry_key` in
       contract.yaml (`entry_key_repeats: true`, e.g. attendee_validity's
       one-row-per-attendee shape) when several rows are genuinely the same
       business entry; those collapse to one line. A source that declares
       `entry_key` WITHOUT `entry_key_repeats: true` asserts that key is
       unique per row -- checked against the data actually read, and the
       run fails loudly if it is not (the check expense_report's old
       declaration should have had, rather than silently collapsing).
    2. Line value. `monetary_basis` (findings.yaml, schema-required) says
       what a finding's cited amount means: 'spend' (a real reimbursed
       transaction -- attributes the line's own full amount), 'excess'
       (only the at-risk portion is attributed -- the lines beyond the
       first, deterministically ordered, in a duplicate_detection group;
       the over-limit part of a group_by threshold_exceedance group,
       allocated pro rata to that group's lines by their own amount),
       'approved_not_spent' (money approved but never actually spent --
       T3.1a's unlinked travel requests; reported separately as
       `run_approved_not_spent_total`, never in this headline), or 'none'
       (no dollar figure). Only 'spend'/'excess' findings contribute. The
       same line can be flagged by more than one finding, or attributed
       different amounts by 'spend' vs 'excess' rules -- its value in the
       headline is the MAX any finding attributes to it, never a sum
       across findings (that would double-count the same real dollar).
       Every finding's own `exposure_amount` above is unaffected by any of
       this -- it is always that finding's own cited metric sum, on its
       own, and two findings' exposure_amount figures may legitimately
       overlap (CLAUDE.md §0.3) -- never summed together either.

    This is the one node besides `execute` that reads bound source data: no
    primitive or population exposes a row_key -> amount map (flagged_rows
    deliberately carries neither, CLAUDE.md build brief P3 §1), so the
    headline requires one lookup pass over each source a spend/excess
    finding's rows come from, at the same pinned versions execute() used --
    plus, for a group_by threshold_exceedance excess allocation, one
    population rebuild (`orchestrator.populations.build_populations`, the
    same function execute() used, never a re-implementation of its
    derivation logic) per such test, from that same already-read raw
    source. A value that fails to parse as a number, or is null, is a
    contract violation the contract's own type/nullability declaration
    should already have caught -- raised here, never silently coerced to
    0.0 (CLAUDE.md NN14)."""
    persisted = ctx.persistence.list_findings(state.run_id)
    now = ctx.clock()

    if not persisted:
        message = "no findings to prioritise"
        return dataclasses.replace(state, findings=[], events=state.events + [_event("prioritise", message, now)])

    existing_metrics = ctx.persistence.get_run_metrics(state.run_id)

    bindings = {b["source"]: b["version"] for b in state.data_assets}
    populations_cfg = ctx.skill.plan.get("populations", {})
    plan_sources = {pop_cfg.get("source") for pop_cfg in populations_cfg.values() if pop_cfg.get("source")}
    amount_col_by_source: dict[str, str] = {}
    for pop_cfg in populations_cfg.values():
        src = pop_cfg.get("source")
        col = pop_cfg.get("amount_column")
        if col and src and src not in amount_col_by_source:
            amount_col_by_source[src] = col

    # B2 (CLAUDE.md P2/P3 gate review): each source's declared entry_key
    # (contract.yaml) -- the REAL columns that identify one distinct
    # transaction entry for that source, never a positional row index.
    # Scoped to sources plan.yaml actually reads (same scoping
    # amount_col_by_source already has above), not every contract-declared
    # source regardless of whether this Skill's plan even uses it.
    entry_key_cols_by_source: dict[str, list[str]] = {
        name: cfg["entry_key"] for name, cfg in ctx.skill.contract.get("sources", {}).items()
        if cfg.get("entry_key") and name in plan_sources
    }
    # Independent review 2026-09-24, item 1: a source declaring entry_key
    # asserts either "this key repeats by design" (entry_key_repeats: true,
    # e.g. attendee_validity) or "this key is unique per row" (the default).
    # The latter is CHECKED below against the data actually read, never
    # assumed -- this is the check expense_report's old declaration never
    # had, which is exactly how it silently collapsed distinct rows.
    entry_key_repeats_by_source: dict[str, bool] = {
        name: bool(ctx.skill.contract.get("sources", {}).get(name, {}).get("entry_key_repeats"))
        for name in entry_key_cols_by_source
    }

    row_amount: dict[tuple[str, str], float] = {}
    row_position: dict[tuple[str, str], int] = {}
    entry_key_by_row: dict[tuple[str, str], tuple] = {}
    source_frames: dict[str, pd.DataFrame] = {}
    for source in sorted(set(amount_col_by_source) | set(entry_key_cols_by_source)):
        version = bindings.get(source)
        # Item 2 (CLAUDE.md P2/P3 gate review): a source a population
        # declares an amount_column for, or that declares an entry_key, must
        # have a real binding -- discover() already required every contract
        # source to have one, so a missing one here means that invariant was
        # violated somewhere upstream. Silently skipping this source would
        # silently drop its rows from the headline's entry-grain
        # de-duplication -- fail loudly instead (CLAUDE.md NN14).
        if version is None:
            raise ContractViolation(
                [f"source {source!r} declares amount_column and/or entry_key but "
                 f"state.data_assets has no binding for it -- discover() should "
                 f"have required one"]
            )
        df = ctx.data_source.read_population(source, version=version)
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
                # G9/duplicate_detection parity: the row's position in this
                # same deterministic read order is how a duplicate_detection
                # primitive itself decides which group member is "the
                # first" (groupby preserves original row order within a
                # group) -- recorded here so this node can reproduce that
                # same choice without re-running the primitive.
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
                    # A declared entry_key does not always include the
                    # amount column (SKILL-001's own attendee_validity
                    # entry_key does, making this structurally unreachable
                    # there, but a future Skill's need not) -- two rows
                    # resolving to the same entry are a genuine data
                    # integrity problem if they disagree on amount, whether
                    # or not repeats are expected: the same transaction
                    # entry cannot have two different amounts.
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

    plan_tests = ctx.skill.plan.get("tests", [])
    tests_by_id = {t["test_id"]: t for t in plan_tests if "not_testable" not in t}
    flags_by_test_id: dict[str, set[str]] = {}
    for e in plan_test_flags(plan_tests):
        flags_by_test_id.setdefault(e["test_id"], set()).add(e["flag"])
    # A flag is not always unique to one test -- T6.1d_dom/T6.1d_int
    # deliberately share one flag name (RF_CS_DailySpendOverLimit) because
    # the existing UI's BREACH_FLAG_GROUPS reads one flag per control, so a
    # row's flag alone cannot say which of several sibling tests produced
    # it. _excess_for_row resolves this by trying every candidate test that
    # both produced the citing finding AND declares this flag, and using
    # whichever one's own re-derived grouping actually contains the row
    # (group_by columns/values differ enough between siblings -- e.g.
    # domestic vs international `country` -- that a row belongs to at most
    # one candidate's exceeding groups).
    flag_to_test_ids: dict[str, list[str]] = {}
    for test_id, flags in flags_by_test_id.items():
        for flag in flags:
            flag_to_test_ids.setdefault(flag, []).append(test_id)
    amount_metrics_by_test_id = plan_test_amount_metrics(plan_tests)
    all_additive_amount_names: set[str] = set()
    for names in amount_metrics_by_test_id.values():
        all_additive_amount_names |= names

    rows_by_flag: dict[str, list[dict]] = {}
    for r in ctx.persistence.list_flagged_rows(state.run_id):
        rows_by_flag.setdefault(r["flag"], []).append(r)

    def _line_id(r: dict) -> tuple:
        source, row_key = r["source"], r["row_key"]
        entry_key_cols = entry_key_cols_by_source.get(source)
        if entry_key_cols is None:
            return (source, row_key)
        key_values = entry_key_by_row.get((source, row_key))
        if key_values is None:
            raise ContractViolation(
                [f"{source} row {row_key!r}: missing entry_key for headline de-duplication -- "
                 f"this source's population should have been read above"]
            )
        return (source, key_values)

    _dup_first_row_cache: dict[str, dict[str, str]] = {}

    def _dup_group_first_row(primary_flag: str) -> dict[str, str]:
        """group_id -> the row_key duplicate_detection itself would treat as
        "the first" of that group (lowest row_position, i.e. earliest in the
        deterministic read order) -- scored against the FULL flagged
        population for `primary_flag` (never a single finding's own subset),
        so the choice is the same regardless of which finding is asking."""
        if primary_flag not in _dup_first_row_cache:
            best: dict[str, tuple[int, str]] = {}
            for r in rows_by_flag.get(primary_flag, []):
                gid = r.get("group_id")
                pos = row_position.get((r["source"], r["row_key"]))
                if gid is None or pos is None:
                    continue
                current = best.get(gid)
                if current is None or pos < current[0]:
                    best[gid] = (pos, r["row_key"])
            _dup_first_row_cache[primary_flag] = {gid: rk for gid, (_, rk) in best.items()}
        return _dup_first_row_cache[primary_flag]

    _per_diem_excess_cache: dict[str, dict[str, float]] = {}
    _contract_validated_sources: set[str] = set()

    def _per_diem_row_excess(test: dict) -> dict[str, float]:
        """row_key -> this row's pro-rata share of its employee-day's
        over-limit excess, for a group_by threshold_exceedance test (T6.1d's
        shape). Rebuilds the ONE population this test reads via
        orchestrator.populations.build_populations -- the exact function
        execute() used, from the same raw source already read above -- so a
        derived limit column (e.g. a per-diem rate converted to AUD) is
        never re-implemented here. Allocation is proportional to each
        line's own amount within its day: a day $100 over limit, with a
        $300 and a $100 line, allocates $75 and $25."""
        test_id = test["test_id"]
        if test_id not in _per_diem_excess_cache:
            params = test["params"]
            pop_name = params["population"]
            pop_cfg = populations_cfg[pop_name]
            # Every DECLARED contract source, not just this population's own
            # `source:` -- a `derive` step's `lookup`/`fx_rate` table may
            # itself be another contract source read as-is (T6.1d's
            # per_diem_rates, orchestrator.populations._resolve_table), and
            # this node has no generic way to know which ones a given
            # population's derive list will ask for short of reading it the
            # same way execute_skill() does: every contract source, once,
            # up front. Cached in source_frames so a second excess test
            # (T6.1d_int alongside T6.1d_dom) never re-reads.
            raw_sources: dict[str, dict] = {}
            for src in ctx.skill.contract.get("sources", {}):
                raw_df = source_frames.get(src)
                if raw_df is None:
                    version = bindings.get(src)
                    if version is None:
                        raise ContractViolation(
                            [f"source {src!r} declares a contract but state.data_assets has no "
                             f"binding for it -- discover() should have required one"]
                        )
                    raw_df = ctx.data_source.read_population(src, version=version)
                    source_frames[src] = raw_df
                if src not in _contract_validated_sources:
                    # build_populations' own filters/derives assume
                    # contract-typed columns (e.g. a real datetime64
                    # Transaction Date, not raw object-dtype values) --
                    # execute() gets this for free because execute_skill()
                    # validates every raw source before building any
                    # population; this node reads sources itself (see this
                    # function's own module docstring) and must do the same
                    # before reusing populations.build_populations, never
                    # assume a plain read_population() already produced
                    # typed columns. Mutates raw_df (and therefore
                    # source_frames[src]) in place, once per source,
                    # idempotently.
                    validate_contract(raw_df, ctx.skill.contract.get("sources", {}).get(src, {}))
                    _contract_validated_sources.add(src)
                raw_sources[src] = {"df": raw_df, "version": bindings.get(src)}
            pop_ctx = PopulationContext(
                sources=raw_sources,
                references=ctx.skill.references,
                audit_period=state.audit_period,
                thresholds=ctx.skill.thresholds,
                custom_derivations=ctx.skill.custom_derivations,
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
                    threshold_value = ctx.skill.thresholds[limit_spec["threshold"]]["value"]
                    limits = pd.Series(threshold_value, index=totals.index)
                for gkey, group_df in df.groupby(group_by, dropna=False):
                    total = float(totals.loc[gkey])
                    limit = float(limits.loc[gkey])
                    excess = max(0.0, total - limit)
                    if excess <= 0.0 or total == 0.0:
                        continue
                    for row_key, amt in zip(group_df["__row_key"], group_df[column]):
                        row_excess[row_key] = excess * (float(amt) / total)
            _per_diem_excess_cache[test_id] = row_excess
        return _per_diem_excess_cache[test_id]

    def _excess_for_row(r: dict, candidate_test_ids) -> float:
        matching = [tid for tid in candidate_test_ids if r["flag"] in flags_by_test_id.get(tid, set())]
        if not matching:
            raise ContractViolation(
                [f"flag {r['flag']!r} on an 'excess' finding's row has no producing plan.yaml test "
                 f"among this finding's own producing tests {sorted(candidate_test_ids)} -- cannot "
                 f"compute its at-risk amount"]
            )
        attempted: list[str] = []
        for test_id in matching:
            test = tests_by_id.get(test_id)
            if test is None:
                continue
            primitive = test.get("primitive")
            if primitive == "duplicate_detection":
                primary_flag = test["params"].get("flag", test.get("flag"))
                first_by_group = _dup_group_first_row(primary_flag)
                gid = r.get("group_id")
                if gid is None:
                    raise ContractViolation([f"{r['source']} row {r['row_key']!r}: excess row has no group_id"])
                if gid not in first_by_group:
                    attempted.append(test_id)
                    continue
                if first_by_group.get(gid) == r["row_key"]:
                    return 0.0
                amt = row_amount.get((r["source"], r["row_key"]))
                if amt is None:
                    raise ContractViolation(
                        [f"{r['source']} row {r['row_key']!r}: no amount available for excess allocation"]
                    )
                return amt
            if primitive == "threshold_exceedance" and test["params"].get("group_by"):
                excess_map = _per_diem_row_excess(test)
                amt = excess_map.get(r["row_key"])
                if amt is None:
                    attempted.append(test_id)
                    continue
                return amt
            raise ContractViolation(
                [f"test {test_id!r} (primitive {primitive!r}) has monetary_basis 'excess' but this "
                 f"node does not know how to allocate its per-line at-risk amount -- add support in "
                 f"orchestrator.nodes.fieldwork.prioritise._excess_for_row rather than guessing"]
            )
        raise ContractViolation(
            [f"{r['source']} row {r['row_key']!r}: no at-risk amount computed by any of this "
             f"finding's producing tests that declare flag {r['flag']!r} ({attempted}) -- row not "
             f"in an exceeding group on re-derivation"]
        )

    line_value: dict[tuple, float] = {}
    approved_not_spent_total = None
    updated_findings: list[dict] = []
    for f in persisted:
        cited_metrics = f.get("metrics_cited") or {}

        # B1: which plan.yaml test(s) actually produced each of this
        # finding's cited metrics -- read from the RECORDED metric->test
        # mapping this run's own execute() persisted (metrics_rows' test_id
        # column, CLAUDE.md build brief P3 §1), never re-derived by matching
        # the finding's single `test_id` against plan.yaml ids by string
        # prefix. A finding cites one test_id but its metrics can legitimately
        # come from several sibling sub-tests that do not share a prefix with
        # each other or with the finding's own test_id (see docstring above).
        producing_test_ids = {
            existing_metrics[name]["test_id"]
            for name in cited_metrics
            if name in existing_metrics and existing_metrics[name].get("test_id")
        }
        # This finding's flags = flags of every test that produced any of its
        # cited metrics -- no prefix matching.
        flags = {flag for tid in producing_test_ids for flag in flags_by_test_id.get(tid, set())}
        rows = [r for flag in flags for r in rows_by_flag.get(flag, [])]

        # This finding's amount metrics = its metrics_cited that are additive
        # AUD metrics (unit-based), produced by ANY plan test -- restricted to
        # what this finding's OWN metrics_cited actually names (CLAUDE.md
        # P2/P3 gate review item 1: "exposure_amount must be consistent with
        # the at-risk metric it CITES") so a plan-declared amount metric the
        # finding's findings.yaml rule does not cite (e.g. a worst-single-day
        # `max` figure a different rule cites for its own purposes) never
        # silently enters this finding's exposure.
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

        # B2: only 'spend'/'excess' findings contribute to the run headline
        # (approved_not_spent is money that was never actually reimbursed --
        # reported separately below, never summed into "flagged spend"; a
        # finding's monetary_basis is required by the schema and read here
        # with no default -- a missing one is a caller bug, not silently
        # treated as non-monetary). Each line's value is the LARGEST amount
        # at risk any finding attributes to it (independent review
        # 2026-09-24, item 1) -- never summed across findings, which would
        # double-count the same real dollar when two findings flag the same
        # line.
        monetary_basis = f.get("monetary_basis")
        if monetary_basis in ("spend", "excess"):
            for r in rows:
                lid = _line_id(r)
                if monetary_basis == "spend":
                    amt = row_amount.get((r["source"], r["row_key"]))
                    if amt is None:
                        raise ContractViolation(
                            [f"{r['source']} row {r['row_key']!r}: no amount available for a "
                             f"'spend' finding's headline contribution"]
                        )
                else:
                    amt = _excess_for_row(r, producing_test_ids)
                current = line_value.get(lid)
                if current is None or amt > current:
                    line_value[lid] = amt
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

    ctx.persistence.write_findings(
        state.run_id,
        updated_findings,
        engagement_id=state.engagement_id,
        skill_id=state.skill_id,
        skill_version=state.skill_version,
        now=now,
    )

    headline_exposure = round(sum(line_value.values()), 2)
    metrics_map = {
        name: {
            "metric_name": name,
            "value": m["value"],
            "unit": m.get("unit"),
            "source_ref": m.get("source_ref", {}),
            "test_id": m.get("test_id"),
        }
        for name, m in existing_metrics.items()
    }
    # Independent review 2026-09-24, item 1: source provenance for every
    # source that actually contributed a distinct line to the headline --
    # table version, the amount column read, and (where declared) the
    # repeats-grain entry_key. A LIST of {name, version, ...}, matching the
    # same shape every other run_metrics row's source_ref.sources already
    # uses (orchestrator.engine._run_metric_source_ref) -- app/src callers
    # read sources[0]["name"] generically across every metric, so this can
    # never be a differently-shaped dict keyed by source name.
    contributing_sources = sorted({src for src, _ in line_value})
    headline_provenance = [
        {
            "name": src,
            "version": bindings.get(src),
            "amount_column": amount_col_by_source.get(src),
            "entry_key": entry_key_cols_by_source.get(src),
        }
        for src in contributing_sources
    ]
    metrics_map["run_exposure_headline"] = {
        "metric_name": "run_exposure_headline",
        "value": headline_exposure,
        "unit": "AUD",
        "source_ref": {
            "label": "Potential exposure",
            "basis": (
                "The amount at risk: every distinct flagged transaction line from findings whose "
                "monetary_basis is 'spend' or 'excess' counts exactly once, at the LARGEST amount "
                "at risk any finding attributes to it -- a 'spend' finding attributes the line's "
                "own full amount, an 'excess' finding attributes only the at-risk portion (the "
                "lines beyond the first in a duplicate group, or a per-diem day's over-limit "
                "amount allocated pro rata to that day's lines). Line identity is row identity for "
                "a row-grain source, or a declared repeats-grain entry_key (contract.yaml) where "
                "one source row is genuinely the same business entry as another (e.g. one row per "
                "attendee of one entertainment claim). Excludes 'approved_not_spent' findings "
                "(money approved but never actually spent -- see run_approved_not_spent_total) and "
                "'none' (no dollar figure). Per-finding exposure_amount figures overlap with each "
                "other and with this headline by design (the same line can be cited by more than "
                "one finding) and must never be summed."
            ),
            "sources": headline_provenance,
        },
        "test_id": None,
    }
    metrics_map["run_approved_not_spent_total"] = {
        "metric_name": "run_approved_not_spent_total",
        "value": approved_not_spent_total,
        "unit": "AUD",
        "source_ref": {
            "label": "Approved but never spent (not flagged spend)",
            "basis": (
                "Sum of every 'approved_not_spent' finding's own exposure_amount -- money "
                "authorised (e.g. an approved travel request) but never turned into an actual "
                "reimbursed transaction. Reported separately because it is not spend at all; "
                "never included in the gross-flagged-spend headline above."
            ),
        },
        "test_id": None,
    }
    ctx.persistence.write_run_metrics(state.run_id, list(metrics_map.values()))

    compact = [
        {
            "finding_id": f["finding_id"],
            "rule_id": f["rule_id"],
            "test_id": f.get("test_id"),
            "severity": f["severity"],
            "title": f["title"],
            "analyst_set_severity": f.get("analyst_set_severity"),
            "exposure_amount": f["exposure_amount"],
        }
        for f in updated_findings
    ]
    message = f"{len(updated_findings)} finding(s) prioritised, headline exposure {headline_exposure}"
    return dataclasses.replace(state, findings=compact, events=state.events + [_event("prioritise", message, now)])


def act(ctx: NodeContext, state: RunState) -> RunState:
    """One draft management action per finding (CLAUDE.md build brief P3 §2).
    priority_rationale stays empty -- narration is a P6 deliverable.

    "Generate management actions after review" (CLAUDE.md §5 UI item 4,
    NN13): unchecked at run setup means state.options["generate_management_
    actions"] is False, and this node must actually honour that -- write no
    actions and record why, rather than silently drafting them anyway (the
    option existed on RunState since service.start_audit_run but nothing
    upstream of this change ever read it)."""
    now = ctx.clock()

    if not state.options.get("generate_management_actions", True):
        ctx.persistence.write_management_actions(state.run_id, [], now=now)
        message = "Management actions not generated (option off)"
        return dataclasses.replace(
            state, management_actions=[], events=state.events + [_event("act", message, now)]
        )

    findings = ctx.persistence.list_findings(state.run_id)

    actions = [
        {
            "action_id": f"MA-{f['finding_id']}",
            "issue_id": f"ISS-{f['finding_id']}",
            "finding_id": f["finding_id"],
            "engagement_id": state.engagement_id,
            "skill_id": state.skill_id,
            "title": f["title"],
            "description": f.get("recommendation"),
            "owner": None,
            "risk": f["severity"],
            "status": "draft",
            "target_date": None,
            "potential_exposure": f.get("exposure_amount"),
            "evidence_link": f.get("test_id"),
        }
        for f in findings
    ]
    ctx.persistence.write_management_actions(state.run_id, actions, now=now)

    compact = [
        {"action_id": a["action_id"], "finding_id": a["finding_id"], "title": a["title"], "status": a["status"]}
        for a in actions
    ]
    message = f"{len(actions)} management action(s) drafted"
    return dataclasses.replace(state, management_actions=compact, events=state.events + [_event("act", message, now)])


# ── export phase ─────────────────────────────────────────────────────────────


# OWASP CSV/formula-injection guard (P2/P3 gate review item 7): a cell whose
# first character is one of these is a live formula/macro trigger in Excel
# (and in most spreadsheet apps re-parsing a CSV export of this workbook),
# so any string that starts with one gets a leading single quote -- Excel
# renders a leading `'` as a literal-text marker and drops it from display,
# so this is invisible to a reader but stops the string being evaluated.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _safe_str(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA_TRIGGER_CHARS):
        return "'" + text
    return text


def _write_str(ws, row: int, col: int, value, fmt=None) -> None:
    # write_string (never the type-sniffing `write()`, which will itself
    # detect a leading "=" and emit a live formula cell) -- every string in
    # this workbook is either data-derived (objective, finding prose) or
    # comes from a Skill/config value that is not this codebase's to trust.
    ws.write_string(row, col, _safe_str(value), fmt)


def _build_ticket_previews(findings: list[dict]) -> list[dict]:
    """One issue-tracker ticket preview per finding (CLAUDE.md §8: submission
    to any tracker -- Jira, ServiceNow, whichever the audit team is on -- is
    not built), via the tracker-neutral IssueTrackerAdapter Protocol so a
    real tracker is a second implementation of that Protocol later, never a
    change here. Returns plain dicts (never the TicketPreview dataclass
    itself) because RunState.exports must stay JSON-serialisable (CLAUDE.md
    §3 NN6)."""
    tracker = PreviewOnlyIssueTracker()
    return [dataclasses.asdict(t) for t in tracker.preview(findings)]


def _require_severity_provenance(f: dict) -> tuple[bool, str]:
    analyst_set = f.get("analyst_set_severity")
    basis = f.get("severity_basis")
    if analyst_set is None:
        raise MissingSeverityProvenance(f["finding_id"], "analyst_set_severity")
    if basis is None:
        raise MissingSeverityProvenance(f["finding_id"], "severity_basis")
    return analyst_set, basis


def _write_xlsx_workpaper(
    state: RunState, findings: list[dict], metrics: dict[str, dict], flagged_rows: list[dict], now: str,
    ticket_previews: list[dict] | None = None,
) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    bold = wb.add_format({"bold": True})
    money = wb.add_format({"num_format": "#,##0.00"})
    footer = f"run_id={state.run_id} | generated_at={now}"

    cover = wb.add_worksheet("Cover")
    _write_str(cover, 0, 0, "AI Audit Analyst — Workpaper Export", bold)
    # label/value split into two columns (rather than one interpolated string)
    # so every data-derived value -- objective and run_owner above all, since
    # both are free text an auditor typed at run setup -- gets its own cell
    # through _write_str's formula-injection guard, not buried mid-string
    # where a leading "=" in the VALUE would not be the cell's first
    # character (CLAUDE.md P2/P3 gate review item 7).
    cover_fields = [
        ("run_id", state.run_id),
        ("generated_at", now),
        ("skill", f"{state.skill_id} v{state.skill_version}"),
        ("audit_period", f"{state.audit_period[0]} to {state.audit_period[1]}"),
        ("objective", state.objective),
        ("run_owner", state.run_owner),
    ]
    signoff = state.signoff or {}
    if signoff:
        cover_fields.append(("signed_off_by", signoff.get("approver")))
        cover_fields.append(("signed_off_at", signoff.get("timestamp")))
        if signoff.get("self_approved"):
            cover_fields.append(("signoff_note", SELF_APPROVED_LABEL))
    for r, (label, value) in enumerate(cover_fields, start=1):
        _write_str(cover, r, 0, label, bold)
        _write_str(cover, r, 1, value)
    _write_str(cover, len(cover_fields) + 2, 0, footer)

    ws = wb.add_worksheet("Findings")
    headers = [
        "finding_id", "rule_id", "test_id", "severity", "analyst_set_severity", "severity_basis",
        "title", "observation", "recommendation", "exposure_amount", "exposure_basis", "review_state",
    ]
    ws.write_row(0, 0, headers, bold)
    for r, f in enumerate(findings, start=1):
        # Never a defaulted False (CLAUDE.md §0.4/G8): raises if the run's own
        # persisted findings never carried these (a write_findings bug), rather
        # than exporting an unattributed severity as if it were policy-backed.
        analyst_set, basis = _require_severity_provenance(f)
        _write_str(ws, r, 0, f["finding_id"])
        _write_str(ws, r, 1, f["rule_id"])
        _write_str(ws, r, 2, f.get("test_id"))
        _write_str(ws, r, 3, f["severity"])
        ws.write_boolean(r, 4, analyst_set)
        _write_str(ws, r, 5, basis)
        _write_str(ws, r, 6, f["title"])
        _write_str(ws, r, 7, f.get("observation"))
        _write_str(ws, r, 8, f.get("recommendation"))
        # B4 (CLAUDE.md NN14): a non-monetary finding's exposure_amount is
        # None, not a fabricated $0.00 -- an auditor reading this column must
        # not be able to mistake "not assessed in dollars" for "assessed at
        # zero risk". Blank cell, same money format, never a written 0.
        exposure_amount = f.get("exposure_amount")
        if exposure_amount is None:
            ws.write_blank(r, 9, None, money)
        else:
            ws.write_number(r, 9, exposure_amount, money)
        _write_str(ws, r, 10, f.get("exposure_basis"))
        _write_str(ws, r, 11, f.get("review_state"))
    _write_str(ws, len(findings) + 2, 0, footer)

    ws2 = wb.add_worksheet("Metrics")
    ws2.write_row(0, 0, ["metric_name", "value", "unit", "test_id", "source_ref"], bold)
    for r, (name, m) in enumerate(sorted(metrics.items()), start=1):
        _write_str(ws2, r, 0, name)
        value = m.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            ws2.write_number(r, 1, value)
        else:
            _write_str(ws2, r, 1, value)
        _write_str(ws2, r, 2, m.get("unit"))
        _write_str(ws2, r, 3, m.get("test_id"))
        _write_str(ws2, r, 4, json.dumps(m.get("source_ref", {}), sort_keys=True))
    _write_str(ws2, len(metrics) + 2, 0, footer)

    ws3 = wb.add_worksheet("Test Results")
    ws3.write_row(0, 0, ["test_id", "status", "exception_units", "reason"], bold)
    for r, t in enumerate(state.test_results, start=1):
        _write_str(ws3, r, 0, t["test_id"])
        _write_str(ws3, r, 1, t["status"])
        ws3.write_number(r, 2, t["exception_units"])
        _write_str(ws3, r, 3, t.get("reason") or "")
    _write_str(ws3, len(state.test_results) + 2, 0, footer)

    ws4 = wb.add_worksheet("Reconciliation")
    ws4.write_row(
        0, 0,
        [
            "source", "engine_rows", "independent_rows", "row_variance",
            "amount", "independent_amount", "amount_variance",
            "min_date", "independent_min_date", "min_date_match",
            "max_date", "independent_max_date", "max_date_match",
        ],
        bold,
    )
    reconciliation = state.reconciliation or {}
    for r, (source, rec) in enumerate(sorted(reconciliation.items()), start=1):
        _write_str(ws4, r, 0, source)
        ws4.write_number(r, 1, rec.get("engine_rows") or 0)
        ws4.write_number(r, 2, rec.get("independent_rows") or 0)
        ws4.write_number(r, 3, rec.get("variance") or 0)
        # B4 (CLAUDE.md NN14): a source whose raw_<source> population declares
        # no amount_column has no amount to reconcile at all -- `rec["amount"]`
        # is None (orchestrator.populations.build_population), never a real
        # 0.0. Writing 0.0 here would read as "this source's amounts
        # reconcile to zero", which is a fabricated claim, not an absence.
        # Explicit text, never a number, when the column was never declared.
        if rec.get("amount") is None:
            _write_str(ws4, r, 4, "n/a — no amount column declared")
            _write_str(ws4, r, 5, "n/a — no amount column declared")
            _write_str(ws4, r, 6, "n/a — no amount column declared")
        else:
            ws4.write_number(r, 4, rec["amount"], money)
            ws4.write_number(r, 5, rec.get("independent_amount") or 0.0, money)
            ws4.write_number(r, 6, rec.get("amount_variance") or 0.0, money)
        _write_str(ws4, r, 7, rec.get("min_date"))
        _write_str(ws4, r, 8, rec.get("independent_min_date"))
        _write_str(ws4, r, 9, str(rec.get("min_date_match")))
        _write_str(ws4, r, 10, rec.get("max_date"))
        _write_str(ws4, r, 11, rec.get("independent_max_date"))
        _write_str(ws4, r, 12, str(rec.get("max_date_match")))
    _write_str(ws4, len(reconciliation) + 2, 0, footer)

    counts: dict[str, int] = {}
    for row in flagged_rows:
        counts[row["flag"]] = counts.get(row["flag"], 0) + 1
    ws5 = wb.add_worksheet("Flagged Row Counts")
    ws5.write_row(0, 0, ["flag", "row_count"], bold)
    for r, (flag, n) in enumerate(sorted(counts.items()), start=1):
        _write_str(ws5, r, 0, flag)
        ws5.write_number(r, 1, n)
    _write_str(ws5, len(counts) + 2, 0, footer)

    # "Prepare Jira ticket previews" (CLAUDE.md §5 UI item 4 -- the checkbox
    # label stays as the UI has always shown it; the tracker behind it is
    # generic, see IssueTrackerAdapter): only present when the run asked for
    # it -- an unchecked run's workbook has no such sheet at all, never an
    # empty one.
    if ticket_previews:
        ws6 = wb.add_worksheet("Ticket Preview")
        ws6.write_row(0, 0, ["issue_id", "title", "severity", "description", "status"], bold)
        for r, t in enumerate(ticket_previews, start=1):
            _write_str(ws6, r, 0, t["issue_id"])
            _write_str(ws6, r, 1, t["title"])
            _write_str(ws6, r, 2, t["severity"])
            _write_str(ws6, r, 3, t.get("description"))
            _write_str(ws6, r, 4, t["status"])
        _write_str(ws6, len(ticket_previews) + 2, 0, footer)

    wb.close()
    return buf.getvalue()


def export(ctx: NodeContext, state: RunState) -> RunState:
    """Builds the XLSX workpaper from persisted run outputs only -- never
    module globals, never a recomputation of what `execute`/`find`/`prioritise`
    already fixed (CLAUDE.md §4.7's "every number comes from RunState/persisted
    outputs" rule, here applied ahead of the P4/P6 PPTX rebuild). Every sheet's
    footer stamps run_id + generation timestamp (§9A.2)."""
    findings = ctx.persistence.list_findings(state.run_id)
    metrics = ctx.persistence.get_run_metrics(state.run_id)
    flagged_rows = ctx.persistence.list_flagged_rows(state.run_id)
    now = ctx.clock()

    # "Prepare Jira ticket previews" (CLAUDE.md §5 UI item 4, NN13) -- the
    # checkbox label is unchanged, but the option key stays
    # jira_preview_requested for backward compatibility with runs already
    # started under it; the tracker behind it is generic (IssueTrackerAdapter).
    # Unchecked means none at all -- not an empty preview -- checked means
    # one labelled preview per finding, stored alongside this run's other
    # exports and included in the XLSX below. Never a submission: CLAUDE.md §8.
    ticket_previews: list[dict] = []
    if state.options.get("jira_preview_requested", False):
        ticket_previews = _build_ticket_previews(findings)

    content = _write_xlsx_workpaper(state, findings, metrics, flagged_rows, now, ticket_previews)
    sha256 = hashlib.sha256(content).hexdigest()
    rel_path = f"exports/{state.run_id}/workpaper.xlsx"
    written_path = ctx.export_storage.write(rel_path, content)
    ctx.persistence.record_export(
        state.run_id, "xlsx", path=written_path, sha256=sha256, created_by=state.run_owner, now=now,
    )

    exports = {**(state.exports or {}), "xlsx": {"path": written_path, "sha256": sha256, "kind": "xlsx"}}

    # PPTX audit pack (CLAUDE.md §4.7): same "persisted outputs only, never
    # recomputed" rule as the XLSX above -- generate_pptx reads `state`,
    # `findings`, `metrics` and this Skill's own static catalogue.yaml,
    # nothing else.
    catalogue_rows = load_catalogue_rows(ctx.skill.skill_dir)
    data_mode = "Local test data" if ctx.backend == "local" else "Unity Catalog"
    pptx_content = generate_pptx(
        state, findings, metrics, catalogue_rows, ctx.skill,
        data_mode=data_mode, template_path=ctx.settings.pptx_template_path, now=now,
    )
    pptx_sha256 = hashlib.sha256(pptx_content).hexdigest()
    pptx_rel_path = f"exports/{state.run_id}/audit_pack.pptx"
    pptx_written_path = ctx.export_storage.write(pptx_rel_path, pptx_content)
    ctx.persistence.record_export(
        state.run_id, "pptx", path=pptx_written_path, sha256=pptx_sha256, created_by=state.run_owner, now=now,
    )
    exports["pptx"] = {"path": pptx_written_path, "sha256": pptx_sha256, "kind": "pptx"}

    if ticket_previews:
        preview_content = json.dumps(
            {"run_id": state.run_id, "status": TICKET_PREVIEW_STATUS, "ticket_previews": ticket_previews},
            indent=2,
        ).encode("utf-8")
        preview_sha256 = hashlib.sha256(preview_content).hexdigest()
        preview_rel_path = f"exports/{state.run_id}/ticket_preview.json"
        preview_written_path = ctx.export_storage.write(preview_rel_path, preview_content)
        ctx.persistence.record_export(
            state.run_id, "ticket_preview", path=preview_written_path, sha256=preview_sha256,
            created_by=state.run_owner, now=now,
        )
        exports["ticket_preview"] = {
            "path": preview_written_path, "sha256": preview_sha256, "kind": "ticket_preview",
            "status": TICKET_PREVIEW_STATUS, "ticket_previews": ticket_previews,
        }

    message = (
        f"XLSX workpaper written to {written_path} ({len(content)} bytes); "
        f"PPTX audit pack written to {pptx_written_path} ({len(pptx_content)} bytes)"
    )
    if ticket_previews:
        message += f"; {len(ticket_previews)} ticket preview(s) prepared ({TICKET_PREVIEW_STATUS})"
    return dataclasses.replace(
        state,
        # Merge, never overwrite -- `execute` already wrote this run's frame
        # snapshots into state.exports["frames"] (CLAUDE.md build brief P4
        # perf fix); replacing the whole dict here would silently drop that
        # entry (both are still recorded independently in the `exports`
        # table, but state.exports is what get_run_frames reads first).
        exports=exports,
        events=state.events + [_event("export", message, now)],
    )


NODES_FOR: dict[str, dict[str, list[tuple[str, object]]]] = {
    "fieldwork": {
        "plan": [("discover", discover), ("profile", profile), ("plan", plan)],
        "execute": [
            ("execute", execute),
            ("classify", classify),
            ("find", find),
            ("prioritise", prioritise),
            ("act", act),
        ],
        "export": [("export", export)],
    }
}
