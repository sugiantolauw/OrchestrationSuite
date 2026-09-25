"""Payload builders for every run-time narration model call (P6 WP N5,
docs/specs/P6_narration_design.md §3.2, §4.1). One `build_<task>_payload`
function per task in the §4.1 table, each returning `(payload, table)` (or
`(payload, {key: table})` for a multi-item call) -- `payload` is the plain,
JSON-serialisable dict `$payload_json` renders in the task's prompt
template; `table` is the matching `{name: PlaceholderEntry}` this same data
produces, which `orchestrator.narration.validate.validate_prose` and
`orchestrator.narration.placeholders.render` both take as their `table`
argument to check and render whatever the model writes back.

Three rules every builder here follows, because this is where CLAUDE.md
non-negotiable 2 and G15 (PII egress) actually get enforced on the way OUT
to a model, not just on the way back:

  * NEVER ROWS. Every value here is an aggregate already computed and
    persisted by a deterministic node (`run_metrics`, `findings`,
    `test_results`, `profile_result`) -- this module reads those dicts, it
    never touches `ctx.data_source` or any DataFrame.
  * NEVER A PII COLUMN. `pii_columns_masked()` walks `skill.contract` for
    every column tagged `pii: true`; callers pass it into
    `orchestrator.llm.gateway.CallContext` (`narration_call_context()`,
    below) so a PII-tagged column can never reach a payload without an
    explicit whitelist entry the Skill itself does not grant here (§4.1:
    `pii_whitelist = []`, always). Nothing in this module ever reads a raw
    contract column value in the first place, so there is no column NAME
    check to bypass -- the guarantee lives in what data these functions are
    given (aggregates), not in a filter applied after the fact.
  * A None-valued metric is OMITTED, never rendered as a fabricated figure
    (§3.2: "Rows whose value is None are omitted"; CLAUDE.md non-negotiable
    14). `_serialise_table` drops every `PlaceholderEntry` whose `value` is
    `None` before it ever reaches a payload dict; the full table (including
    the omitted rows) is still returned, so a model reference to an
    omitted-but-known name is later caught as N-V1 (a known placeholder
    with no value), not N-G3 (unknown name entirely)."""

from __future__ import annotations

import re

from orchestrator.findings import format_metric_value
from orchestrator.llm.gateway import CallContext
from orchestrator.narration.placeholders import PlaceholderEntry, class_for_unit
from orchestrator.pptx_export import load_catalogue_rows

__all__ = [
    "pii_columns_masked",
    "narration_call_context",
    "finding_key",
    "build_finding_table",
    "build_theme_table",
    "build_finding_payload",
    "build_synthesis_payload",
    "build_candidates_payload",
    "build_priority_payload",
    "build_remediation_payload",
    "build_exec_summary_payload",
    "build_caption_payload",
    "build_profile_payload",
]

_SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2, "Indeterminate": 3}


def _canonical_json(value) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ── PII / CallContext (§4.1 "Never sent") ───────────────────────────────────


def pii_columns_masked(skill) -> list[str]:
    """Every contract column tagged `pii: true`, across every declared
    source, sorted -- what `CallContext.pii_columns_masked` records (§4.1;
    G15). A column NAME here is not itself sensitive; it is the audit trail
    of what this run's contract withheld, never a value."""
    columns: set[str] = set()
    for source_cfg in (skill.contract.get("sources") or {}).values():
        for column, spec in (source_cfg.get("columns") or {}).items():
            if spec.get("pii"):
                columns.add(column)
    return sorted(columns)


def narration_call_context(
    skill, *, run_id: str, engagement_id: str | None = None, node_name: str = "narrate",
    execution_key: str | None = None, actor: str = "system",
) -> CallContext:
    """Every narration call's `CallContext` (§4.1: `pii_whitelist = []`,
    always -- narration never whitelists a PII column the way `classify`
    whitelists its one free-text column, orchestrator.nodes.fieldwork's own
    `_maybe_classify_rows`)."""
    return CallContext(
        run_id=run_id, engagement_id=engagement_id, node_name=node_name,
        execution_key=execution_key, actor=actor,
        pii_columns_masked=pii_columns_masked(skill), pii_whitelist=[],
    )


# ── catalogue lookup (test_name / control_objective by test_id) ────────────
#
# Duplicated matching logic, not imported from orchestrator.service or
# orchestrator.pptx_export -- the same deliberate choice pptx_export.py's
# own `_catalogue_id_for` documents (avoiding a cross-layer/circular import
# between this package and those modules). `load_catalogue_rows` itself
# (no leading underscore -- a public helper) IS imported: only the small
# id-matching rule is re-stated here.


