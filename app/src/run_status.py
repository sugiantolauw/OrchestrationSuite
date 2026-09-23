"""/run/<run_id> — polls Delta (via get_run) for a run's status and exposes
the two HITL gates (CLAUDE.md §2.4): plan confirmation (optional in
Playbook) and findings sign-off (mandatory in both modes, blocking export).

The page never executes a node. Every button here calls a single
orchestrator.service function (confirm_plan / sign_off / resume_run /
get_export) and lets the executor and the next poll do the rest.
"""

from __future__ import annotations

from dash import Input, Output, State, dcc, html
from dash.exceptions import PreventUpdate
from flask import request

from src.platform import adapters

_POLL_MS = 3000
_TERMINAL_STATUSES = {"completed", "failed"}


def _request_actor() -> str:
    """See run_setup.py's _request_owner -- same rule: "local-user" is a
    label the LOCAL backend alone may use; the deployed backend must block
    plan confirmation / sign-off / resume rather than record an unverified
    approver identity (CLAUDE.md §9A.1, P2/P3 gate review item 7)."""
    owner = request.headers.get("X-Forwarded-Email") or request.headers.get("X-Forwarded-User")
    if owner:
        return owner
    if adapters.is_local_backend():
        return "local-user"
    raise adapters.MissingIdentityHeader(
        "No verified identity header (X-Forwarded-Email / X-Forwarded-User) was present on "
        "this request. Refusing to record an action under an unverified identity."
    )


def _error_panel(exc: Exception) -> html.Div:
    return html.Div([
        html.H3("Action blocked", style={"margin": "0 0 6px", "color": "#b85042"}),
        html.P(f"{type(exc).__name__}: {exc}", className="sub"),
    ], className="panel", style={"marginTop": 16, "borderColor": "#b85042"})


def run_page(run_id: str) -> html.Div:
    return html.Div([
        dcc.Store(id="run-page-run-id", data=run_id),
        dcc.Interval(id="run-poll", interval=_POLL_MS, n_intervals=0),
        # A browser-native confirm popup, not a DOM subtree the 3s poll
        # renders: dcc.ConfirmDialog lives here, outside run-page-body, so
        # _poll's periodic re-render of run-page-body can never race with
        # (and silently close) an open confirmation the way a server-rendered
        # "confirm/cancel" panel living inside the polled subtree could --
        # that was the actual cause of the export stall this page's docstring
        # used to attribute to orchestrator/ (see git history): the confirm
        # button was part of run-page-body, so a poll response landing
        # between "Sign off findings" and the auditor's next click could
        # revert it to the bare button before the click ever registered.
        dcc.ConfirmDialog(
            id="run-signoff-confirm-dialog",
            message="Signing off records your identity and timestamp on the run and is "
                    "required before export (CLAUDE.md §2.4).",
        ),
        html.Div(id="run-page-body"),
    ], className="shell dashboard-shell")


def _progress_view(run: dict) -> html.Div:
    progress = run.get("progress") or {}
    total = progress.get("total_nodes")
    idx = progress.get("node_index")
    stage = progress.get("current_stage")
    if total:
        pct = int(100 * (idx or 0) / total)
        bar = html.Div(
            html.Div(style={"width": f"{pct}%", "height": 8, "background": "#1e2761", "borderRadius": 4}),
            style={"width": "100%", "height": 8, "background": "#e4e7ee", "borderRadius": 4, "marginBottom": 8},
        )
    else:
        bar = None
    return html.Div([
        bar,
        html.P(f"Stage: {stage}" if stage else "Waiting to start…", style={"fontSize": 13, "color": "#3b4150"}),
    ])


