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

`execute` is the only node that ALWAYS reads source data and it NEVER calls
an LLM (CLAUDE.md §3 non-negotiable 2); `find`/`prioritise`/`act` work only
from what `execute` already persisted (run_metrics, flagged_rows), so a
re-execution of any later node is a pure re-derivation, not a re-read of raw
data -- except `prioritise`, which re-reads bound sources once to look up each
row's amount for exposure de-duplication (no primitive or population exposes a
row_key -> amount map, so this is the one place outside `execute` that touches
raw data; see the function's own docstring), and `classify`, which -- ONLY
when `Settings.enable_row_level_llm` is true AND the Skill's plan.yaml
declares a `not_testable.llm_classification` block (the real SKILL-001 does
not) -- reads a text column for T4.3's row-level LLM classification
(independent review 2026-09-24 item 4). Off by default, this capability is
never exercised by a real run today; see classify()'s own docstring.
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
from orchestrator.explorer.canonical import to_canonical
from orchestrator.explorer.payload import build_planner_payload
from orchestrator.explorer.profile import load_repo_pii_flags, profile_source
from orchestrator.explorer.validate import EXPLORER_VALIDATOR_VERSION, validate_wire_proposal
from orchestrator.explorer.wire_schema import PLAN_PROPOSAL_SCHEMA, WIRE_SCHEMA_SHA256
from orchestrator import exposure
from orchestrator.findings import build_findings
from orchestrator.frames import build_row_snapshots, frame_parquet_bytes, sha256_bytes
from orchestrator.llm.gateway import CallContext
from orchestrator.llm.tasks import TASK_PROFILES
from orchestrator.narration import resolve as narration_resolve
from orchestrator.narration.payloads import build_caption_payload, build_finding_table, build_run_table, build_theme_table
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.narration import finalise, narrate
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
    proceeding on a guess (CLAUDE.md NN14/G7). Explorer (state.mode ==
    "explorer") has no contract yet before plan confirmation -- ctx.skill is
    None (docs/specs/P6_P8_explorer_llm_design.md §4.11's resolve_run_skill)
    -- so its branch validates every state.data_assets binding directly."""
    if state.mode == "explorer":
        return _discover_explorer(ctx, state)

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


def _discover_explorer(ctx: NodeContext, state: RunState) -> RunState:
    """Explorer's `sources` (§4.2 -- 1 to 5 entries, resolved and pinned at
    `start_explorer_run`) ARE `state.data_assets`; there is no contract to
    cross-check them against yet, so every binding is validated directly
    (same TOCTOU-safe re-resolve_version check as the Playbook branch)."""
    violations: list[str] = []
    for binding in state.data_assets:
        source = binding["source"]
        try:
            current_version = ctx.data_source.resolve_version(source)
        except Exception as exc:  # noqa: BLE001 - surfaced as a named contract violation
            violations.append(f"{source}: bound source unreachable: {exc!r}")
            continue
        if str(current_version) != str(binding["version"]):
            violations.append(
                f"{source}: bound at version {binding['version']!r} but now resolves to "
                f"{current_version!r} -- the source changed after it was pinned"
            )
    if violations:
        raise ContractViolation(violations)

    now = ctx.clock()
    names = sorted(b["source"] for b in state.data_assets)
    message = f"{len(names)} source(s) bound: {', '.join(names)}"
    return dataclasses.replace(state, events=state.events + [_event("discover", message, now)])


def profile(ctx: NodeContext, state: RunState) -> RunState:
    """Per contract source: row count and null count per contract column. Read
    via ctx.data_source.read_population + computed in pandas (CLAUDE.md build
    brief P3 §1) -- UCTableDataSource does not yet expose a SQL-pushdown
    `profile()` entry point, so this reads the whole bound population once per
    source rather than aggregating server-side. Fine at today's ~150K-row
    scale (§2.3 rule 4); a future SQL-pushdown profile() would replace this
    node's body only, not its output shape.

    Explorer (state.mode == "explorer") branches to _profile_explorer: an
    aggregates-only, PII-masked profile (docs/specs/P6_P8_explorer_llm_
    design.md §4.3), never row counts/null counts of contract columns
    (Explorer has no contract yet)."""
    if state.mode == "explorer":
        return _profile_explorer(ctx, state)

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


def _profile_explorer(ctx: NodeContext, state: RunState) -> RunState:
    """Aggregates only, never rows (docs/specs/P6_P8_explorer_llm_design.md
    §4.3): per bound source, `orchestrator.explorer.profile.profile_source`
    computes raw per-column statistics and applies §4.3.1 PII masking BEFORE
    anything is stored here -- `state.profile_result` for Explorer never
    carries a PII column's values (G15). Over `explorer_max_columns` columns
    (summed across selected sources) is a named ContractViolation, not a
    silent truncation."""
    # getattr with the same defaults orchestrator.config.Settings itself
    # carries -- never a DIFFERENT default, just tolerance for a test
    # harness's minimal settings stub that predates these fields.
    settings = ctx.settings
    max_distinct = getattr(settings, "explorer_category_max_distinct", 30)
    min_count = getattr(settings, "explorer_category_min_count", 5)
    max_columns = getattr(settings, "explorer_max_columns", 200)
    pii_tag_names = getattr(settings, "pii_tag_names", ())
    audit_timezone = getattr(settings, "audit_timezone", None)

    audit_period = None
    if audit_timezone and state.audit_period and all(state.audit_period):
        audit_period = tuple(state.audit_period)

    repo_pii_flags = load_repo_pii_flags()

    sources: dict[str, dict] = {}
    total_columns = 0
    for binding in state.data_assets:
        source = binding["source"]
        result = profile_source(
            ctx.data_source, source, version=binding["version"], max_distinct=max_distinct,
            min_count=min_count, audit_period=audit_period, audit_timezone=audit_timezone,
            repo_pii_flags=repo_pii_flags, pii_tag_names=pii_tag_names,
        )
        total_columns += len(result["columns"])
        sources[source] = result

    if total_columns > max_columns:
        raise ContractViolation(
            [f"too many columns for Explorer: select fewer sources ({total_columns} > {max_columns})"]
        )

    profile_result = {"kind": "explorer", "sources": sources}
    n_pii = sum(1 for s in sources.values() for c in s["columns"] if c.get("pii"))
    now = ctx.clock()
    message = f"Profiled {len(sources)} source(s), {total_columns} column(s) ({n_pii} PII column(s) masked)"
    return dataclasses.replace(
        state, profile_result=profile_result, events=state.events + [_event("profile", message, now)]
    )


def plan(ctx: NodeContext, state: RunState) -> RunState:
    """Playbook: the resolved plan IS the Skill's plan.yaml tests, unchanged
    (CLAUDE.md §4.4 -- building the test plan is generic pipeline code reading
    plan.yaml, never a per-Skill method). Explorer (state.mode == "explorer")
    branches to _plan_explorer: one planner call, at most one repair round,
    never a third (docs/specs/P6_P8_explorer_llm_design.md §4.5/§4.8) -- the
    node PROPOSES; nothing runs until an auditor confirms (service.
    confirm_plan), matching CLAUDE.md §3 non-negotiable 2's "the LLM authors
    rules; it does not decide results at runtime"."""
    if state.mode == "explorer":
        return _plan_explorer(ctx, state)
    if state.mode != "playbook":
        raise ValueError(f"plan node: mode={state.mode!r} is not a supported mode")

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