def _catalogue_id_for(plan_test_id: str, catalogue_ids: set[str]) -> str:
    if plan_test_id in catalogue_ids:
        return plan_test_id
    for cid in catalogue_ids:
        if plan_test_id.startswith(f"{cid}_"):
            return cid
    return plan_test_id


def _catalogue_lookup(skill) -> dict[str, dict]:
    return {row["test_id"]: row for row in load_catalogue_rows(skill.skill_dir)}


def _catalogue_row(skill, test_id: str | None) -> dict:
    if not test_id:
        return {}
    lookup = _catalogue_lookup(skill)
    return lookup.get(_catalogue_id_for(test_id, set(lookup)), {})


# ── finding/candidate identity ──────────────────────────────────────────────


def finding_key(item: dict) -> str:
    """§1 point 8: "Items are keyed by the rule id with its Skill prefix
    removed" -- `rule_id` is always `<skill_prefix>.<id>` (orchestrator.
    findings.build_findings) or `<skill_prefix>.ai.<hash>` for a candidate
    (§5.1); this strips everything up to and including the first `.`,
    leaving `T4_1` / `ai.<hash>`. Falls back to `finding_id`/`candidate_id`
    for a caller that has not set `rule_id` (e.g. a bare test fixture)."""
    rule_id = item.get("rule_id") or item.get("finding_id") or item.get("candidate_id") or ""
    return rule_id.split(".", 1)[1] if "." in rule_id else rule_id


# ── per-item metric table (§3.2's "finding prose" row, shared by find,
# priority rationale, remediation and synthesis's theme validation) ────────


def _metric_entry(name: str, metric: dict, *, test_id: str | None, test_name: str | None, source_field: str) -> PlaceholderEntry | None:
    value = metric.get("value")
    unit = metric.get("unit")
    if value is None:
        return None
    cls = class_for_unit(unit)
    meaning = f"{cls} metric of test {test_id} ({test_name}), unit {unit}"
    label = (metric.get("source_ref") or {}).get("label")
    if label:
        meaning = f"{meaning}; {label}"
    return PlaceholderEntry(name=name, unit=unit, value=value, source_field=source_field, meaning=meaning)


def build_finding_table(finding: dict, *, skill, period: tuple[str, str] | None = None) -> dict[str, PlaceholderEntry]:
    """This finding's own placeholder table (§3.2): its non-null
    `metrics_cited`, its `threshold_refs`, `exposure_amount` (if not null),
    and -- when `period` is given -- the run's audit-period dates. Shared by
    `find`, `prioritise` and `act`'s payload builders below (§4.1: "that
    finding's table" / "that finding's or candidate's table"), and by
    `build_theme_table` for synthesis output validation."""
    finding_id = finding.get("finding_id") or finding.get("candidate_id") or finding_key(finding)
    test_id = finding.get("test_id")
    test_name = _catalogue_row(skill, test_id).get("test_name")

    table: dict[str, PlaceholderEntry] = {}
    for name, metric in (finding.get("metrics_cited") or {}).items():
        entry = _metric_entry(
            name, metric, test_id=test_id, test_name=test_name,
            source_field=f"findings[{finding_id}].metrics_cited.{name}",
        )
        if entry is not None:
            table[name] = entry

    for ref in finding.get("threshold_refs") or []:
        tid = ref.get("id")
        value = ref.get("value")
        if tid is None or value is None:
            continue
        unit = ref.get("unit") or "value"
        provenance = (
            "analyst-set, pending policy confirmation" if ref.get("pending_policy_confirmation")
            else (ref.get("provenance_type") or "unspecified provenance")
        )
        table[tid] = PlaceholderEntry(
            name=tid, unit=unit, value=value, source_field=f"thresholds.{tid}",
            meaning=f"threshold {tid} ({provenance}), unit {unit}",
        )

    exposure_amount = finding.get("exposure_amount")
    if exposure_amount is not None:
        table["exposure_amount"] = PlaceholderEntry(
            name="exposure_amount", unit="AUD", value=exposure_amount,
            source_field=f"findings[{finding_id}].exposure_amount",
            meaning=(
                "this finding's own amount at risk (its cited amount metric(s) summed) -- "
                "overlaps by design with other findings and with the run headline, and is "
                "never itself the headline"
            ),
        )

    if period:
        start, end = period
        table["run_audit_period_start"] = PlaceholderEntry(
            name="run_audit_period_start", unit="date", value=start,
            source_field="RunState.audit_period", meaning="the audit period's start date",
        )
        table["run_audit_period_end"] = PlaceholderEntry(
            name="run_audit_period_end", unit="date", value=end,
            source_field="RunState.audit_period", meaning="the audit period's end date",
        )

    return table


