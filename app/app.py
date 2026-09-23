"""AI Audit Analyst — connected App entry point.

Every data read goes through orchestrator.service (CLAUDE.md §2.1: the web
tier never executes a node; it only polls Delta and renders). There are no
module-level data globals here — `ctx` is the one process-wide handle
(the AppContext itself, not audit data), and every page/callback fetches
what it needs per request.
"""

from __future__ import annotations

import os
import sys

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

from src import run_setup, run_status, workspace_tne  # noqa: E402
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
    return run_setup.home_layout()


def _parse_run_id(search: str | None) -> str | None:
    if not search:
        return None
    from urllib.parse import parse_qs
    params = parse_qs(search.lstrip("?"))
    values = params.get("run_id")
    return values[0] if values else None


run_setup.register_callbacks(app)
run_status.register_callbacks(app)
workspace_tne.register_callbacks(app)


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
    ctx.executor.start()
    _STARTED = True


if __name__ == "__main__":
    _start_executor_once()
    # Databricks Apps serve on DATABRICKS_APP_PORT; PORT is the local-dev
    # fallback (default when neither is set).
    port = int(os.environ.get("DATABRICKS_APP_PORT") or os.environ.get("PORT", 8050))
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    # Imported (e.g. under a WSGI server, or by tests that don't want the
    # executor running). Tests exercise routing/layout without starting it;
    # a real deploy always goes through the __main__ path above.
    pass
