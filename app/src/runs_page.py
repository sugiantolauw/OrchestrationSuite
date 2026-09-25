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
per filter value and never one query per run.

CLAUDE.md §11 "Run cards on /runs (user decision, 2026-09-25)": this module
also wires the run_card's View, Export and Trace buttons (src/platform/
components.py), whose pattern-matching ids ({"type": "run-view-btn"/
"run-export-btn"/"run-trace-btn", "index": run_id}) this decision newly
authorises for Export/Trace (View already had one, unused).

- View navigates the page's existing dcc.Location (app.py's "url", id
  reused — no new visible element) to /workspace/tne?run_id=<id> for a run
  whose Skill has a dedicated workspace page and has reached a status with
  computed, trustworthy results, and to /run/<id> — the one page every run
  kind and status already renders — for every other run. See
  `_view_target`'s own docstring.
- Export streams that run's xlsx via adapters.get_export/dcc.send_bytes,
  the same call src/run_status.py's "Download Excel" button already makes,
  through one hidden dcc.Download (app.py's "runs-export-download", placed
  the same way app.py already places the Explorer Stores/Interval — outside
  any single page's own tree, so it adds nothing to audit_runs_page()'s own
  layout-parity comparison). CLAUDE.md §11 "Message line" (2026-09-25):
  before sign-off no xlsx export is recorded yet (orchestrator/service.py
  get_export raises FileNotFoundError) — that case, and any other export
  error (NN14 — never silent), now sets the "runs-export-error" Div
  (src/platform/pages.py, styled like /workspace/tne's own
  "tne-export-error") to a message instead of no-opping; a successful
  download clears it.