def build_theme_table(member_findings: list[dict], *, skill) -> dict[str, PlaceholderEntry]:
    """§3.2 "synthesis (per theme)" row: the union of the member findings'
    own tables (metric names are unique per run, so a later finding's entry
    for the same name is identical, never conflicting) -- built from the
    model's OWN theme grouping once `find_synthesis` has answered, so this
    is called for validation, not for the `find_synthesis` payload itself
    (which sends every finding's table up front; see
    `build_synthesis_payload`)."""
    table: dict[str, PlaceholderEntry] = {}
    for finding in member_findings:
        table.update(build_finding_table(finding, skill=skill))
    return table


def _serialise_table(table: dict[str, PlaceholderEntry]) -> list[dict]:
    """What the model actually SEES for a table (§3.1's PLACEHOLDERS list):
    the placeholder token, its rendered value and its meaning -- never the
    raw, unformatted `value` (the model has no use for it, and the fewer
    numeral-shaped things in its context, the less there is to accidentally
    copy verbatim, N-D1). Sorted by name for determinism (T-PT)."""
    out: list[dict] = []
    for name in sorted(table):
        entry = table[name]
        if entry.value is None:
            continue
        out.append({
            "placeholder": f"{{{entry.cls}:{name}}}",
            "rendered": format_metric_value(entry.value, entry.unit),
            "meaning": entry.meaning,
        })
    return out


# ── `find` (finding-narration/1): one call per rule finding ────────────────


def build_finding_payload(finding: dict, *, skill, period: tuple[str, str]) -> tuple[dict, dict[str, PlaceholderEntry]]:
    test_id = finding.get("test_id")
    catalogue_row = _catalogue_row(skill, test_id)
    table = build_finding_table(finding, skill=skill, period=period)
    payload = {
        "finding_key": finding_key(finding),
        "title": finding.get("title"),
        "test_id": test_id,
        "test_name": catalogue_row.get("test_name"),
        "control_objective": catalogue_row.get("control_objective"),
        "severity": finding.get("severity"),
        "severity_rule": finding.get("severity_rule"),
        "analyst_set_severity": bool(finding.get("analyst_set_severity")),
        "placeholders": _serialise_table(table),
        "template_observation": finding.get("observation"),
        "template_recommendation": finding.get("recommendation"),
        "template_management_questions": list(finding.get("management_questions") or []),
    }
    return payload, table


# ── `find_synthesis` (finding-synthesis/1): one call, themes across every
# finding this run has ────────────────────────────────────────────────────


def build_synthesis_payload(findings: list[dict], *, skill) -> tuple[dict, dict[str, dict[str, PlaceholderEntry]]]:
    items = []
    tables: dict[str, dict[str, PlaceholderEntry]] = {}
    for finding in findings:
        key = finding_key(finding)
        table = build_finding_table(finding, skill=skill)
        tables[key] = table
        items.append({
            "key": key,
            "title": finding.get("title"),
            "test_id": finding.get("test_id"),
            "severity": finding.get("severity"),
            "severity_rule": finding.get("severity_rule"),
            "monetary_basis": finding.get("monetary_basis"),
            "placeholders": _serialise_table(table),
        })
    return {"findings": items}, tables


# ── `find_candidates` (finding-candidates/1): one call, skipped by the
# caller unless some test has exception_units > 0 (§4.1) ───────────────────


