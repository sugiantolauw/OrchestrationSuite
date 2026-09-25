"""The per-run placeholder table (P6 WP N5, docs/specs/P6_narration_design.md
§3.2's "exec summary" row, and the `run_*` names every other task's own
table may also draw on): `run_values(state, findings, metrics)` builds one
`{name: PlaceholderEntry}` table of run-wide figures, independent of any
single finding.

Two callers, at two different points in the run (§3.2):

  * `narrate` calls it with the rule findings only -- the provisional
    figures shown while the run is still `awaiting_signoff`;
  * `export` (via `finalise`) calls it with the FINAL finding set -- rule
    findings plus every accepted AI-proposed candidate -- so the exec
    summary's own numbers match the recomputed headline (G13), never the
    provisional narrate()-time count.

`run_exposure_headline` and `run_approved_not_spent_total` are never
recomputed here: CLAUDE.md non-negotiable 2 -- the model receives numbers
Python already fixed, and those two figures are already persisted
`run_metrics` rows (`orchestrator.exposure.compute_run_exposure`, written by
`prioritise`/`finalise`). This module only reads them through, unit and all.
Only the finding-count and test-count breakdowns are computed here, because
no node persists them as a `run_metrics` row of their own -- they are a
property of the finding LIST and `state.test_results`, recomputed fresh on
every call so `narrate` and `export` never need to agree in advance on what
"the finding count" means.

Gap-fix (independent review 2026-09-24, gap #5): `run_tests_total` /
`run_tests_with_exceptions` / `run_tests_not_testable` must be counted at
CATALOGUE grain (14 for SKILL-001), not over `state.test_results`'s own,
larger set of plan-grain primitive-instance sub-tests (21) -- see
`orchestrator.catalogue_counts`. `catalogue_tests` is a REQUIRED keyword,
not merely accepted: every caller (`orchestrator.narration.payloads.
build_exec_summary_payload`, `orchestrator.narration.runner.
narrate_exec_summary`, the `narrate` node, `orchestrator.service`'s "run"
narrative table) resolves it via `orchestrator.catalogue_counts.
catalogue_tests_for_skill(skill)` and passes it through explicitly, so a
future caller cannot silently fall back to the wrong grain by forgetting
the argument the way every caller here once did. An empty list is still a
legitimate, EXPLICIT choice (no Skill/plan in scope) -- it still falls back
to the plan-grain count below rather than raising, since a wrong grain is
still better than a missing figure; what changed is that nothing may reach
that fallback by omission any more."""

from __future__ import annotations

from orchestrator.catalogue_counts import catalogue_test_counts
from orchestrator.exposure import dominant_exposure_finding
from orchestrator.narration.placeholders import PlaceholderEntry

__all__ = ["run_values"]

_SEVERITIES = ("High", "Medium", "Low", "Indeterminate")


def _count_entry(name: str, value: int, meaning: str) -> PlaceholderEntry:
    return PlaceholderEntry(name=name, unit="count", value=value, source_field=f"computed:{name}", meaning=meaning)


def _money_passthrough_entry(name: str, metrics: dict[str, dict], meaning: str) -> PlaceholderEntry:
    # A run_metrics row this module never re-derives (see module docstring).
    # Absent (value=None) only before prioritise/finalise has run yet --
    # PlaceholderEntry(value=None) is still returned, never a fabricated
    # 0.0, so a premature reference to it is caught as N-V1 (a known
    # placeholder with no value), not silently rendered as "$0.00".
    row = metrics.get(name)
    value = row.get("value") if row is not None else None
    unit = row.get("unit") if row is not None else "AUD"
    return PlaceholderEntry(name=name, unit=unit, value=value, source_field=f"run_metrics.{name}", meaning=meaning)


def _date_entry(name: str, value: str | None, meaning: str) -> PlaceholderEntry:
    return PlaceholderEntry(name=name, unit="date", value=value, source_field="RunState.audit_period", meaning=meaning)


_BASIS_MEANING = {
    "spend": "'spend': the full amount is spend under review, not a confirmed loss",
    "excess": "'excess': only the portion over a threshold is at risk, not the whole transaction",
}