- Trace navigates to /trace?run_id=<id> — src/platform/pages.py's
  platform_trace_page() reads it (via app.py) to preselect its own existing
  "Filter by run" dropdown, so the table is filtered on arrival."""

from __future__ import annotations

from dash import ALL, Input, Output, ctx, dcc
from dash.exceptions import PreventUpdate

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


# The prototype's "Needs review" status option names no single run status;
# it means runs waiting on a person -- the two HITL gates (CLAUDE.md §2.4).
_NEEDS_REVIEW = "needs review"
_NEEDS_REVIEW_STATUSES = ("Awaiting Confirmation", "Awaiting Signoff")


def _status_matches(actual, wanted: str | None) -> bool:
    if wanted and wanted.casefold() == _NEEDS_REVIEW:
        return any(_matches(actual, s) for s in _NEEDS_REVIEW_STATUSES)
    return _matches(actual, wanted)


def _rows_for_filter(skill: str | None, status: str | None) -> list:
    runs = adapters.list_audit_runs()
    filtered = [
        r for r in runs
        if _matches(r.get("skill_name"), skill) and _status_matches(r.get("status"), status)
    ]
    return [run_card(r) for r in filtered]


# CLAUDE.md gap #6's own _ELIGIBLE_STATUSES_FOR_TOTALS (orchestrator/
# service.py): a run in either of these has already run every test and
# computed every number -- only export (Completed) or sign-off (Awaiting
# Signoff) is outstanding -- so its workspace numbers are trustworthy, not
# a run still in progress, queued, failed or interrupted.
_WORKSPACE_VIEWABLE_STATUSES = ("Awaiting Signoff", "Completed")


def _view_target(run: dict) -> str:
    """View (CLAUDE.md §11 "Run cards on /runs"): a run whose Skill has a
    dedicated workspace page (`has_workspace` — orchestrator/service.py's
    own generic "does this Skill directory have a workspace.py" check, true
    only for SKILL-001 today, never for an Explorer run — CLAUDE.md §6 D5:
    "/workspace/tne stays SKILL-001's") and has reached a status whose
    numbers are already fixed (`_WORKSPACE_VIEWABLE_STATUSES`) opens
    /workspace/tne?run_id=<id> — checked directly against a real
    awaiting_signoff run (tests/test_runs_page.py) rather than assumed, since
    the task asked whether workspace_tne.tne_workspace_layout gates on
    status: it does not (src/workspace_tne.py's own _load_bundle/
    get_run_payload build from whatever a run has persisted so far, with no
    status check at all), so both statuses route here rather than
    Awaiting Signoff falling back to /run/<id>. Every other run opens
    /run/<id> instead — the one page every run kind and status already
    renders."""
    if run.get("has_workspace") and run.get("status") in _WORKSPACE_VIEWABLE_STATUSES:
        return f"/workspace/tne?run_id={run['run_id']}"
    return f"/run/{run['run_id']}"


def _url_parts(target: str) -> tuple[str, str]:
    path, _, query = target.partition("?")
    return path, f"?{query}" if query else ""


def register_callbacks(app) -> None:

    # BUG-RUNVIEW-1 (live browser round 3): without prevent_initial_call,
    # this callback fired once on every /runs page load anyway (dcc.Dropdown
    # Inputs default to firing on mount even with value=None -- unlike
    # _view_run/_trace_run/_export_run below, which all already declare
    # prevent_initial_call=True), replacing the whole server-rendered
    # runs-list.children subtree with a second, functionally-identical
    # client-side render moments after paint. audit_runs_page() already
    # renders the correct unfiltered runs-list itself, so that extra round
    # trip served no purpose -- and it recreates every run-view-btn/
    # run-export-btn/run-trace-btn pattern-matching button as a fresh
    # element right after mount, the same "subtree gets replaced out from
    # under a click shortly after render" shape the sign-off race
    # (tests/e2e/test_connected_app.py's module docstring) was fixed
    # against once already. A click landing in that window updates a button
    # instance dash-renderer is about to treat as superseded, and the
    # pattern-matching callback's own request resolves against the
    # freshly-mounted (unclicked) set -- a 204 PreventUpdate with no
    # navigation, on every button in runs-list, not just View. Real
    # deployed-app latency (cross_run_totals, list_audit_runs on real Delta)
    # makes that window far wider than this suite's fast local backend ever
    # shows it. Fixed by never re-rendering runs-list on mount at all.
    @app.callback(
        Output("runs-list", "children"),
        Input("runs-skill-filter", "value"),
        Input("runs-status-filter", "value"),
        prevent_initial_call=True,
    )
    def _filter_runs(skill, status):
        return _rows_for_filter(skill, status)

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("url", "search", allow_duplicate=True),
        Input({"type": "run-view-btn", "index": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def _view_run(n_clicks_list):
        if not any(n_clicks_list) or not isinstance(ctx.triggered_id, dict):
            raise PreventUpdate
        run_id = ctx.triggered_id["index"]
        run = next((r for r in adapters.list_audit_runs() if r["run_id"] == run_id), None)
        if run is None:
            raise PreventUpdate
        return _url_parts(_view_target(run))

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("url", "search", allow_duplicate=True),
        Input({"type": "run-trace-btn", "index": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def _trace_run(n_clicks_list):
        if not any(n_clicks_list) or not isinstance(ctx.triggered_id, dict):
            raise PreventUpdate
        run_id = ctx.triggered_id["index"]
        return _url_parts(f"/trace?run_id={run_id}")

    @app.callback(
        Output("runs-export-download", "data"),
        Output("runs-export-error", "children"),
        Input({"type": "run-export-btn", "index": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def _export_run(n_clicks_list):
        if not any(n_clicks_list) or not isinstance(ctx.triggered_id, dict):
            raise PreventUpdate
        run_id = ctx.triggered_id["index"]
        try:
            filename, blob = adapters.get_export(run_id, "xlsx")
        except FileNotFoundError:
            # No xlsx export recorded yet (before sign-off). CLAUDE.md §11
            # "Message line": states plainly that sign-off is what is
            # missing, mirroring src/workspace_tne.py's own export-error
            # pattern (_export_pptx/_export_excel) rather than no-opping.
            return None, "This run must be signed off before its export is available."
        except Exception as exc:  # NN14 — never silent
            return None, f"Export is not available for this run ({exc})"
        return dcc.send_bytes(blob, filename), ""
