"""Platform Management Actions page — wires the "Filter by Skill" /
"Risk level" / "Status" controls.

BUG-ACTIONS-1 (live test round 3, following BUG-TRACE-1's fix pattern
exactly): `management_actions_page()` (src/platform/pages.py) renders the
`actions-skill-filter`, `actions-risk-filter` and `actions-status-filter`
dcc.Dropdowns exactly as the prototype does (same ids, same options, same
text — CLAUDE.md §11 "UI is the prototype's, exactly"), but neither the
prototype nor this build ever wired a callback to them, so selecting a
value never filtered the actions table. This module does only that,
mirroring src/trace_page.py's own register_callbacks pattern (registered
alongside it from app/app.py).

The summary KPI row above the filters (Total actions / Open-Under review /
High risk / Total exposure) is left UNFILTERED, on purpose, for the same
reason as /runs (src/runs_page.py): the prototype computes it once, at page
load, from the full actions list, above the filter row. "Total exposure"
specifically is orchestrator.service.cross_run_totals' cross-run,
double-count-safe figure over every ELIGIBLE run (CLAUDE.md independent
review 2026-09-24 gap #6) — a deliberately global figure, never "whatever
happens to be currently displayed" for a transient dropdown selection.
Only the `actions-table-body` container below the filter row follows the
filter.

Reads go through adapters.list_management_actions() — the same single
batched call management_actions_page() itself already makes to build the
page and its filter dropdown options (orchestrator/service.py
list_management_actions) — filtering happens in Python over that one
fetch, never a second query per filter value and never one query per
action."""

from __future__ import annotations

from dash import Input, Output

from src.platform import adapters
from src.platform.components import action_row
from src.runs_page import _matches


def _rows_for_filter(skill: str | None, risk: str | None, status: str | None) -> list:
    actions = adapters.list_management_actions()
    filtered = [
        a for a in actions
        if _matches(a.get("skill_name"), skill)
        and _matches(a.get("risk"), risk)
        and _matches(a.get("status"), status)
    ]
    return [action_row(a) for a in filtered]


def register_callbacks(app) -> None:

    @app.callback(
        Output("actions-table-body", "children"),
        Input("actions-skill-filter", "value"),
        Input("actions-risk-filter", "value"),
        Input("actions-status-filter", "value"),
    )
    def _filter_actions(skill, risk, status):
        return _rows_for_filter(skill, risk, status)