def build_candidates_payload(
    *, skill, state, metrics: dict[str, dict], findings: list[dict], decided_candidates: list[dict] = (),
) -> tuple[dict, dict[str, dict[str, PlaceholderEntry]]] | None:
    """Returns `None` when no plan test has any exception -- the data-shaped
    half of §4.1's skip condition (the config switch is the caller's own
    check, not this function's). Otherwise `(payload, tables)`, `tables`
    keyed by `test_id` -- each test's own metric table, for validating a
    proposed candidate that cites metrics from that test."""
    test_results_by_id = {t["test_id"]: t for t in (getattr(state, "test_results", None) or [])}
    catalogue = _catalogue_lookup(skill)
    catalogue_ids = set(catalogue)

    covering_keys_by_metric: dict[str, list[str]] = {}
    for finding in findings:
        key = finding_key(finding)
        for name in (finding.get("metrics_cited") or {}):
            covering_keys_by_metric.setdefault(name, []).append(key)

    tables: dict[str, dict[str, PlaceholderEntry]] = {}
    test_items: list[dict] = []
    any_exceptions = False

    for test in skill.plan.get("tests", []):
        if "not_testable" in test:
            continue
        test_id = test["test_id"]
        result = test_results_by_id.get(test_id)
        if result is None:
            continue
        exception_units = result.get("exception_units") or 0
        if exception_units > 0:
            any_exceptions = True

        catalogue_row = catalogue.get(_catalogue_id_for(test_id, catalogue_ids), {})
        metrics_spec = (test.get("params") or {}).get("metrics") or {}
        table: dict[str, PlaceholderEntry] = {}
        metric_items = []
        for name, spec in metrics_spec.items():
            row = metrics.get(name)
            if row is None:
                continue
            entry = _metric_entry(
                name, row, test_id=test_id, test_name=catalogue_row.get("test_name"),
                source_field=f"run_metrics.{name}",
            )
            if entry is None:
                continue
            table[name] = entry
            metric_items.append({
                "placeholder": f"{{{entry.cls}:{name}}}",
                "rendered": format_metric_value(entry.value, entry.unit),
                "kind": spec.get("kind"),
                "covering_finding_keys": sorted(covering_keys_by_metric.get(name, [])),
            })

        tables[test_id] = table
        test_items.append({
            "test_id": test_id,
            "name": catalogue_row.get("test_name"),
            "control_objective": catalogue_row.get("control_objective"),
            "status": result.get("status"),
            "exception_units": exception_units,
            "metrics": metric_items,
        })

    if not any_exceptions:
        return None

    payload = {
        "tests": test_items,
        "rule_findings": [
            {
                "key": finding_key(f), "title": f.get("title"),
                "metrics_cited": sorted((f.get("metrics_cited") or {})),
            }
            for f in findings
        ],
        "decided_candidates": [
            {"rule_id": c.get("rule_id"), "title": c.get("title"), "status": c.get("candidate_status")}
            for c in decided_candidates
        ],
    }
    return payload, tables


# ── `prioritise` (priority-rationale/1) / `act` (remediation/1): one call
# each, one item per already-ordered finding/candidate ─────────────────────


def build_priority_payload(items: list[dict], *, skill, period: tuple[str, str] | None = None) -> tuple[dict, dict[str, dict[str, PlaceholderEntry]]]:
    """`items`: this run's findings/candidates in Python's OWN priority
    order (§4.1: "ordered items ... key, severity, title, table") -- this
    function never reorders them."""
    out_items = []
    tables: dict[str, dict[str, PlaceholderEntry]] = {}
    for item in items:
        key = finding_key(item)
        table = build_finding_table(item, skill=skill, period=period)
        tables[key] = table
        out_items.append({
            "key": key, "severity": item.get("severity"), "title": item.get("title"),
            "placeholders": _serialise_table(table),
        })
    return {"items": out_items}, tables


def build_remediation_payload(items: list[dict], *, skill, period: tuple[str, str] | None = None) -> tuple[dict, dict[str, dict[str, PlaceholderEntry]]]:
    out_items = []
    tables: dict[str, dict[str, PlaceholderEntry]] = {}
    for item in items:
        key = finding_key(item)
        table = build_finding_table(item, skill=skill, period=period)
        tables[key] = table
        out_items.append({
            "key": key, "title": item.get("title"),
            "effective_recommendation": item.get("recommendation"),
            "placeholders": _serialise_table(table),
        })
    return {"items": out_items}, tables


# ── `export_summary` (exec-summary/1): one call, skipped on a clean run
# (G10 -- the caller checks `not findings`, this function returns None the
# same way `build_candidates_payload` signals a skip) ──────────────────────


def build_exec_summary_payload(
    state, findings: list[dict], metrics: dict[str, dict], *, catalogue_tests: list[dict], themes: list[dict] = (),
) -> tuple[dict, dict[str, PlaceholderEntry]] | None:
    if not findings:
        return None
    from orchestrator.narration.run_values import run_values

    table = run_values(state, findings, metrics, catalogue_tests=catalogue_tests)
    top5 = sorted(findings, key=lambda f: _SEVERITY_ORDER.get(f.get("severity"), 3))[:5]
    payload = {
        "placeholders": _serialise_table(table),
        "theme_titles": [t.get("title") for t in themes],
        "top_findings": [{"title": f.get("title"), "severity": f.get("severity")} for f in top5],
    }
    return payload, table