def _canon_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _explorer_data_gaps(sources: dict) -> list[str]:
    """The three Python-derived data-gap sentences (docs/specs/
    P6_P8_explorer_llm_design.md §4.3) plus a null-column one, computed
    directly from the already-PII-masked Explorer profile -- never a number
    other than counts that are themselves profile fields."""
    gaps: list[str] = []
    for source in sorted(sources):
        info = sources[source]
        row_count = info.get("row_count") or 0
        columns = info.get("columns", [])
        for col in columns:
            if row_count and col.get("null_count") == row_count:
                gaps.append(f"{source}.{col['name']} is entirely null")
        if not any(c.get("type") in ("date", "datetime") for c in columns):
            gaps.append(f"{source} has no date column")
        if not any(c.get("semantic_type") == "amount" for c in columns):
            gaps.append(f"{source} has no numeric amount column")
        has_currency_evidence = any(
            c.get("semantic_type") == "currency_code" and len(c.get("values") or []) == 1
            for c in columns
        )
        if not has_currency_evidence:
            gaps.append(f"{source} has no currency evidence")
    return gaps


def _explorer_pii_masked(sources: dict) -> list[str]:
    return sorted(
        f"{source}.{c['name']}"
        for source, info in sources.items()
        for c in info.get("columns", [])
        if c.get("pii")
    )


def _explorer_llm_call_entry(result) -> dict:
    return {
        "call_id": result.call_id, "source": result.source, "outcome": result.status,
        "served_model_version": result.served_model_version,
    }


def _explorer_plan_inputs(*, profile_payload: dict, prompts, reference_skills: list) -> dict:
    return {
        "profile_sha256": sha256_bytes(_canon_json(profile_payload).encode("utf-8")),
        # BUG-EXPLORER-2 (independent review round 2): the run's own pinned
        # reference Skills (state.options.explorer.reference_skill_ids,
        # ctx.explorer_reference_skills), keyed by id -> content_hash, the
        # same shape the fingerprint's explorer_inputs_hash uses.
        "reference_skills": {skill.skill_id: skill.content_hash for skill in reference_skills},
        "wire_schema_sha256": WIRE_SCHEMA_SHA256,
        "prompt_template_version": prompts.template_set_version(["explorer/planner", "explorer/repair"]),
        "validator_version": EXPLORER_VALIDATOR_VERSION,
    }