def _render_body(run: dict | None, run_id: str) -> html.Div:
    if run is None:
        return html.Div([
            html.H2("Run not found", className="page-title"),
            html.P(f"No run with id {run_id}.", className="page-subtitle"),
        ], className="page-header")

    status = run.get("status")
    label = run.get("status_label") or status

    header = html.Div([
        html.Span(run.get("run_id", run_id), className="chip mono"),
        html.Span(f"Skill: {run.get('skill_id', '—')}", className="chip"),
        html.Span(f"Status: {label}", className="chip"),
        html.Span(f"Owner: {run.get('run_owner', '—')}", className="chip"),
    ], className="chip-row", style={"marginBottom": 12})

    blocks = [header, _progress_view(run)]

    if status == "awaiting_confirmation":
        blocks.append(html.Div([
            html.H3("Proposed plan is ready for review", style={"margin": "0 0 6px"}),
            html.P("Confirm the plan to continue to execution.", className="sub"),
            html.Button("Confirm plan", id="run-confirm-plan-btn", className="btn-generate",
                        style={"width": "auto", "padding": "10px 24px"}),
        ], className="panel", style={"marginTop": 16}))

    elif status == "awaiting_signoff":
        findings = run.get("findings", [])
        n_high = sum(1 for f in findings if f.get("severity") == "High")
        blocks.append(html.Div([
            html.H3("Findings are ready for sign-off", style={"margin": "0 0 6px"}),
            html.P(f"{len(findings)} finding(s), {n_high} High. Sign-off is required before export "
                   "(CLAUDE.md §2.4) — this is the control that matters for a defensible workpaper.",
                   className="sub"),
            html.Button("Sign off findings", id="run-signoff-open-btn", className="btn-generate",
                        style={"width": "auto", "padding": "10px 24px"}),
        ], className="panel", style={"marginTop": 16}))

    elif status == "interrupted":
        blocks.append(html.Div([
            html.H3("This run was interrupted", style={"margin": "0 0 6px"}),
            html.P(run.get("status_reason") or
                   "The container restarted mid-run (CLAUDE.md §2.3) — resume to continue from where it left off.",
                   className="sub"),
            html.Button("Resume", id="run-resume-btn", className="btn-generate",
                        style={"width": "auto", "padding": "10px 24px"}),
        ], className="panel", style={"marginTop": 16}))

    elif status == "failed":
        blocks.append(html.Div([
            html.H3("Run failed", style={"margin": "0 0 6px", "color": "#b85042"}),
            html.P(run.get("status_reason") or "No reason recorded.", className="sub"),
        ], className="panel", style={"marginTop": 16}))

    elif status == "completed":
        blocks.append(html.Div([
            html.H3("Run complete", style={"margin": "0 0 6px"}),
            html.Div([
                dcc.Link("Open /workspace/tne", href=f"/workspace/tne?run_id={run_id}",
                         className="btn-generate", style={"display": "inline-block", "width": "auto", "padding": "10px 24px"}),
                html.Button("Download Excel", id="run-download-xlsx-btn", className="ghost",
                            style={"width": "auto", "padding": "10px 24px"}),
                dcc.Download(id="run-download-xlsx"),
            ], style={"display": "flex", "gap": 10, "marginTop": 8}),
        ], className="panel", style={"marginTop": 16}))

    else:
        blocks.append(html.Div(
            html.P("Running…", style={"color": "#6b7283"}), className="panel", style={"marginTop": 16},
        ))

    return html.Div(blocks)


def register_callbacks(app) -> None:

    @app.callback(
        Output("run-page-body", "children"),
        Output("run-poll", "disabled"),
        Input("run-poll", "n_intervals"),
        State("run-page-run-id", "data"),
    )
    def _poll(_n, run_id):
        run = adapters.get_run(run_id)
        disabled = bool(run and run.get("status") in _TERMINAL_STATUSES)
        return _render_body(run, run_id), disabled

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input("run-confirm-plan-btn", "n_clicks"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _confirm_plan(n_clicks, run_id):
        if not n_clicks:
            raise PreventUpdate
        try:
            adapters.confirm_plan(run_id, _request_actor())
        except adapters.MissingIdentityHeader as exc:
            return _error_panel(exc)
        return _render_body(adapters.get_run(run_id), run_id)

    # "Sign off findings" only opens the native confirm dialog (a single-
    # Input callback writing a single, ALWAYS-mounted component's own prop --
    # never run-page-body, so it cannot race with _poll at all). The actual
    # sign_off call happens in _confirm_signoff below, on the dialog's own
    # submit_n_clicks, once the auditor confirms in the browser popup.
    @app.callback(
        Output("run-signoff-confirm-dialog", "displayed"),
        Input("run-signoff-open-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def _open_signoff(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        return True

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input("run-signoff-confirm-dialog", "submit_n_clicks"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _confirm_signoff(submit_n_clicks, run_id):
        if not submit_n_clicks:
            raise PreventUpdate
        try:
            adapters.sign_off(run_id, _request_actor())
        except adapters.MissingIdentityHeader as exc:
            return _error_panel(exc)
        return _render_body(adapters.get_run(run_id), run_id)

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input("run-resume-btn", "n_clicks"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _resume(n_clicks, run_id):
        if not n_clicks:
            raise PreventUpdate
        try:
            adapters.resume_run(run_id, _request_actor())
        except adapters.MissingIdentityHeader as exc:
            return _error_panel(exc)
        return _render_body(adapters.get_run(run_id), run_id)

    @app.callback(
        Output("run-download-xlsx", "data"),
        Input("run-download-xlsx-btn", "n_clicks"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _download_xlsx(n_clicks, run_id):
        if not n_clicks:
            raise PreventUpdate
        filename, blob = adapters.get_export(run_id, "xlsx")
        return dcc.send_bytes(blob, filename)