# ── `export_caption` (chart-captions/1): one call, one item per chart ──────


def build_caption_payload(charts: list[dict], metrics: dict[str, dict]) -> tuple[dict, dict[str, dict[str, PlaceholderEntry]]]:
    """`charts`: `[{"chart_id", "what_it_plots", "metric_names"}]` -- the
    caller's own description of what each chart shows; this function only
    resolves each named metric to its already-persisted `run_metrics` value."""
    items = []
    tables: dict[str, dict[str, PlaceholderEntry]] = {}
    for chart in charts:
        chart_id = chart["chart_id"]
        table: dict[str, PlaceholderEntry] = {}
        for name in chart.get("metric_names", []):
            row = metrics.get(name)
            if row is None:
                continue
            entry = _metric_entry(
                name, row, test_id=row.get("test_id"), test_name=None,
                source_field=f"run_metrics.{name}",
            )
            if entry is not None:
                table[name] = entry
        tables[chart_id] = table
        items.append({
            "chart_id": chart_id, "what_it_plots": chart.get("what_it_plots"),
            "placeholders": _serialise_table(table),
        })
    return {"charts": items}, tables


# ── `profile` (profile-narrative/1): one call, source row/null counts only
# -- never a column VALUE ────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, used: set[str]) -> str:
    base = _SLUG_RE.sub("_", text.strip().lower()).strip("_") or "col"
    if not base[0].isalpha():
        base = f"c_{base}"
    base = base[:50]
    slug, i = base, 2
    while slug in used:
        slug = f"{base}_{i}"
        i += 1
    used.add(slug)
    return slug


def build_profile_payload(state) -> tuple[dict, dict[str, PlaceholderEntry]]:
    """§3.2 "profile" row: per source `rows_<source>`, plus the 20 columns
    with the highest null counts ACROSS EVERY SOURCE COMBINED (a
    deliberate reading of "the 20 columns" as one bounded, run-wide list,
    not 20 per source -- otherwise a Skill with many sources could blow the
    payload out arbitrarily) as `nulls_<source>_<col_slug>`. Column NAMES
    appear (never a column's VALUES) -- `state.profile_result` itself
    (orchestrator.nodes.fieldwork.profile) already carries only row/null
    counts, so there is nothing further to mask here.

    `state.profile_result` has two shapes depending on `state.mode`
    (orchestrator.nodes.fieldwork.profile/_profile_explorer): a Playbook run
    stores `{source: {row_count, null_counts}}` directly; an Explorer run
    stores `{"kind": "explorer", "sources": {source: {row_count,
    null_counts, columns}}}` -- `sources` carries the same
    Playbook-compatible `row_count`/`null_counts` keys per source
    (orchestrator.explorer.profile.pandas_profile_columns/profile_source),
    it is just one level deeper. Reading `profile_result.items()` directly
    against the Explorer shape used to iterate `("kind", "explorer")` as a
    (source, info) pair and crash on `"explorer".get(...)` -- branch on
    `kind` first so both shapes reach the same per-source loop below."""
    profile_result = getattr(state, "profile_result", None) or {}
    if profile_result.get("kind") == "explorer":
        sources_by_name = profile_result.get("sources") or {}
    else:
        sources_by_name = profile_result

    table: dict[str, PlaceholderEntry] = {}
    for source, info in sources_by_name.items():
        name = f"rows_{source}"
        table[name] = PlaceholderEntry(
            name=name, unit="count", value=info.get("row_count"),
            source_field=f"profile_result.{source}.row_count", meaning=f"row count of source {source}",
        )

    used_slugs: set[str] = set()
    null_candidates: list[tuple[int, str, str, str]] = []
    for source, info in sources_by_name.items():
        for column, count in (info.get("null_counts") or {}).items():
            null_candidates.append((count, source, column, _slugify(column, used_slugs)))
    null_candidates.sort(key=lambda row: (-row[0], row[1], row[2]))

    for count, source, column, slug in null_candidates[:20]:
        name = f"nulls_{source}_{slug}"
        table[name] = PlaceholderEntry(
            name=name, unit="count", value=count,
            source_field=f"profile_result.{source}.null_counts[{column!r}]",
            meaning=f"null count of column {column!r} in source {source}",
        )

    payload = {"placeholders": _serialise_table(table)}
    return payload, table
