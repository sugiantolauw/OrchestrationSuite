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

import xlsxwriter

from orchestrator.contract import ContractViolation
from orchestrator.errors import MissingSeverityProvenance, ReconciliationError
from orchestrator.engine import execute_skill
from orchestrator.findings import build_findings
from orchestrator.frames import build_row_snapshots, frame_parquet_bytes, sha256_bytes
from orchestrator.nodes.context import NodeContext
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


def execute(ctx: NodeContext, state: RunState) -> RunState:
    """Runs every test in plan order via orchestrator.engine.execute_skill
    (CLAUDE.md §4.2) and persists its two durable outputs: run_metrics (every
    metric any test produced) and flagged_rows (the row-level RF_* evidence,
    long-format). NEVER calls an LLM.

    G6 reconciliation compares each contract source's row count as the engine
    saw it (via that source's `raw_<source>` population, declared unfiltered in
    plan.yaml) against an INDEPENDENTLY obtained row count from
    DataSourceAdapter.row_count() at the same pinned version -- not a number
    re-derived from the same in-memory frame. A non-zero variance fails the
    run outright (CLAUDE.md §5 G6); it is never silently reported and ignored.

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

    reconciliation: dict[str, dict] = {}
    differences: list[str] = []
    for source, version in result.source_versions.items():
        raw_pop = result.populations.get(f"raw_{source}")
        engine_rows = raw_pop["rows"] if raw_pop is not None else None
        independent_rows = ctx.data_source.row_count(source, version=version)
        variance = None if engine_rows is None else engine_rows - independent_rows
        reconciliation[source] = {
            "engine_rows": engine_rows,
            "independent_rows": independent_rows,
            "variance": variance,
            "amount": raw_pop["amount"] if raw_pop is not None else None,
            "min_date": raw_pop["min_date"] if raw_pop is not None else None,
            "max_date": raw_pop["max_date"] if raw_pop is not None else None,
        }
        if variance not in (None, 0):
            differences.append(f"{source}: engine_rows={engine_rows} independent_rows={independent_rows}")
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
            "analyst_set_severity": f.get("analyst_set_severity", False),
        }
        for f in persisted
    ]
    message = f"{len(persisted)} finding(s) generated"
    return dataclasses.replace(state, findings=compact, events=state.events + [_event("find", message, now)])


def _flags_for_test_id(test_flag: dict[str, str], test_id: str) -> set[str]:
    # A finding cites one plan.yaml test_id, but the T&E Skill splits several
    # catalogue tests into several plan.yaml sub-tests sharing that prefix
    # (e.g. finding T3_2a cites test_id "T3.2a", while plan.yaml has
    # "T3.2a_air_dom", "T3.2a_air_int", ... each with its own flag) -- so a
    # finding's flags are every plan.yaml test whose id equals or is prefixed
    # by "<test_id>_".
    return {
        flag for tid, flag in test_flag.items()
        if tid == test_id or tid.startswith(f"{test_id}_")
    }


def prioritise(ctx: NodeContext, state: RunState) -> RunState:
    """Deterministic ordering: severity, then de-duplicated exposure (CLAUDE.md
    §0.3 -- fixes app.py's double count, where the same row could be summed
    into more than one finding's `max(amount over cited metrics)`). Per-finding
    exposure is the sum of amount over the DISTINCT flagged rows of that
    finding's test(s); the run's headline exposure is the sum over the union
    of distinct (source, row_key) pairs across every finding, never the sum of
    per-finding totals.

    This is the one node besides `execute` that reads bound source data: no
    primitive or population exposes a row_key -> amount map (flagged_rows
    deliberately carries no amount column, CLAUDE.md build brief P3 §1), so
    de-duplicating exposure at row grain requires one lookup pass over each
    source that a population declares an `amount_column` for, at the same
    pinned versions execute() used."""
    persisted = ctx.persistence.list_findings(state.run_id)
    now = ctx.clock()

    if not persisted:
        message = "no findings to prioritise"
        return dataclasses.replace(state, findings=[], events=state.events + [_event("prioritise", message, now)])

    bindings = {b["source"]: b["version"] for b in state.data_assets}
    amount_col_by_source: dict[str, str] = {}
    for pop_cfg in ctx.skill.plan.get("populations", {}).values():
        src = pop_cfg.get("source")
        col = pop_cfg.get("amount_column")
        if col and src and src not in amount_col_by_source:
            amount_col_by_source[src] = col

    row_amount: dict[tuple[str, str], float] = {}
    for source, col in amount_col_by_source.items():
        version = bindings.get(source)
        if version is None:
            continue
        df = ctx.data_source.read_population(source, version=version)
        if col not in df.columns:
            continue
        for row_key, amount in zip(df["__row_key"], df[col]):
            try:
                value = float(amount)
            except (TypeError, ValueError):
                value = 0.0
            row_amount[(source, row_key)] = 0.0 if value != value else value  # NaN guard

    test_flag = {
        t["test_id"]: t["flag"] for t in ctx.skill.plan.get("tests", []) if t.get("flag")
    }
    rows_by_flag: dict[str, list[tuple[str, str]]] = {}
    for r in ctx.persistence.list_flagged_rows(state.run_id):
        rows_by_flag.setdefault(r["flag"], []).append((r["source"], r["row_key"]))

    all_flagged_keys: set[tuple[str, str]] = set()
    updated_findings: list[dict] = []
    for f in persisted:
        flags = _flags_for_test_id(test_flag, f.get("test_id") or "")
        keys: set[tuple[str, str]] = set()
        for flag in flags:
            keys.update(rows_by_flag.get(flag, []))
        exposure = round(sum(row_amount.get(k, 0.0) for k in keys), 2)
        all_flagged_keys |= keys
        updated_findings.append(
            {
                **f,
                "exposure_amount": exposure,
                "exposure_basis": (
                    f"Sum of amount over {len(keys)} distinct flagged row(s) for test "
                    f"{f.get('test_id')} (flags: {sorted(flags)}), de-duplicated so a row "
                    f"shared with another finding is never summed twice within THIS finding "
                    f"(CLAUDE.md §0.3)."
                ),
            }
        )

    updated_findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 3), -f["exposure_amount"]))

    ctx.persistence.write_findings(
        state.run_id,
        updated_findings,
        engagement_id=state.engagement_id,
        skill_id=state.skill_id,
        skill_version=state.skill_version,
        now=now,
    )

    headline_exposure = round(sum(row_amount.get(k, 0.0) for k in all_flagged_keys), 2)
    existing_metrics = ctx.persistence.get_run_metrics(state.run_id)
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
    metrics_map["run_exposure_headline"] = {
        "metric_name": "run_exposure_headline",
        "value": headline_exposure,
        "unit": "AUD",
        "source_ref": {
            "basis": "sum of amount over the union of distinct (source,row_key) flagged "
            "across every finding -- never the sum of per-finding totals (CLAUDE.md §0.3)",
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
            "analyst_set_severity": f.get("analyst_set_severity", False),
            "exposure_amount": f["exposure_amount"],
        }
        for f in updated_findings
    ]
    message = f"{len(updated_findings)} finding(s) prioritised, headline exposure {headline_exposure}"
    return dataclasses.replace(state, findings=compact, events=state.events + [_event("prioritise", message, now)])


def act(ctx: NodeContext, state: RunState) -> RunState:
    """One draft management action per finding (CLAUDE.md build brief P3 §2).
    priority_rationale stays empty -- narration is a P6 deliverable."""
    findings = ctx.persistence.list_findings(state.run_id)
    now = ctx.clock()

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


def _require_severity_provenance(f: dict) -> tuple[bool, str]:
    analyst_set = f.get("analyst_set_severity")
    basis = f.get("severity_basis")
    if analyst_set is None:
        raise MissingSeverityProvenance(f["finding_id"], "analyst_set_severity")
    if basis is None:
        raise MissingSeverityProvenance(f["finding_id"], "severity_basis")
    return analyst_set, basis


def _write_xlsx_workpaper(state: RunState, findings: list[dict], metrics: dict[str, dict], flagged_rows: list[dict], now: str) -> bytes:
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
        ws.write_number(r, 9, f.get("exposure_amount") or 0.0, money)
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
        ["source", "engine_rows", "independent_rows", "variance", "amount", "min_date", "max_date"],
        bold,
    )
    reconciliation = state.reconciliation or {}
    for r, (source, rec) in enumerate(sorted(reconciliation.items()), start=1):
        _write_str(ws4, r, 0, source)
        ws4.write_number(r, 1, rec.get("engine_rows") or 0)
        ws4.write_number(r, 2, rec.get("independent_rows") or 0)
        ws4.write_number(r, 3, rec.get("variance") or 0)
        ws4.write_number(r, 4, rec.get("amount") or 0.0, money)
        _write_str(ws4, r, 5, rec.get("min_date"))
        _write_str(ws4, r, 6, rec.get("max_date"))
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

    content = _write_xlsx_workpaper(state, findings, metrics, flagged_rows, now)
    sha256 = hashlib.sha256(content).hexdigest()
    rel_path = f"exports/{state.run_id}/workpaper.xlsx"
    written_path = ctx.export_storage.write(rel_path, content)
    ctx.persistence.record_export(
        state.run_id, "xlsx", path=written_path, sha256=sha256, created_by=state.run_owner, now=now,
    )

    message = f"XLSX workpaper written to {written_path} ({len(content)} bytes)"
    return dataclasses.replace(
        state,
        # Merge, never overwrite -- `execute` already wrote this run's frame
        # snapshots into state.exports["frames"] (CLAUDE.md build brief P4
        # perf fix); replacing the whole dict here would silently drop that
        # entry (both are still recorded independently in the `exports`
        # table, but state.exports is what get_run_frames reads first).
        exports={**(state.exports or {}), "xlsx": {"path": written_path, "sha256": sha256, "kind": "xlsx"}},
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
