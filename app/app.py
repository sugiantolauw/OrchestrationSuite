"""AI Audit Analyst — connected App entry point.

Every data read goes through orchestrator.service (CLAUDE.md §2.1: the web
tier never executes a node; it only polls Delta and renders). There are no
module-level data globals here — `ctx` is the one process-wide handle
(the AppContext itself, not audit data), and every page/callback fetches
what it needs per request.
"""

from __future__ import annotations

import logging
import os
import sys

_LOG = logging.getLogger(__name__)

# Make both `app/` (for `import src...`, mirroring reference_app's layout)
# and the repo root (for `import orchestrator...`) importable regardless of
# the working directory this is launched from.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_APP_DIR)
for _path in (_REPO_ROOT, _APP_DIR):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

import dash  # noqa: E402
import dash_bootstrap_components as dbc  # noqa: E402
from dash import Input, Output, dcc, html  # noqa: E402
from flask import jsonify  # noqa: E402

from src import run_setup, run_status, trace_page, workspace_tne  # noqa: E402
from src.platform import adapters  # noqa: E402
from src.platform.pages import (  # noqa: E402
    audit_runs_page,
    management_actions_page,
    platform_header,
    platform_nav,
    platform_trace_page,
    skill_library_page,
    skill_methodology_page,
)

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    title="AI Audit Analyst",
    suppress_callback_exceptions=True,
)
server = app.server


def serve_layout():
    """A callable layout, not a value built once at import (Dash evaluates
    this per session). platform_header() reads the backend's health via
    adapters, so building it eagerly at import time would mean the process
    can't even start without a live backend — and would be exactly the kind
    of module-level, import-time data read CLAUDE.md §3 NN14 rules out."""
    return html.Div([
        dcc.Location(id="url", refresh=False),
        platform_header(),
        platform_nav(),
        # Home page selection state (src/run_setup.py): kept here, outside
        # page-content, so home_layout()'s own returned tree stays an exact
        # match of the prototype's landing_page() for
        # tests/test_layout_parity.py -- see that module's docstring.
        dcc.Store(id="selected-mode-store", data="playbook"),
        dcc.Store(id="selected-skill-store", data=None),
        # Explorer Mode (docs/specs/P6_P8_explorer_llm_design.md §12 D2): the
        # one hidden dcc.Store (this session's active Explorer run_id, if
        # any) and one hidden dcc.Interval the landing page needs. Both live
        # here, outside home_layout()'s own returned tree, for the same
        # reason the two Stores above do -- tests/test_layout_parity.py's
        # zero-diff comparison covers only what home_layout() itself
        # returns. The Interval starts disabled: CLAUDE.md §11's cost
        # incident means idle polling is never acceptable, so it is enabled
        # only while an Explorer run is actually in its plan phase
        # (src/run_setup.py's render_workflow_preview is the sole writer of
        # its `disabled` prop).
        dcc.Store(id="explorer-run-store", data=None),
        dcc.Interval(id="explorer-poll-interval", interval=3000, disabled=True),
        html.Div(id="page-content"),
    ], className="app-shell")


app.layout = serve_layout


@app.callback(
    Output("page-content", "children"),
    Input("url", "pathname"),
    Input("url", "search"),
)
def route_page(pathname, search):
    if pathname == "/skills":
        return skill_library_page()
    if pathname and pathname.startswith("/skills/"):
        skill_id = pathname.rsplit("/", 1)[-1]
        return skill_methodology_page(skill_id)
    if pathname == "/runs":
        return audit_runs_page()
    if pathname == "/trace":
        return platform_trace_page()
    if pathname == "/actions":
        return management_actions_page()
    if pathname and pathname.startswith("/run/"):
        run_id = pathname.rsplit("/", 1)[-1]
        return run_status.run_page(run_id)
    if pathname == "/workspace/tne":
        run_id = _parse_run_id(search)
        return workspace_tne.tne_workspace_layout(run_id)
    # Explorer Mode D5.1: the Skill Library's "Start Explorer Mode" button
    # navigates to "/?mode=explorer" -- home_layout()'s own default (no
    # argument, every other route to "/") stays exactly the prototype's
    # playbook-selected landing page, so tests/test_layout_parity.py's
    # zero-diff comparison is unaffected.
    default_mode = "explorer" if _parse_query_param(search, "mode") == "explorer" else "playbook"
    return run_setup.home_layout(default_mode=default_mode)


def _parse_run_id(search: str | None) -> str | None:
    return _parse_query_param(search, "run_id")


