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
"the finding count" means."""

from __future__ import annotations

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


def run_values(state, findings: list[dict], metrics: dict[str, dict]) -> dict[str, PlaceholderEntry]:
    """Builds the `run_*` table. `findings` is whichever finding LIST the
    caller currently has (rule findings, or rule findings plus accepted
    candidates); `metrics` is this run's persisted `run_metrics` dict
    (`persistence.get_run_metrics(run_id)`, {name: {value, unit,
    source_ref, test_id}}). `state` supplies `audit_period` and
    `test_results` -- a plain `RunState`, or any object exposing those two
    attributes (the fixture harnesses' minimal state stubs already do)."""
    severity_counts = {s: 0 for s in _SEVERITIES}
    for f in findings:
        severity = f.get("severity")
        if severity in severity_counts:
            severity_counts[severity] += 1

    test_results = list(getattr(state, "test_results", None) or [])
    tests_total = len(test_results)
    tests_with_exceptions = sum(1 for t in test_results if t.get("status") == "exception")
    tests_not_testable = sum(1 for t in test_results if t.get("status") == "not_testable")

    audit_period = getattr(state, "audit_period", None)
    period_start, period_end = tuple(audit_period) if audit_period else (None, None)

    return {
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
            "run_tests_total", tests_total, "count of this run's plan tests",
        ),
        "run_tests_with_exceptions": _count_entry(
            "run_tests_with_exceptions", tests_with_exceptions,
            "count of this run's plan tests with at least one exception",
        ),
        "run_tests_not_testable": _count_entry(
            "run_tests_not_testable", tests_not_testable,
            "count of this run's plan tests that could not be tested",
        ),
        "run_audit_period_start": _date_entry(
            "run_audit_period_start", period_start, "the audit period's start date",
        ),
        "run_audit_period_end": _date_entry(
            "run_audit_period_end", period_end, "the audit period's end date",
        ),
    }