def _plan_explorer(ctx: NodeContext, state: RunState) -> RunState:
    """§4.8's pseudocode: r1 = planner call; if unavailable, plan.status =
    'llm_unavailable' and return (no repair, no fallback -- CLAUDE.md §6
    NN13, TASK_PROFILES["plan_explorer"].fallback is None and it is absent
    from FALLBACK_ROLE). Otherwise validate; if the proposal or any test is
    invalid, one repair round on GPT-OSS, re-validated from scratch; its
    report REPLACES the first, which is kept only as validation_before_repair
    for the auditor to see what was fixed. Never a third call."""
    now = ctx.clock()
    sources = (state.profile_result or {}).get("sources", {})
    data_gaps_computed = _explorer_data_gaps(sources)
    profile_payload = {"sources": sources, "data_gaps": data_gaps_computed}
    options = (state.options or {}).get("explorer", {})
    audit_timezone = options.get("audit_timezone") or getattr(ctx.settings, "audit_timezone", None)

    # BUG-EXPLORER-2 (independent review round 2): this run's own pinned
    # reference Skills (resolved once at start_explorer_run, §4.5), already
    # loaded by service.build_node_context -- never re-resolved here.
    reference_skills = list(getattr(ctx, "explorer_reference_skills", None) or [])

    payload = build_planner_payload(
        objective=state.objective, audit_period=state.audit_period, audit_timezone=audit_timezone,
        business_unit=state.business_unit, materiality=state.materiality,
        profile_result=profile_payload, reference_skills=reference_skills,
    )

    if ctx.llm is None or ctx.prompts is None:
        raise ValueError(
            "plan node: Explorer mode requires ctx.llm and ctx.prompts "
            "(service.build_node_context builds both for an explorer run)"
        )
    llm, prompts = ctx.llm, ctx.prompts

    call_ctx = CallContext(
        run_id=state.run_id, engagement_id=state.engagement_id, node_name="plan",
        execution_key=state.current_node_attempt_id, actor=state.run_owner,
        pii_columns_masked=_explorer_pii_masked(sources), pii_whitelist=[],
    )
    inputs = _explorer_plan_inputs(profile_payload=profile_payload, prompts=prompts, reference_skills=reference_skills)

    r1 = llm.call(
        task="plan_explorer", seq=1, messages=prompts.render("explorer/planner", **payload),
        desired_params=TASK_PROFILES["plan_explorer"].desired_params, schema=PLAN_PROPOSAL_SCHEMA,
        ctx=call_ctx,
    )
    if r1.status == "unavailable":
        plan_payload = {
            "kind": "explorer", "status": "llm_unavailable",
            "label": narration_resolve.LABEL_LLM_UNAVAILABLE, "proposal": None,
            "proposal_sha256": None, "validation": None, "validation_before_repair": None,
            "llm": {"planner": _explorer_llm_call_entry(r1), "repair": None},
            "data_gaps_computed": data_gaps_computed, "inputs": inputs,
        }
        message = "Explorer plan: LLM unavailable — deterministic profile only, no proposal"
        return dataclasses.replace(state, plan=plan_payload, events=state.events + [_event("plan", message, now)])

    run_sources = sorted(sources)
    pinned_versions = {b["source"]: b["version"] for b in state.data_assets}

    def _validate(wire: dict) -> dict:
        return validate_wire_proposal(
            wire, profile=sources, run_sources=run_sources, data_source=ctx.data_source,
            pinned_versions=pinned_versions,
        )

    proposal = r1.parsed if r1.status == "ok" else None
    stage = "planner"
    if proposal is not None:
        report = _validate(proposal)
    else:
        report = {
            "proposal_errors": [{"rule": "V-S1", "message": f"not valid PlanProposal JSON: {r1.error}"}],
            "tests": {}, "findings": {}, "warnings": [],
        }

    validation_before_repair = None
    repair_entry = None
    needs_repair = bool(report["proposal_errors"]) or any(not t["valid"] for t in report["tests"].values())
    if needs_repair:
        validation_before_repair = report
        previous_output = _canon_json(proposal) if proposal is not None else (r1.text or "")[:20000]
        violations = (
            list(report["proposal_errors"])
            + [
                {"rule": r["rule"], "message": f"tests.{key}: {r['message']}"}
                for key, t in report["tests"].items() for r in t["reasons"]
            ]
            + [
                {"rule": r["rule"], "message": f"findings.{key}: {r['message']}"}
                for key, f in report["findings"].items() for r in f["reasons"]
            ]
        )
        repair_payload = dict(payload)
        repair_payload["previous_output"] = previous_output
        repair_payload["violations_json"] = _canon_json(violations)
        r2 = llm.call(
            task="plan_repair", seq=2, messages=prompts.render("explorer/repair", **repair_payload),
            desired_params=TASK_PROFILES["plan_repair"].desired_params, schema=PLAN_PROPOSAL_SCHEMA,
            ctx=call_ctx,
        )
        repair_entry = _explorer_llm_call_entry(r2)
        if r2.status == "ok":
            proposal = r2.parsed
            report = _validate(proposal)
            stage = "repair"
        # else: r2 failed (unavailable/invalid_output) -- keep the planner's
        # own proposal/report from r1; never a third call (§4.8).

    try:
        canonical = to_canonical(proposal) if proposal is not None else None
    except Exception:  # noqa: BLE001 -- a still schema-invalid proposal may not canonicalise cleanly
        canonical = None

    proposal_sha256 = sha256_bytes(_canon_json(proposal).encode("utf-8")) if proposal is not None else None
    any_valid_test = any(t["valid"] for t in report["tests"].values())
    plan_status = "proposed" if any_valid_test else "no_valid_tests"

    plan_payload = {
        "kind": "explorer", "status": plan_status, "label": None, "proposal": canonical,
        "proposal_sha256": proposal_sha256, "validation": report,
        "validation_before_repair": validation_before_repair,
        "llm": {"planner": _explorer_llm_call_entry(r1), "repair": repair_entry},
        "data_gaps_computed": data_gaps_computed, "inputs": inputs,
    }
    # Greyed tests (validation.tests[key].valid == false) still get their
    # proposal-authored rationale here -- the auditor reads it to understand
    # WHY a test was proposed even when they can never include it (§4.9).
    plan_rationale = {t["key"]: t.get("rationale", "") for t in (canonical.get("tests", []) if canonical else [])}

    n_valid = sum(1 for t in report["tests"].values() if t["valid"])
    n_total = len(report["tests"])
    message = f"Explorer plan proposed ({stage}): {n_valid}/{n_total} test(s) valid"
    return dataclasses.replace(
        state, plan=plan_payload, plan_rationale=plan_rationale,
        events=state.events + [_event("plan", message, now)],
    )


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
            # CLAUDE.md §0.5/NN14: the same declared contract timezone
            # execute_skill() (above) threaded into read_population --
            # without it here too, a UC-backed source's independently
            # queried min/max date would be converted to a different
            # timezone than the engine's own read, and G6 would fail on a
            # boundary row that never actually moved.
            independent = ctx.data_source.column_stats(
                source, version=version, amount_column=amount_col, date_column=date_col,
                audit_timezone=ctx.skill.contract.get("timezone"),
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


def _llm_classification_config(skill) -> tuple[str, dict] | None:
    """The optional `not_testable.llm_classification` block on a plan.yaml
    test entry (independent review 2026-09-24 item 4) -- `(test_id, cfg)`
    for the first test that declares one, or None. The real SKILL-001
    plan.yaml declares no such block, so this returns None for every real
    run today regardless of `enable_row_level_llm` -- the capability is
    built, but nothing in the shipped Skill opts into it."""
    for test in skill.plan.get("tests", []):
        nt = test.get("not_testable")
        if nt and "llm_classification" in nt:
            return test["test_id"], nt["llm_classification"]
    return None


def _build_classify_gateway(ctx: NodeContext):
    from orchestrator.config import NODE_MODELS
    from orchestrator.llm.gateway import LLMGateway

    client = ctx.model_client
    if client is None:  # pragma: no cover -- real client, exercised only with RUN_LIVE_LLM=1
        from orchestrator.adapters.model_databricks import DatabricksModelClient

        client = DatabricksModelClient()
    return LLMGateway(
        settings=ctx.settings, client=client, persistence=ctx.persistence, node_models=NODE_MODELS,
        retry_backoff_s=getattr(ctx.settings, "llm_retry_backoff_s", 5.0),
        timeout_s=getattr(ctx.settings, "llm_timeout_s", 180.0), clock=ctx.clock,
        prompt_template_id="classify/t43", prompt_template_version="1",
        max_transport_attempts=getattr(ctx.settings, "llm_max_transport_attempts", 5),
        retry_backoff_max_s=getattr(ctx.settings, "llm_retry_backoff_max_s", 75.0),
    )


def _maybe_classify_rows(ctx: NodeContext, state: RunState, exceptions: list[dict], now: str) -> tuple[list[dict], str]:
    """Independent review 2026-09-24 item 4: T4.3 row-level LLM
    classification, PII-safe (only the declared row-key and text columns are
    ever read) and off by default. Returns `(exceptions, message_suffix)` --
    `exceptions` unchanged, `message_suffix` `""`, whenever the capability is
    off or the Skill has not opted in, so a normal run is byte-for-byte what
    it was before this capability existed."""
    if not getattr(ctx.settings, "enable_row_level_llm", False):
        return exceptions, ""
    found = _llm_classification_config(ctx.skill)
    if found is None:
        return exceptions, ""
    test_id, cfg = found

    from orchestrator.llm.classify import DEFAULT_BATCH_SIZE, classify_rows, to_persisted_rows
    from orchestrator.llm.gateway import CallContext

    source = cfg["source"]
    row_key_column = cfg.get("row_key_column", "__row_key")
    text_column = cfg["text_column"]
    binding = next((b for b in state.data_assets if b["source"] == source), None)
    if binding is None:
        return exceptions, ""

    # __source/__row_key are always included by every DataSourceAdapter
    # regardless of `columns` (orchestrator.contract.parse_source_bytes) --
    # requesting row_key_column again when it IS "__row_key" would duplicate
    # it in the projection. Only ask for genuinely extra columns.
    extra_columns = [c for c in (row_key_column, text_column) if c not in ("__source", "__row_key")]
    df = ctx.data_source.read_population(source, version=binding["version"], columns=extra_columns)
    rows = df[[row_key_column, text_column]].dropna(subset=[text_column]).to_dict("records")
    if not rows:
        return exceptions, ""

    gateway = _build_classify_gateway(ctx)
    call_ctx = CallContext(
        run_id=state.run_id, engagement_id=state.engagement_id, node_name="classify",
        execution_key=state.current_node_attempt_id, actor=state.run_owner,
        pii_columns_masked=[], pii_whitelist=[text_column],
    )
    results = classify_rows(
        rows, text_column=text_column, row_key_column=row_key_column, gateway=gateway,
        ctx=call_ctx, batch_size=cfg.get("batch_size", DEFAULT_BATCH_SIZE),
    )
    ctx.persistence.write_classification_results(state.run_id, to_persisted_rows(results, now=now))

    if not results:
        # Every batch was unavailable/invalid -- degrade gracefully, stay
        # not_testable rather than claim a result that does not exist
        # (CLAUDE.md §6 NN13).
        return exceptions, ""

    threshold = cfg.get("confidence_threshold", 0.5)
    flagged = [r for r in results if r.personal_expense and r.confidence >= threshold]
    updated = []
    for e in exceptions:
        if e["test_id"] == test_id:
            e = dict(e)
            e["status"] = "exception" if flagged else "pass"
            e["exception_units"] = len(flagged)
            e["reason"] = (
                f"{len(results)}/{len(rows)} claim(s) classified; {len(flagged)} flagged as "
                f"possible personal expense (confidence >= {threshold})"
            )
        updated.append(e)
    return updated, f" T4.3: {len(results)} row(s) classified."


def classify(ctx: NodeContext, state: RunState) -> RunState:
    """The deterministic part -- per-test exception summaries from what
    `execute` already persisted -- always runs. Row-level LLM classification
    for T4.3 (independent review 2026-09-24 item 4) additionally runs ONLY
    when `Settings.enable_row_level_llm` is true AND the Skill's plan.yaml
    opts a test into it via `not_testable.llm_classification`; see
    `_maybe_classify_rows`'s own docstring. Neither is true for SKILL-001
    today, so T4.3 stays `not_testable` ("awaiting governance approval to
    send expense descriptions to a model") on every real run."""
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
    exceptions, classification_suffix = _maybe_classify_rows(ctx, state, exceptions, now)
    n_exceptions = sum(1 for e in exceptions if e["status"] == "exception")
    message = f"{n_exceptions} test(s) with exceptions, {len(exceptions)} test(s) total" + classification_suffix
    return dataclasses.replace(state, exceptions=exceptions, events=state.events + [_event("classify", message, now)])


def _finding_skill_ref(ctx: NodeContext, state: RunState) -> tuple[str | None, str | None]:
    """docs/specs/P6_P8_explorer_llm_design.md §4.12 item 1: `state.skill_id`/
    `state.skill_version` are None throughout an Explorer run (RunState's own
    "None for Explorer") -- ctx.skill is the resolved Skill regardless of
    mode (resolve_run_skill, §4.11), so falling back to its own
    skill_id/version is what makes an Explorer run's findings link to the
    EXPLORER-<run_id> ledger snapshot instead of persisting a null skill_id.
    A Playbook run's state.skill_id is always already set, so this is a pure
    generalisation, not a behaviour change for SKILL-001."""
    if state.skill_id:
        return state.skill_id, state.skill_version
    if ctx.skill is not None:
        return ctx.skill.skill_id, ctx.skill.version
    return None, None


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
    skill_id, skill_version = _finding_skill_ref(ctx, state)
    persisted = ctx.persistence.write_findings(
        state.run_id,
        findings,
        engagement_id=state.engagement_id,
        skill_id=skill_id,
        skill_version=skill_version,
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
    into more than one finding's `max(amount over cited metrics)`). The
    actual exposure computation -- reading bound source amounts, resolving
    each finding's own line contributions, and reducing to the run headline
    -- lives in `orchestrator.exposure` (P6 WP N4, docs/specs/
    P6_narration_design.md §5.3): see that module's own docstring for the
    full design (line identity, monetary_basis, the headline's max-per-line
    rule) and `orchestrator.exposure.compute_run_exposure`'s docstring for
    what it returns. This node is now the thin persistence wrapper: load
    this run's findings/metrics/flagged rows, call into `exposure`, and
    write back the findings, the `test_line_values` table (new -- every
    testable plan test's per-row spend/excess amount, so `finalise`, P6 WP
    N10, can later recompute the headline over rule findings plus accepted
    AI-proposed candidates without re-reading source data) and `run_metrics`
    (the "Potential exposure" and "Approved but never spent" figures).

    `prioritise` is still the only node besides `execute` that reads raw
    source data (no primitive or population exposes a row_key -> amount
    map), and `test_line_values` is still built from the SAME raw reads
    `exposure.compute_run_exposure` performs once per run, never a
    per-finding re-read."""
    persisted = ctx.persistence.list_findings(state.run_id)
    now = ctx.clock()

    if not persisted:
        message = "no findings to prioritise"
        return dataclasses.replace(state, findings=[], events=state.events + [_event("prioritise", message, now)])

    existing_metrics = ctx.persistence.get_run_metrics(state.run_id)
    rows_by_flag: dict[str, list[dict]] = {}
    for r in ctx.persistence.list_flagged_rows(state.run_id):
        rows_by_flag.setdefault(r["flag"], []).append(r)

    outcome = exposure.compute_run_exposure(ctx, state, ctx.skill, persisted, existing_metrics, rows_by_flag)
    updated_findings = outcome["updated_findings"]

    skill_id, skill_version = _finding_skill_ref(ctx, state)
    ctx.persistence.write_findings(
        state.run_id,
        updated_findings,
        engagement_id=state.engagement_id,
        skill_id=skill_id,
        skill_version=skill_version,
        now=now,
    )
    ctx.persistence.put_test_line_values(state.run_id, outcome["test_line_values"])

    headline_exposure = outcome["headline_exposure"]
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
            "label": "Potential exposure",
            "basis": (
                "The amount at risk: every distinct flagged transaction line counts exactly once, "
                "at the largest amount any finding attributes to it. A finding that flags a full "
                "spend amount counts the line's own full value; a finding that flags only an "
                "excess over a limit counts only the at-risk portion (for example the extra lines "
                "beyond the first in a duplicate group, or the part of a per-diem day's spend that "
                "is over the daily limit, allocated pro rata to that day's lines). Line identity is "
                "the row's own identity for a one-row-per-entry source, or a repeats-grain key "
                "declared in the Skill's source contract where one source row is genuinely the same "
                "business entry as another (e.g. one row per attendee of one entertainment claim). "
                "Findings for money that was approved but never spent are excluded here and "
                "reported separately as approved-but-unspent; findings with no dollar figure are "
                "also excluded. Per-finding exposure figures overlap with each other and with this "
                "headline by design (the same line can be cited by more than one finding) and must "
                "never be summed."
            ),
            "sources": outcome["headline_provenance"],
        },
        "test_id": None,
    }
    metrics_map["run_approved_not_spent_total"] = {
        "metric_name": "run_approved_not_spent_total",
        "value": outcome["approved_not_spent_total"],
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
    An action's `description` is the EFFECTIVE remediation draft (P6 WP N7,
    docs/specs/P6_narration_design.md §2/§4.6, now resolved through WP N10's
    `orchestrator.narration.resolve.effective_remediation`): `narrate` (now
    running just before this node, CLAUDE.md §4.2's amended fieldwork order)
    already wrote one `narratives` row per finding for field `remediation`;
    this node reads it back rather than the raw rule-authored
    `recommendation` -- falling back to that same `recommendation` when
    narration is off, or this finding's own remediation draft never
    validated (`effective_remediation`'s own fallback). `description_origin`
    (migration 011) records which of the three it actually was.

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
    skill_id, _skill_version = _finding_skill_ref(ctx, state)
    narratives_by_target = {
        (r["target_kind"], r["target_id"], r["field"]): r for r in ctx.persistence.get_narratives(state.run_id)
    }

    actions = []
    for f in findings:
        table = build_finding_table(f, skill=ctx.skill, period=state.audit_period)
        resolved = narration_resolve.effective_remediation(f, narratives_by_target, table=table)
        actions.append(
            {
                "action_id": f"MA-{f['finding_id']}",
                "issue_id": f"ISS-{f['finding_id']}",
                "finding_id": f["finding_id"],
                "engagement_id": state.engagement_id,
                "skill_id": skill_id,
                "title": f["title"],
                "description": resolved["text"],
                "owner": None,
                "risk": f["severity"],
                "status": "draft",
                "target_date": None,
                "potential_exposure": f.get("exposure_amount"),
                "evidence_link": f.get("test_id"),
                # P6 §6.1 migration 011: template|model|human, from the SAME
                # resolver status `effective_remediation` returned -- a
                # fallback status ('fallback_invalid'/'fallback_unavailable',
                # or no narrative row at all) means the template recommendation
                # was used, never a model/human draft.
                "description_origin": (
                    "human" if resolved["status"] == "human_edit"
                    else "model" if resolved["status"] in ("model", "model_repaired")
                    else "template"
                ),
            }
        )
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


def _write_recon_value(ws, row: int, col: int, value, fmt=None) -> None:
    """A Reconciliation-sheet cell whose value genuinely was not computed
    (None) writes as "—", never a fabricated 0/0.0 (independent review
    2026-09-24 item 4, CLAUDE.md NN14 / the "—" decision, §11) -- a real
    computed 0 still writes as the number 0."""
    if value is None:
        _write_str(ws, row, col, "—")
    else:
        ws.write_number(row, col, value, fmt)


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


# ── P6 WP N11 (docs/specs/P6_narration_design.md §9): the export narration
# bundle, resolved ONCE and shared by both exporters (PPTX -- generate_pptx
# -- and XLSX -- _write_xlsx_workpaper), so a paragraph reads identically on
# both artefacts (G13). Built here, not in orchestrator/pptx_export.py:
# orchestrator/narration/payloads.py itself imports pptx_export.py (for
# load_catalogue_rows), so pptx_export.py importing payloads.py back would
# be a circular import -- this module already imports both, so resolution
# happens here and only fully-rendered plain text/labels/dicts cross into
# either exporter (§6.4's own resolver, never a raw `{class:name}` span).
_NARRATIVE_TEXT_ORIGINS = ("model", "model_repaired", "human_edit")


def _narrative_sheet_row(*, target_kind: str, target_id: str, field: str, resolved: dict) -> dict:
    numbers_from = ", ".join(sorted({s["source_field"] for s in (resolved.get("sources") or [])}))
    text = resolved.get("text")
    if isinstance(text, list):
        text = " || ".join(text)
    return {
        "target_kind": target_kind, "target_id": target_id, "field": field,
        "status": resolved.get("status") or "fallback_unavailable", "label": resolved.get("label") or "",
        "text": text, "numbers_from": numbers_from,
    }


def _build_export_narration(ctx: NodeContext, state: RunState, findings: list[dict], metrics: dict[str, dict]) -> dict:
    """Resolves every model-written field this export needs, once: rule
    findings' own observation/recommendation/management_questions
    (`effective_prose` over `target_kind='finding'`, unchanged for an
    accepted AI-proposed finding, whose text `finalise` already rendered and
    froze at acceptance -- §5.2's own note that a candidate has no separate
    'finding'-kind narrative row), this generation's confirmed themes (never
    a superseded or prior-generation row), the exec summary and the one
    chart caption the deck renders, plus a flat `narrative_rows` list for
    the XLSX Narrative sheet (§9) -- rejected/superseded/undecided candidate
    rows are never read back into it (§14 Q10: "rejected candidates ...
    excluded from all exports")."""
    skill = ctx.skill
    period = tuple(state.audit_period) if state.audit_period else None
    generation = int((state.options or {}).get("narration_generation", 0) or 0)
    all_narratives = ctx.persistence.get_narratives(state.run_id)
    narratives_by_target = {(r["target_kind"], r["target_id"], r["field"]): r for r in all_narratives}
    versions = ((state.signoff or {}).get("narration") or {}).get("narrative_versions")
    candidates_by_id = {c["candidate_id"]: c for c in ctx.persistence.list_candidates(state.run_id)}
    findings_by_id = {f["finding_id"]: f for f in findings}

    resolved_findings: list[dict] = []
    narrative_rows: list[dict] = []

    for f in findings:
        if f.get("origin") == "ai_proposed":
            # §5.2: an accepted candidate's observation/recommendation/
            # management_questions were already rendered and frozen by
            # `finalise` at acceptance time -- no 'finding'-kind narrative
            # row exists to re-resolve here. Its own 'candidate'-kind rows
            # still exist (for the Narrative sheet, below) and are read
            # back through the SAME candidate metrics table `finalise`
            # itself used (§5.1's exposure table), never a raw metric name.
            label = narration_resolve.accepted_candidate_label(f.get("accepted_by") or "—")
            resolved_findings.append({**f, "prose_label": label, "narration_status": "ai_proposed_accepted"})
            candidate = candidates_by_id.get(f.get("candidate_id"))
            if candidate is not None:
                cand_table = narration_resolve.metrics_placeholder_table(candidate.get("metrics_cited") or [], metrics)
                for field_name, is_list in (("observation", False), ("recommendation", False), ("management_questions", True)):
                    row = narratives_by_target.get(("candidate", candidate["candidate_id"], field_name))
                    if row is None:
                        continue
                    resolved = narration_resolve.effective_prose(
                        target_kind="candidate", target_id=candidate["candidate_id"], field=field_name,
                        narratives_by_target=narratives_by_target, table=cand_table, fallback_text=None,
                        is_list=is_list, versions=versions, accepted_label=label,
                    )
                    narrative_rows.append(
                        _narrative_sheet_row(target_kind="finding", target_id=f["finding_id"], field=field_name, resolved=resolved)
                    )
            continue

        table = build_finding_table(f, skill=skill, period=period)
        field_results: dict[str, dict] = {}
        for field_name, fallback, is_list in (
            ("observation", f.get("observation"), False),
            ("recommendation", f.get("recommendation"), False),
            ("management_questions", f.get("management_questions") or [], True),
        ):
            resolved = narration_resolve.effective_prose(
                target_kind="finding", target_id=f["finding_id"], field=field_name,
                narratives_by_target=narratives_by_target, table=table, fallback_text=fallback,
                is_list=is_list, versions=versions,
            )
            field_results[field_name] = resolved
            narrative_rows.append(
                _narrative_sheet_row(target_kind="finding", target_id=f["finding_id"], field=field_name, resolved=resolved)
            )
        resolved_findings.append({
            **f,
            "observation": field_results["observation"]["text"],
            "recommendation": field_results["recommendation"]["text"],
            "management_questions": field_results["management_questions"]["text"] or [],
            # §9's own PPTX table: only an accepted candidate (above) and the
            # analyst-set-threshold chip (already rendered by pptx_export.py
            # itself) carry a label on a Top Matters slide -- a rule
            # finding's own fallback/degraded status is a RUN-level label
            # (exec summary / What we found / XLSX run metadata), never
            # repeated per finding (§7 UI-7).
            "prose_label": None,
            "observation_status": field_results["observation"]["status"],
            "recommendation_status": field_results["recommendation"]["status"],
            "management_questions_status": field_results["management_questions"]["status"],
        })

    themes_rows = [
        t for t in ctx.persistence.list_themes(state.run_id)
        if t.get("generation") == generation and not t.get("superseded")
    ]
    theme_blocks: list[dict] = []
    for theme in themes_rows:
        members = [findings_by_id[fid] for fid in theme.get("finding_ids", []) if fid in findings_by_id]
        theme_table = build_theme_table(members, skill=skill)
        title = narration_resolve.effective_prose(
            target_kind="theme", target_id=theme["theme_id"], field="title",
            narratives_by_target=narratives_by_target, table=theme_table, fallback_text=None, versions=versions,
        )
        summary = narration_resolve.effective_prose(
            target_kind="theme", target_id=theme["theme_id"], field="summary",
            narratives_by_target=narratives_by_target, table=theme_table, fallback_text=None, versions=versions,
        )
        root_cause = narration_resolve.effective_prose(
            target_kind="theme", target_id=theme["theme_id"], field="root_cause",
            narratives_by_target=narratives_by_target, table=theme_table, fallback_text=None, versions=versions,
        )
        if title["text"] is None or summary["text"] is None:
            # A theme row this run's `narrate` wrote (finding_ids etc.) but
            # whose own title/summary narrative never validated (§3.5's own
            # per-FIELD fallback, never a whole-theme one) has nothing
            # reviewed to show -- CLAUDE.md NN14, never a blank block.
            continue
        theme_blocks.append({
            "theme_id": theme["theme_id"], "title": title["text"], "summary": summary["text"],
            "root_cause": root_cause["text"],
            "members": [{"severity": m.get("severity"), "title": m.get("title")} for m in members],
        })
        for field_name, resolved in (("title", title), ("summary", summary), ("root_cause", root_cause)):
            if resolved["text"] is None:
                continue
            narrative_rows.append(
                _narrative_sheet_row(target_kind="theme", target_id=theme["theme_id"], field=field_name, resolved=resolved)
            )

    # The one bookkeeping row `narrate_synthesis` persists on a fallback
    # (no confirmed themes at all, §3.5: "synthesis -> no themes"): a
    # `theme`/'run'/'summary' row whose own origin/label carries the reason
    # (narration off, unavailable or invalid) -- probed the same way any
    # other field is, never a private re-implementation of the fallback-label
    # rule (`resolve._FALLBACK_LABELS`).
    themes_probe = narration_resolve.effective_prose(
        target_kind="theme", target_id="run", field="summary",
        narratives_by_target=narratives_by_target, table={}, fallback_text=None, versions=None,
    )
    themes_label = None if theme_blocks else themes_probe["label"]

    from orchestrator.catalogue_counts import catalogue_tests_for_skill

    # Independent narration-content review 2026-09-25 (BUG-4c, found live):
    # `narrate_exec_summary`/`build_exec_summary_payload` (generation time)
    # builds its table with `build_run_table`, which merges each top
    # finding's own placeholders (`run_exposure_dominant_amount`/`_title`/
    # `_basis` among them, `orchestrator.narration.run_values.run_values`'s
    # own new entries) into the bare run-level table -- a model may
    # therefore write one of those into its exec summary. Reading it back
    # here with the bare `run_values()` (this table's OLD source) does not
    # carry those names, so `render()` raises `NarrationConfigError` on a
    # perfectly valid, already-validated row and the reader falls back to
    # `render_error`/"model text failed validation" for the WHOLE exec
    # summary -- exactly BUG-4b's own shape (a mismatched re-validation
    # table), reproduced live by this WP. `build_run_table` is the same
    # table `orchestrator.service._narrative_table`'s "run" case already
    # uses for the human-edit path; this is the third and last caller that
    # must build it the same way.
    run_table = build_run_table(
        state, findings, metrics, skill=ctx.skill, catalogue_tests=catalogue_tests_for_skill(ctx.skill) if ctx.skill else [],
    )
    exec_resolved = narration_resolve.effective_prose(
        target_kind="run", target_id="run", field="exec_summary",
        narratives_by_target=narratives_by_target, table=run_table, fallback_text=None,
        is_list=True, versions=versions,
    )
    if exec_resolved["status"] in _NARRATIVE_TEXT_ORIGINS:
        approver = (state.signoff or {}).get("approver") or "—"
        exec_summary_label = (
            f"Model-written summary, reviewed at sign-off by {approver}; "
            f"every number inserted from this run's results"
        )
    else:
        exec_summary_label = exec_resolved["label"]
    narrative_rows.append(
        _narrative_sheet_row(target_kind="run", target_id="run", field="exec_summary", resolved=exec_resolved)
    )

    # Independent narration-content review 2026-09-25 (BUG-4c, same shape as
    # the exec-summary fix above): `narrate_captions`/`_chart_specs`
    # (generation time) may add `run_exposure_dominant_amount`/`_title`/
    # `_basis` to the 'risk_and_exposure' chart's own table -- reconstruct
    # the SAME per-chart table here, exactly as `orchestrator.service.
    # _narrative_table`'s "chart" case (BUG-4b) already does, rather than a
    # bare single-metric table that cannot resolve those names.
    from orchestrator.nodes.narration import _chart_specs

    chart_specs, chart_metrics = _chart_specs(findings, metrics)
    _, chart_tables = build_caption_payload(chart_specs, chart_metrics)
    caption_table = chart_tables.get("risk_and_exposure", {})
    caption_resolved = narration_resolve.effective_prose(
        target_kind="chart", target_id="risk_and_exposure", field="caption",
        narratives_by_target=narratives_by_target, table=caption_table, fallback_text=None, versions=versions,
    )
    if caption_resolved["text"]:
        narrative_rows.append(
            _narrative_sheet_row(target_kind="chart", target_id="risk_and_exposure", field="caption", resolved=caption_resolved)
        )

    served_model_versions = sorted({n["served_model_version"] for n in all_narratives if n.get("served_model_version")})
    accepted_ai_findings = [f for f in resolved_findings if f.get("origin") == "ai_proposed"]

    return {
        "findings": resolved_findings,
        "themes": theme_blocks,
        "themes_label": themes_label,
        "accepted_ai_findings": accepted_ai_findings,
        "exec_summary_paragraphs": exec_resolved["text"],
        "exec_summary_label": exec_summary_label,
        "risk_chart_caption": caption_resolved["text"],
        "generation": generation,
        "served_model_versions": served_model_versions,
        "narrative_rows": narrative_rows,
    }


def _code_revision_export_note(computed_code_revision: str | None, export_code_revision: str | None) -> str | None:
    """CLAUDE.md §11 "Paused runs across a code deploy" / independent review
    2026-09-24 gap #11: once orchestrator.pipeline.run_phase has recorded an
    export_code_revision for this run that differs from the code_revision
    its (immutable) run_fingerprints row was created under -- meaning
    `execute` computed this run's numbers on one deployment and `export` ran
    on a later one -- both exported artefacts say so. Absent (never a
    fabricated "unchanged" line) whenever export_code_revision was never
    recorded, or was recorded but happens to equal computed_code_revision
    (an export re-run on the SAME deployment that created the run)."""
    if computed_code_revision and export_code_revision and export_code_revision != computed_code_revision:
        return f"Computed under code revision {computed_code_revision}, exported under {export_code_revision}"
    return None


def _write_xlsx_workpaper(
    state: RunState, findings: list[dict], metrics: dict[str, dict], flagged_rows: list[dict], now: str,
    ticket_previews: list[dict] | None = None, narration: dict | None = None,
    code_revision_note: str | None = None,
) -> bytes:
    # P6 WP N11: `findings` here is already the export narration bundle's
    # OWN resolved list (its observation/recommendation/management_questions
    # are effective text, never a raw findings.yaml template when narration
    # produced something reviewed) -- `narration` (§9) supplies everything
    # ELSE this sheet set needs: the run-level generation/served-model
    # metadata and the flat Narrative-sheet rows. `None` (no caller passes
    # it today outside `export()`) degrades to "no narration to report" --
    # every new column/sheet below still writes, just with nothing to show.
    narration = narration or {}
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
    # CLAUDE.md §11 "Paused runs across a code deploy": absent for the
    # common case (see _code_revision_export_note's own docstring).
    if code_revision_note:
        cover_fields.append(("code_revision_note", code_revision_note))
    signoff = state.signoff or {}
    if signoff:
        cover_fields.append(("signed_off_by", signoff.get("approver")))
        cover_fields.append(("signed_off_at", signoff.get("timestamp")))
        if signoff.get("self_approved"):
            cover_fields.append(("signoff_note", SELF_APPROVED_LABEL))
    # P6 WP N11 (§9 "run metadata gains the narration generation, the served
    # model versions, and the count of accepted AI-proposed findings"):
    # additive only, so a run that never touched narration (the field
    # defaults/omissions above) writes the same three rows with honest
    # empty values, never a fabricated "0 generation(s)".
    cover_fields.append(("narration_generation", narration.get("generation")))
    served_model_versions = narration.get("served_model_versions") or []
    cover_fields.append(("narration_served_model_versions", ", ".join(served_model_versions) or "—"))
    accepted_ai_count = sum(1 for f in findings if f.get("origin") == "ai_proposed")
    cover_fields.append(("ai_proposed_findings_accepted", accepted_ai_count))
    for r, (label, value) in enumerate(cover_fields, start=1):
        _write_str(cover, r, 0, label, bold)
        _write_str(cover, r, 1, value)
    _write_str(cover, len(cover_fields) + 2, 0, footer)

    ws = wb.add_worksheet("Findings")
    headers = [
        "finding_id", "rule_id", "test_id", "severity", "analyst_set_severity", "severity_basis",
        "title", "observation", "recommendation", "exposure_amount", "exposure_basis", "review_state",
        # P6 WP N11 (§9): origin/accepted_by/proposed-vs-decided severity,
        # and each prose field's own resolved status (model/model_repaired/
        # human_edit/fallback_invalid/fallback_unavailable, or
        # 'ai_proposed_accepted' for a candidate whose text `finalise`
        # already froze at acceptance, §5.2).
        "origin", "accepted_by", "proposed_severity", "decided_severity",
        "observation_status", "recommendation_status", "management_questions_status",
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
        origin = f.get("origin") or "rule"
        _write_str(ws, r, 12, origin)
        _write_str(ws, r, 13, f.get("accepted_by"))
        _write_str(ws, r, 14, f.get("proposed_severity"))
        _write_str(ws, r, 15, f["severity"] if origin == "ai_proposed" else "")
        if origin == "ai_proposed":
            # A finding this frozen at acceptance (§5.2) has no per-field
            # narration status of its own -- the SAME single
            # 'ai_proposed_accepted' status for all three, never a
            # fabricated model/fallback status it never actually had.
            _write_str(ws, r, 16, "ai_proposed_accepted")
            _write_str(ws, r, 17, "ai_proposed_accepted")
            _write_str(ws, r, 18, "ai_proposed_accepted")
        else:
            _write_str(ws, r, 16, f.get("observation_status"))
            _write_str(ws, r, 17, f.get("recommendation_status"))
            _write_str(ws, r, 18, f.get("management_questions_status"))
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
        # Independent review 2026-09-24 item 4 (CLAUDE.md NN14, the "—"
        # decision, §11): `rec.get(...) or 0` wrote a real 0 for a value
        # that was never computed at all -- e.g. `engine_rows`/`variance`
        # are None when this source's raw_<source> population was never
        # bound (fieldwork.py's own per-source loop above), which reads on
        # the sheet as "reconciled with zero variance", a fabricated pass,
        # not an absence. "—", never a written 0, when the value is None;
        # an actually-computed 0 still writes as the number 0.
        _write_recon_value(ws4, r, 1, rec.get("engine_rows"))
        _write_recon_value(ws4, r, 2, rec.get("independent_rows"))
        _write_recon_value(ws4, r, 3, rec.get("variance"))
        if rec.get("amount") is None:
            # No amount_column was declared for this source at all -- not
            # just "not computed this run" -- so the more specific reason
            # stays, rather than the bare "—" used when a column WAS
            # declared but one side of it individually came back empty.
            _write_str(ws4, r, 4, "n/a — no amount column declared")
            _write_str(ws4, r, 5, "n/a — no amount column declared")
            _write_str(ws4, r, 6, "n/a — no amount column declared")
        else:
            ws4.write_number(r, 4, rec["amount"], money)
            _write_recon_value(ws4, r, 5, rec.get("independent_amount"), fmt=money)
            _write_recon_value(ws4, r, 6, rec.get("amount_variance"), fmt=money)
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

    # P6 WP N11 (§9): "a Narrative sheet listing every paragraph with its
    # status, label and the RunState fields that supplied its numbers
    # (NN12)" -- `narration["narrative_rows"]` (built once by
    # `_build_export_narration`, shared with the PPTX) already excludes a
    # rejected/superseded/undecided candidate's own rows entirely (§14 Q10),
    # so this sheet writes exactly what it is handed, in the same order.
    narrative_rows = narration.get("narrative_rows") or []
    ws7 = wb.add_worksheet("Narrative")
    ws7.write_row(0, 0, ["target_kind", "target_id", "field", "status", "label", "text", "numbers_from"], bold)
    for r, row in enumerate(narrative_rows, start=1):
        _write_str(ws7, r, 0, row["target_kind"])
        _write_str(ws7, r, 1, row["target_id"])
        _write_str(ws7, r, 2, row["field"])
        _write_str(ws7, r, 3, row["status"])
        _write_str(ws7, r, 4, row["label"])
        _write_str(ws7, r, 5, row["text"])
        _write_str(ws7, r, 6, row["numbers_from"])
    _write_str(ws7, len(narrative_rows) + 2, 0, footer)

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

    # CLAUDE.md §11 "Paused runs across a code deploy" / independent review
    # 2026-09-24 gap #11: the SAME two reads service.get_run already makes
    # for its own run-page label (run_fingerprints stays immutable, so
    # `execute`'s own code_revision lives only on the fingerprint;
    # orchestrator.pipeline.run_phase records what actually ran export on
    # `runs.export_code_revision` when the two differ).
    fingerprint = ctx.persistence.get_fingerprint(state.fingerprint_id)
    computed_code_revision = (fingerprint or {}).get("code_revision")
    run_row = ctx.persistence.get_run_row(state.run_id)
    export_code_revision = (run_row or {}).get("export_code_revision")
    code_revision_note = _code_revision_export_note(computed_code_revision, export_code_revision)

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

    # P6 WP N11 (docs/specs/P6_narration_design.md §9): resolved ONCE here,
    # shared by both exporters below -- `narration["findings"]` carries the
    # SAME findings, with observation/recommendation/management_questions
    # replaced by their effective (reviewed model, or reviewed template
    # fallback) text, so both artefacts show identical prose (G13).
    narration = _build_export_narration(ctx, state, findings, metrics)
    resolved_findings = narration["findings"]

    content = _write_xlsx_workpaper(
        state, resolved_findings, metrics, flagged_rows, now, ticket_previews, narration=narration,
        code_revision_note=code_revision_note,
    )
    sha256 = hashlib.sha256(content).hexdigest()
    rel_path = f"exports/{state.run_id}/workpaper.xlsx"
    written_path = ctx.export_storage.write(rel_path, content)
    ctx.persistence.record_export(
        state.run_id, "xlsx", path=written_path, sha256=sha256, created_by=state.run_owner, now=now,
    )

    exports = {**(state.exports or {}), "xlsx": {"path": written_path, "sha256": sha256, "kind": "xlsx"}}

    # PPTX audit pack (CLAUDE.md §4.7): same "persisted outputs only, never
    # recomputed" rule as the XLSX above -- generate_pptx reads `state`,
    # the resolved `findings`, `metrics`, this Skill's own static
    # catalogue.yaml and the same `narration` bundle, nothing else.
    catalogue_rows = load_catalogue_rows(ctx.skill.skill_dir)
    data_mode = "Local test data" if ctx.backend == "local" else "Unity Catalog"
    pptx_content = generate_pptx(
        state, resolved_findings, metrics, catalogue_rows, ctx.skill,
        data_mode=data_mode, template_path=ctx.settings.pptx_template_path, now=now, narration=narration,
        code_revision_note=code_revision_note,
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
            # P6 WP N7 (docs/specs/P6_narration_design.md §2): `narrate` runs
            # after `prioritise` and before `act` so `act` can read back the
            # remediation draft `narrate` just wrote (above).
            ("narrate", narrate),
            ("act", act),
        ],
        # P6 WP N10 (docs/specs/P6_narration_design.md §2): `finalise` is the
        # NEW first export-phase node -- it runs after sign-off (§2.4's gate)
        # and before `export`, so every accepted AI-proposed finding, the
        # recomputed headline and the rewritten issues/actions are already in
        # Delta by the time `export` reads them.
        "export": [("finalise", finalise), ("export", export)],
    }
}