def _parse_query_param(search: str | None, name: str) -> str | None:
    if not search:
        return None
    from urllib.parse import parse_qs
    params = parse_qs(search.lstrip("?"))
    values = params.get(name)
    return values[0] if values else None


run_setup.register_callbacks(app)
run_status.register_callbacks(app)
workspace_tne.register_callbacks(app)
trace_page.register_callbacks(app)


@server.route("/health")
def health_route():
    return jsonify(adapters.service.health(adapters.get_context()))


@server.route("/ready")
def ready_route():
    payload = adapters.service.ready(adapters.get_context())
    status_code = 200 if payload.get("ready") else 503
    return jsonify(payload), status_code


_STARTED = False


def _start_executor_once() -> None:
    global _STARTED
    if _STARTED:
        return
    ctx = adapters.get_context()
    # Item 5 (CLAUDE.md §4.1, P2/P3 gate review): repair_projections() runs
    # BEFORE the reaper, same "called at App start" contract its own
    # docstring has always stated (persistence_delta.py/persistence_local.py
    # repair_projections) but that was never actually wired into a real App
    # start -- a `runs` row whose state_version lagged its `run_state` row
    # (a save whose projection update failed and was swallowed) stayed
    # stale forever, with nothing to notice or fix it. Runs first so the
    # reaper's own `runs`-table reads (find_runs) see corrected rows.
    ctx.persistence.repair_projections()
    # Reaper runs BEFORE the executor's admission loop starts (CLAUDE.md
    # §2.3 rule 2: "A reaper runs on App start... this is load-bearing").
    # reap_orphaned_runs_with_leases existed and was covered by
    # tests/test_p3_service.py's restart-survival test (which calls it by
    # hand, simulating what the App itself is supposed to do) but was never
    # actually wired into a real App start -- found live, item 11 of the P3
    # integration pass: after a genuine App restart, a run orphaned by the
    # old process stayed `running` forever with no Resume action offered,
    # because nothing had ever marked it `interrupted`.
    from orchestrator.executor import reap_orphaned_runs_with_leases

    # Same tracing/grace_s/exclude_run_ids semantics as the executor's own
    # periodic reap (orchestrator.executor.ThreadExecutor._reap_orphans):
    # `tracing=ctx.tracing` so an orphaned run's MLflow parent run is ended
    # KILLED here too, not just when the admission loop reaps it later;
    # `grace_s` matches ctx.executor's own configured lease-reap grace, so an
    # App-start reap does not use a stricter (0-grace) tolerance than the
    # very same executor's admission loop will use moments later; and
    # `exclude_run_ids` is empty because, at App start, this process has no
    # active runs of its own yet to exclude.
    reap_orphaned_runs_with_leases(
        ctx.persistence, now=ctx.clock(), tracing=ctx.tracing,
        exclude_run_ids=set(), grace_s=getattr(ctx.executor, "_lease_reap_grace_s", 0.0),
    )

    # Independent review 2026-09-24 item 5: warm the readiness cache at App
    # start and log (never raise on) anything that isn't ready -- a platform
    # outage at deploy time must not prevent the App itself from coming up
    # and serving /health; start_audit_run's own gate is what actually
    # blocks a run.
    if ctx.readiness is not None:
        try:
            report = ctx.readiness.get()
            blocking = report.blocking_failures()
            if blocking:
                _LOG.warning(
                    "App start: readiness check failing: %s",
                    [c.name for c in blocking],
                )
        except Exception:
            _LOG.exception("App start: readiness check raised")

    ctx.executor.start()
    _STARTED = True


if __name__ == "__main__":
    _start_executor_once()
    # Databricks Apps serve on DATABRICKS_APP_PORT; PORT is the local-dev
    # fallback (default when neither is set).
    port = int(os.environ.get("DATABRICKS_APP_PORT") or os.environ.get("PORT", 8050))
    # threaded=True: Werkzeug's dev server defaults to handling one request
    # at a time, which is fine for the background executor (its own threads,
    # unaffected either way) but not for the web tier -- a browser holding
    # one connection open (a slow download, a stalled request) must not
    # block every other user's poll/callback. Still not the "real" WSGI
    # server the startup banner warns about; that's a separate P9-scope item.
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
else:
    # Imported (e.g. under a WSGI server, or by tests that don't want the
    # executor running). Tests exercise routing/layout without starting it;
    # a real deploy always goes through the __main__ path above.
    pass