def _dominant_exposure_entries(
    findings: list[dict], headline_row: dict | None,
) -> dict[str, PlaceholderEntry]:
    """Independent narration-content review 2026-09-25: neither the exec
    summary nor the 'risk_and_exposure' chart caption previously had any way
    to say WHAT drives the amount-at-risk headline -- only the total itself
    was in either one's table, so a model asked to explain the figure had
    nothing to explain it with beyond restating it. `orchestrator.exposure.
    dominant_exposure_finding` picks the single largest headline-eligible
    contributor; this turns that finding into placeholder entries so a
    model may name it, its own amount, whether that amount is 'spend' under
    review or an 'excess' over a threshold (never a confirmed loss either
    way -- CLAUDE.md non-negotiable 2's ban on unhedged causal language), and
    its share of the headline. Returns {} when there is no dominant finding
    (a clean run, or every finding's basis is 'approved_not_spent'/'none') --
    the same "omit rather than fabricate" rule every other entry here
    follows (§3.2: "Rows whose value is None are omitted")."""
    dominant = dominant_exposure_finding(findings)
    if dominant is None:
        return {}
    finding_id = dominant.get("finding_id") or dominant.get("rule_id") or "?"
    amount = dominant["exposure_amount"]
    basis = dominant.get("monetary_basis")
    entries: dict[str, PlaceholderEntry] = {
        "run_exposure_dominant_test_id": PlaceholderEntry(
            name="run_exposure_dominant_test_id", unit="value", value=dominant.get("test_id"),
            source_field=f"findings[{finding_id}].test_id",
            meaning="the test id of the single finding contributing the most to the amount-at-risk headline",
        ),
        "run_exposure_dominant_title": PlaceholderEntry(
            name="run_exposure_dominant_title", unit="value", value=dominant.get("title"),
            source_field=f"findings[{finding_id}].title",
            meaning="the title of the single finding contributing the most to the amount-at-risk headline",
        ),
        "run_exposure_dominant_amount": PlaceholderEntry(
            name="run_exposure_dominant_amount", unit="AUD", value=amount,
            source_field=f"findings[{finding_id}].exposure_amount",
            meaning=(
                "that finding's own amount at risk, the largest of any headline-eligible finding "
                f"this run; {_BASIS_MEANING.get(basis, 'basis not recorded')}"
            ),
        ),
        "run_exposure_dominant_basis": PlaceholderEntry(
            name="run_exposure_dominant_basis", unit="value", value=basis,
            source_field=f"findings[{finding_id}].monetary_basis",
            meaning=_BASIS_MEANING.get(basis, "this finding's monetary basis"),
        ),
    }
    headline_value = headline_row.get("value") if headline_row else None
    if headline_value:
        entries["run_exposure_dominant_pct"] = PlaceholderEntry(
            name="run_exposure_dominant_pct", unit="%", value=round((amount / headline_value) * 100, 1),
            source_field="computed:run_exposure_dominant_pct",
            meaning="that finding's own amount at risk as a share of the run's headline",
        )
    return entries


def run_values(
    state, findings: list[dict], metrics: dict[str, dict], *, catalogue_tests: list[dict],
) -> dict[str, PlaceholderEntry]:
    """Builds the `run_*` table. `findings` is whichever finding LIST the
    caller currently has (rule findings, or rule findings plus accepted
    candidates); `metrics` is this run's persisted `run_metrics` dict
    (`persistence.get_run_metrics(run_id)`, {name: {value, unit,
    source_ref, test_id}}). `state` supplies `audit_period` and
    `test_results` -- a plain `RunState`, or any object exposing those two
    attributes (the fixture harnesses' minimal state stubs already do).
    `catalogue_tests` is REQUIRED (see module docstring): this run's
    Skill's catalogue.yaml `tests` list, normally from `orchestrator.
    catalogue_counts.catalogue_tests_for_skill(skill)` -- when non-empty,
    the test-count entries are computed at catalogue grain (`orchestrator.
    catalogue_counts`) rather than over the plan's own, larger set of
    sub-tests; pass `[]` explicitly (never omit the argument) for a caller
    with no Skill/plan in scope."""
    severity_counts = {s: 0 for s in _SEVERITIES}
    for f in findings:
        severity = f.get("severity")
        if severity in severity_counts:
            severity_counts[severity] += 1

    test_results = list(getattr(state, "test_results", None) or [])
    if catalogue_tests:
        counts = catalogue_test_counts(catalogue_tests, test_results)
        tests_total = counts["total"]
        tests_with_exceptions = counts["with_exceptions"]
        tests_not_testable = counts["not_testable"]
    else:
        tests_total = len(test_results)
        tests_with_exceptions = sum(1 for t in test_results if t.get("status") == "exception")
        tests_not_testable = sum(1 for t in test_results if t.get("status") == "not_testable")

    audit_period = getattr(state, "audit_period", None)
    period_start, period_end = tuple(audit_period) if audit_period else (None, None)

    table = {
        "run_finding_count": _count_entry(
            "run_finding_count", len(findings),
            "count of this run's findings (rule findings, plus at export every accepted "
            "AI-proposed candidate)",
        ),
        "run_high_count": _count_entry(
            "run_high_count", severity_counts["High"], "count of this run's High-severity findings",
        ),
        "run_medium_count": _count_entry(
            "run_medium_count", severity_counts["Medium"], "count of this run's Medium-severity findings",
        ),
        "run_low_count": _count_entry(
            "run_low_count", severity_counts["Low"], "count of this run's Low-severity findings",
        ),
        "run_indeterminate_count": _count_entry(
            "run_indeterminate_count", severity_counts["Indeterminate"],
            "count of this run's findings whose severity could not be determined (a severity "
            "rule's metric was missing)",
        ),
        "run_exposure_headline": _money_passthrough_entry(
            "run_exposure_headline", metrics,
            "the run's amount-at-risk headline: every distinct flagged transaction line counted "
            "once, at the largest amount any finding attributes to it",
        ),
        "run_approved_not_spent_total": _money_passthrough_entry(
            "run_approved_not_spent_total", metrics,
            "money approved but never actually spent -- excluded from the exposure headline, "
            "reported separately",
        ),
        "run_tests_total": _count_entry(
            "run_tests_total", tests_total, "count of this run's tests",
        ),
        "run_tests_with_exceptions": _count_entry(
            "run_tests_with_exceptions", tests_with_exceptions,
            "count of this run's tests with at least one exception",
        ),
        "run_tests_not_testable": _count_entry(
            "run_tests_not_testable", tests_not_testable,
            "count of this run's tests that could not be tested",
        ),
        "run_audit_period_start": _date_entry(
            "run_audit_period_start", period_start, "the audit period's start date",
        ),
        "run_audit_period_end": _date_entry(
            "run_audit_period_end", period_end, "the audit period's end date",
        ),
    }
    table.update(_dominant_exposure_entries(findings, metrics.get("run_exposure_headline")))
    return table
