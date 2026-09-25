"""Platform Audit Runs page — wires the "Filter by Skill" / "Status" controls.

BUG-RUNS-1 (live test round 3, following BUG-TRACE-1's fix pattern exactly):
`audit_runs_page()` (src/platform/pages.py) renders the `runs-skill-filter`
and `runs-status-filter` dcc.Dropdowns exactly as the prototype does (same
ids, same options, same text — CLAUDE.md §11 "UI is the prototype's,
exactly"), but neither the prototype nor this build ever wired a callback
to them, so selecting a value never filtered the runs list. This module
does only that, mirroring src/trace_page.py's own register_callbacks
pattern (registered alongside it from app/app.py).

The summary KPI row above the filters (Total runs / Completed / High-risk
findings / Open actions) is left UNFILTERED, on purpose: the prototype
computes it once, at page load, from the full run list — it sits above the
filter row, not inside the filtered list container, and the prototype's own
layout (kpi row, then filter row, then runs-list) never implied the KPIs
should track a transient dropdown selection. "High-risk findings" in
particular is orchestrator.service.cross_run_totals' cross-run,
double-count-safe figure over every ELIGIBLE run (CLAUDE.md independent
review 2026-09-24 gap #6), which is deliberately not "whatever happens to
be currently displayed". Only the `runs-list` container below the filter
row follows the filter — exactly what BUG-TRACE-1 did for /trace's events
table.

Reads go through adapters.list_audit_runs() — the same single batched call
audit_runs_page() itself already makes to build the page and its filter
dropdown options (orchestrator/service.py list_runs, CLAUDE.md §2.3 rule
4) — filtering happens in Python over that one fetch, never a second query
per filter value and never one query per run."""

from __future__ import annotations

from dash import Input, Output

from src.platform import adapters
from src.platform.components import run_card


def _matches(actual, wanted: str | None) -> bool:
    """Dash's own dcc.Dropdown clears to `None` or `""` depending on how the
    clear was triggered — either means "no filter". A wanted value is
    compared case-insensitively: the actions-status-filter dropdown's own
    prototype-original option text ("Under review") differs only in casing
    from the status this build actually renders ("Under Review" — see
    orchestrator/service.py list_management_actions' `.title()`), a known,
    already-documented mismatch (CLAUDE.md NN14) that a case-sensitive
    comparison would silently defeat this whole filter over."""
    if not wanted:
        return True
    return str(actual or "").casefold() == wanted.casefold()


def _rows_for_filter(skill: str | None, status: str | None) -> list:
    runs = adapters.list_audit_runs()
    filtered = [
        r for r in runs
        if _matches(r.get("skill_name"), skill) and _matches(r.get("status"), status)
    ]
    return [run_card(r) for r in filtered]


def register_callbacks(app) -> None:

    @app.callback(
        Output("runs-list", "children"),
        Input("runs-skill-filter", "value"),
        Input("runs-status-filter", "value"),
    )
    def _filter_runs(skill, status):
        return _rows_for_filter(skill, status)
