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
# Statuses at which this page stops polling (found-live cost review: every
# open /run/<id> tab was polling get_run every 3s forever, even while
# waiting on a gate only a click on THIS page can clear). Terminal statuses
# (nothing left to happen) plus the HITL gates and "interrupted" -- each of
# those already has its own button here (Confirm plan / Sign off findings /
# Resume) whose callback re-renders run-page-body directly on click, so
# nothing is lost by not polling while waiting for it. "queued" and the
# default "running" branch keep polling: those progress on their own.
_TERMINAL_STATUSES = {"completed", "failed", "awaiting_confirmation", "awaiting_signoff", "interrupted"}

# Independent review 2026-09-24 item 6: a "queued" run whose own queue_note
# says it was created by a DIFFERENT deployment (service._queue_affinity_note
# -- a code_revision mismatch means THIS deployment's executor will never
# claim it, ThreadExecutor's own admission check) is, in practice, almost
# always permanently stuck: the deployment that created it is the one whose
# redeploy replaced it. Polling this page forever costs a query every
# _POLL_MS for a status that will realistically never change from here.
# Independent of that: any status, queued or running, that has not changed
# in _MAX_IDLE_INTERVALS polls (30 minutes at the default 3s interval) stops
# polling too -- an abandoned browser tab must not poll forever.
_MAX_IDLE_INTERVALS = 600


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


def _signoff_text(run: dict) -> str:
    signoff = run.get("signoff")
    if not signoff:
        return "Not yet signed off."
    text = f"Signed off by {signoff['approver']} at {signoff['timestamp']}"
    if signoff.get("self_approved"):
        text += " (self-approved — segregation of duties not enforced)"
    # CLAUDE.md §11 "Paused runs across a code deploy" / independent review
    # 2026-09-24 gap #11: a run paused at sign-off may continue its export
    # under a later code revision than the one that computed its numbers --
    # recorded (never silent) so the difference is stated here, not only in
    # the export files. Absent for every run that exported under the same
    # revision it was created on, which is the common case.
    computed = run.get("computed_code_revision")
    exported = run.get("export_code_revision")
    if computed and exported and exported != computed:
        text += f" — computed under code revision {computed}, exported under {exported}"
    return text


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
                    "required before export.",
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
                   "— this is the control that matters for a defensible workpaper.",
                   className="sub"),
            html.Button("Sign off findings", id="run-signoff-open-btn", className="btn-generate",
                        style={"width": "auto", "padding": "10px 24px"}),
        ], className="panel", style={"marginTop": 16}))

    elif status == "queued":
        # P3 gate review item 4: run.get("queue_note") is None for an ordinary
        # queued run (waiting on the concurrency cap, CLAUDE.md §2.3 rule 3) and
        # set only when this deployment's executor will never claim it (a
        # different deployment's run, orchestrator.service._queue_affinity_note).
        blocks.append(html.Div([
            html.H3("Waiting to start", style={"margin": "0 0 6px"}),
            html.P(run.get("queue_note") or "Queued — waiting for an available run slot.",
                   className="sub"),
        ], className="panel", style={"marginTop": 16}))

    elif status == "interrupted":
        blocks.append(html.Div([
            html.H3("This run was interrupted", style={"margin": "0 0 6px"}),
            html.P(run.get("status_reason") or
                   "The container restarted mid-run — resume to continue from where it left off.",
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
            html.P(_signoff_text(run), className="sub"),
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


def _should_stop_polling(run: dict | None, n_intervals: int) -> bool:
    """The run-poll dcc.Interval's `disabled` decision (independent review
    2026-09-24 item 6), pulled out of the callback closure so it is directly
    unit-testable: terminal/gate statuses (nothing left to happen here), a
    queued run this deployment can never claim (its own queue_note says a
    different deployment created it), or this tab has simply been open too
    long (_MAX_IDLE_INTERVALS) -- whichever comes first."""
    status = run.get("status") if run else None
    if status in _TERMINAL_STATUSES:
        return True
    if status == "queued" and bool(run.get("queue_note")):
        return True
    if n_intervals >= _MAX_IDLE_INTERVALS:
        return True
    return False


def register_callbacks(app) -> None:

    @app.callback(
        Output("run-page-body", "children"),
        Output("run-poll", "disabled"),
        Input("run-poll", "n_intervals"),
        State("run-page-run-id", "data"),
    )
    def _poll(_n, run_id):
        run = adapters.get_run(run_id)
        return _render_body(run, run_id), _should_stop_polling(run, _n)

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
