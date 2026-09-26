"""/run/<run_id> — polls Delta (via get_run) for a run's status and exposes
the two HITL gates (CLAUDE.md §2.4): plan confirmation (optional in
Playbook) and findings sign-off (mandatory in both modes, blocking export).

The page never executes a node. Every button here calls a single
orchestrator.service function (confirm_plan / sign_off / resume_run /
get_export / decide_candidate / edit_narrative / regenerate_narration /
restart_stale_run) and lets the executor and the next poll do the rest.

On the awaiting_signoff block, this also renders the P6 narration review
(docs/specs/P6_narration_design.md §7 UI-1 to UI-4): a "Model-written text
for review" panel (rule findings' observation, themes, the exec summary,
each with an edit control), an "AI-proposed findings" panel (Accept/Reject,
a required severity or reason), and a Regenerate button. Sign-off is
refused while any AI-proposed finding is undecided (UI-3). None of it
decides a rule finding's existence, numbers or severity (CLAUDE.md §3 NN2)
-- it is the human review surface for what the model wrote.
"""

from __future__ import annotations

from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import request

from orchestrator.errors import (
    CandidateAlreadyDecided,
    CandidateNotFound,
    CandidateReasonRequired,
    CandidateSeverityRequired,
    CandidateSuperseded,
    CandidatesUndecided,
    NarrationDisabled,
    NarrationNodeUnavailable,
    NarrativeEditConflict,
    NarrativeEditNotAllowed,
    NarrativeEditRejected,
    NarrativeNotFound,
    NarrativeTargetNotFound,
    ReviewActionRefused,
    RunCodeRevisionStale,
    RunNotAwaitingSignoff,
)
from src import pending_runs
from src.platform import adapters
from src.platform.components import format_money_or_dash

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
    # UI-R5 (docs/specs/P7_mapping_authoring_design.md §3.8): "Prepared by P,
    # reviewed by R, signed off by A at T" once a signoff carries prepared_by
    # (the P7-gated path) -- a legacy signoff keeps its original sentence,
    # unchanged (§3.7 "Existing runs").
    if signoff.get("prepared_by") is not None:
        text = (
            f"Prepared by {signoff.get('prepared_by')}, reviewed by {signoff.get('reviewed_by')}, "
            f"signed off by {signoff['approver']} at {signoff['timestamp']}"
        )
    else:
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


def _run_inputs_lines(run_inputs: dict | None) -> list[str]:
    """UI-M1 (D-P7-2, docs/specs/P7_mapping_authoring_design.md §1.4): one
    line per declared column mapping, parameter and not_supplied source, in
    the exact shapes named there -- so a mandatory plan confirmation shows
    the auditor what they are confirming, never an invisible mapping."""
    run_inputs = run_inputs or {}
    lines: list[str] = []
    for source, columns in sorted((run_inputs.get("mappings") or {}).items()):
        pairs = ", ".join(f"{physical!r} used as {contract}" for contract, physical in sorted(columns.items()))
        lines.append(f"{source}: column {pairs}")
    for name, param in sorted((run_inputs.get("parameters") or {}).items()):
        provenance = param.get("provenance") or {}
        filename = param.get("path", "").rsplit("/", 1)[-1]
        sha_prefix = (param.get("sha256") or "")[:8]
        lines.append(
            f"{name}: {filename} (sha256 {sha_prefix}…, owner {provenance.get('owner')}, "
            f"as of {provenance.get('as_of')})"
        )
    for source, entry in sorted((run_inputs.get("not_supplied") or {}).items()):
        affected = ", ".join(entry.get("affected_tests") or [])
        suffix = f" — {affected} not testable" if affected else ""
        lines.append(f"{source} not supplied ({entry.get('reason')}){suffix}")
    return lines


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
        # UI-4 (docs/specs/P6_narration_design.md §7): the same
        # outside-the-polled-subtree pattern as the sign-off dialog above,
        # for the same reason (a 3s poll re-rendering run-page-body must
        # never race with, and silently close, an open confirmation).
        dcc.ConfirmDialog(
            id="run-regenerate-confirm-dialog",
            message="Regenerate rewrites the model-written prose only -- your accept/reject "
                    "decisions and edits are kept.",
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


# ── P6 narration review (docs/specs/P6_narration_design.md §7 UI-1/UI-2/
# UI-3/UI-4/UI-7): a "Model-written text for review" panel and an
# "AI-proposed findings" panel, shown only on the awaiting_signoff block.
# The model never decides a rule finding's existence, numbers or severity
# (CLAUDE.md §3 NN2) -- these panels are the human review surface for what
# it DID write: prose around numbers the pipeline already fixed, plus any
# candidate findings it proposed (accepted/rejected here, before sign-off).


def _narrative_label_chip(resolved: dict) -> html.Span:
    text = resolved.get("label") or (resolved.get("status") or "").replace("_", " ").title() or "—"
    return html.Span(text, className="chip", style={"fontSize": 10.5})


def _sources_line(resolved: dict) -> html.P:
    sources = resolved.get("sources") or []
    names = sorted({s["source_field"] for s in sources})
    text = "Numbers from: " + (", ".join(names) if names else "—")
    return html.P(text, className="sub mono", style={"fontSize": 11, "margin": "4px 0 0"})


def _edit_control(resolved: dict) -> html.Details | None:
    """UI-1: "Each paragraph gets an edit control." Only shown when a real
    `narratives` row backs this text (`narrative_id` is not None) --
    `service.edit_narrative` has nothing to edit otherwise (there is no row
    for `NarrativeNotFound` to name)."""
    narrative_id = resolved.get("narrative_id")
    if not narrative_id:
        return None
    text = resolved.get("text")
    is_list = isinstance(text, list)
    value = "\n\n".join(text) if is_list else (text or "")
    return html.Details([
        html.Summary("Edit", className="ghost", style={"fontSize": 11, "width": "auto", "display": "inline-block"}),
        html.Div([
            dcc.Textarea(
                id={"type": "narrative-edit-textarea", "index": narrative_id},
                value=value, style={"width": "100%", "minHeight": 80, "marginTop": 6, "fontSize": 12.5},
            ),
            html.Button(
                "Save edit",
                id={"type": "narrative-edit-save-btn", "index": narrative_id, "list": is_list},
                className="btn-generate", style={"width": "auto", "padding": "6px 16px", "marginTop": 6},
            ),
        ]),
    ], style={"marginTop": 4})


def _reviewed_item(title, chips: list, paragraphs, resolved: dict) -> html.Div:
    body = paragraphs if isinstance(paragraphs, list) else [paragraphs]
    return html.Div([
        html.Div(chips, className="chip-row", style={"display": "flex", "gap": 6, "marginBottom": 6, "flexWrap": "wrap"}),
        html.H3(title, style={"margin": "0 0 6px", "fontSize": 15}) if title else None,
        *[html.P(p, style={"margin": "0 0 6px", "fontSize": 13, "lineHeight": 1.5}) for p in body if p],
        _sources_line(resolved),
        _edit_control(resolved),
    ], className="panel", style={"marginTop": 10})


def _narration_review_panel(narration: dict) -> html.Div:
    children = [html.H3("Model-written text for review", style={"margin": "0 0 10px"})]

    if narration.get("degraded_label"):
        children.append(html.Div(
            html.Span(narration["degraded_label"], className="chip"), style={"marginBottom": 10},
        ))

    for f in narration.get("findings", []):
        children.append(_reviewed_item(
            f.get("title"),
            [
                html.Span(f.get("severity") or "—", className="chip"),
                _narrative_label_chip(f),
            ],
            f.get("text"), f,
        ))

    for theme in narration.get("themes", []):
        summary = theme["summary"]
        root_cause = theme.get("root_cause") or {}
        review_obs = (theme.get("review_observations") or {}).get("text") or []
        body = [summary.get("text")]
        if root_cause.get("text"):
            body.append(f"Root-cause hypothesis (for discussion): {root_cause['text']}")
        item = _reviewed_item(
            theme["title"].get("text"),
            [html.Span("Theme", className="chip"), _narrative_label_chip(summary)],
            body, summary,
        )
        if review_obs:
            item.children.append(html.Div([
                html.P("Review observations (not findings -- informational only, never exported):",
                       className="sub", style={"margin": "8px 0 2px", "fontWeight": 600}),
                html.Ul([html.Li(o, style={"fontSize": 12.5}) for o in review_obs]),
            ]))
        children.append(item)

    exec_summary = narration.get("exec_summary")
    if exec_summary and exec_summary.get("text"):
        children.append(_reviewed_item(
            "Executive summary",
            [html.Span("Exec summary", className="chip"), _narrative_label_chip(exec_summary)],
            exec_summary.get("text"), exec_summary,
        ))

    return html.Div(children, className="panel", style={"marginTop": 16})


def _candidate_item(candidate: dict) -> html.Div:
    status = candidate.get("candidate_status")
    header = [
        html.Span("AI-proposed", className="chip", style={"fontSize": 10.5}),
        html.Span(candidate.get("proposed_severity") or "—", className="chip", style={"fontSize": 10.5}),
    ]
    metrics = ", ".join(candidate.get("metrics_cited") or []) or "—"
    body = [
        html.Div(header, style={"display": "flex", "gap": 6, "marginBottom": 6}),
        html.H3(candidate.get("title") or candidate.get("rule_id"), style={"margin": "0 0 6px", "fontSize": 15}),
        html.P(candidate.get("observation_text") or "", style={"margin": "0 0 6px", "fontSize": 13, "lineHeight": 1.5}),
        html.P(
            "Numbers from: " + (
                ", ".join(sorted({s["source_field"] for s in candidate.get("observation_sources") or []})) or "—"
            ),
            className="sub mono", style={"fontSize": 11},
        ),
        html.P(f"Model-proposed severity: {candidate.get('proposed_severity') or '—'} "
               f"— {candidate.get('severity_reason') or 'no reason given'}",
               className="sub", style={"fontSize": 12}),
        html.P(f"Cites: {metrics}", className="sub mono", style={"fontSize": 11}),
        html.P(f"Own exposure: {format_money_or_dash(candidate.get('exposure_amount'))}",
               className="sub mono", style={"fontSize": 12}),
    ]

    candidate_id = candidate["candidate_id"]
    if status == "candidate":
        controls = html.Div([
            dcc.Dropdown(
                id={"type": "candidate-severity-dropdown", "index": candidate_id},
                options=[{"label": s, "value": s} for s in ("High", "Medium", "Low")],
                placeholder="Severity (required to accept)", clearable=True,
                style={"width": 240, "display": "inline-block", "marginRight": 8, "verticalAlign": "top"},
            ),
            dcc.Input(
                id={"type": "candidate-reason-input", "index": candidate_id}, type="text",
                placeholder="Reason (required to reject)",
                style={"width": 260, "marginRight": 8},
            ),
            html.Button("Accept", id={"type": "candidate-accept-btn", "index": candidate_id},
                        className="btn-generate", style={"width": "auto", "padding": "6px 16px", "marginRight": 6}),
            html.Button("Reject", id={"type": "candidate-reject-btn", "index": candidate_id},
                        className="ghost", style={"width": "auto", "padding": "6px 16px"}),
        ], style={"display": "flex", "alignItems": "center", "flexWrap": "wrap", "gap": 6, "marginTop": 8})
    elif status == "accepted":
        controls = html.P(
            f"Accepted by {candidate.get('decided_by') or '—'} at {candidate.get('decided_at') or '—'} "
            f"(severity: {candidate.get('decided_severity') or '—'})",
            className="sub", style={"marginTop": 8},
        )
    elif status == "rejected":
        controls = html.P(
            f"Rejected by {candidate.get('decided_by') or '—'}: {candidate.get('decision_reason') or '—'}",
            className="sub", style={"marginTop": 8},
        )
    else:
        controls = html.P("Superseded by a narration regeneration.", className="sub", style={"marginTop": 8})

    body.append(controls)
    return html.Div(body, className="panel", style={"marginTop": 10})


def _candidates_panel(narration: dict) -> html.Div:
    candidates = narration.get("candidates") or []
    children = [html.H3("AI-proposed findings", style={"margin": "0 0 10px"})]
    if not candidates:
        children.append(html.P("No AI-proposed findings for this run.", className="sub"))
    else:
        children.extend(_candidate_item(c) for c in candidates)
    return html.Div(children, className="panel", style={"marginTop": 16})


# ── P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.8):
# UI-R1 to UI-R6, all on /run/<id>'s awaiting_signoff block. Existing
# prototype pages change nothing (§3.8's own closing line).


def _review_stage_text(review: dict) -> str:
    """UI-R1: one P.sub line naming whose turn it is."""
    stage = review.get("stage", "preparation")
    if stage == "review":
        prepared = review.get("prepared") or {}
        return f"Prepared by {prepared.get('actor', '—')} at {prepared.get('at', '—')} — awaiting review"
    if stage == "approval":
        reviewed = review.get("reviewed") or {}
        return f"Reviewed by {reviewed.get('actor', '—')} at {reviewed.get('at', '—')} — awaiting sign-off"
    return "Review stage: preparation"


def _review_note_item(note: dict, findings_by_id: dict) -> html.Div:
    """UI-R4: a chip (Open/Responded/Cleared), the body, "Raised by X (role)
    at T · on finding <title>", a response line once responded, and
    Respond/Clear controls while still open."""
    if note.get("state") == "cleared":
        status_text = "Cleared"
    elif note.get("response"):
        status_text = "Responded"
    else:
        status_text = "Open"

    target = ""
    finding_id = note.get("finding_id")
    if finding_id:
        title = (findings_by_id.get(finding_id) or {}).get("title") or finding_id
        target = f" · on finding {title}"

    children = [
        html.Div([html.Span(status_text, className="chip", style={"fontSize": 10.5})],
                 style={"display": "flex", "gap": 6, "marginBottom": 6}),
        html.P(note.get("body"), style={"margin": "0 0 4px", "fontSize": 13, "lineHeight": 1.5}),
        html.P(f"Raised by {note.get('raised_by')} ({note.get('raised_role')}) at "
               f"{note.get('raised_at')}{target}", className="sub", style={"fontSize": 11.5}),
    ]
    if note.get("response"):
        children.append(html.P(
            f"Response by {note.get('responded_by')}: {note.get('response')}",
            className="sub", style={"fontSize": 11.5},
        ))
    if note.get("state") != "cleared":
        note_id = note["note_id"]
        children.append(html.Div([
            dcc.Input(
                id={"type": "review-note-response-input", "index": note_id}, type="text",
                placeholder="Response", style={"width": 260, "marginRight": 8},
            ),
            html.Button("Respond", id={"type": "review-note-respond-btn", "index": note_id},
                        className="ghost", style={"width": "auto", "padding": "6px 16px", "marginRight": 6}),
            html.Button("Clear", id={"type": "review-note-clear-btn", "index": note_id},
                        className="ghost", style={"width": "auto", "padding": "6px 16px"}),
        ], style={"display": "flex", "alignItems": "center", "flexWrap": "wrap", "gap": 6, "marginTop": 8}))
    return html.Div(children, className="panel", style={"marginTop": 10})


def _review_notes_panel(review_notes: list[dict], findings: list[dict], stage: str) -> html.Div:
    """UI-R4: the "Review notes" panel, plus a raise row (a finding dropdown
    with "Whole run", a body input and a "Raise note" button) shown only
    during review/approval -- notes may only be raised then (§3.3)."""
    findings_by_id = {f["finding_id"]: f for f in findings}
    children = [html.H3("Review notes", style={"margin": "0 0 10px"})]
    if not review_notes:
        children.append(html.P("No review notes on this run.", className="sub"))
    else:
        children.extend(_review_note_item(n, findings_by_id) for n in review_notes)
    if stage in ("review", "approval"):
        options = [{"label": "Whole run", "value": "__run__"}] + [
            {"label": f.get("title") or f["finding_id"], "value": f["finding_id"]} for f in findings
        ]
        children.append(html.Div([
            dcc.Dropdown(
                id="review-note-raise-target", options=options, value="__run__", clearable=False,
                style={"width": 240, "display": "inline-block", "marginRight": 8, "verticalAlign": "top"},
            ),
            dcc.Input(id="review-note-raise-body", type="text", placeholder="Note",
                      style={"width": 300, "marginRight": 8}),
            html.Button("Raise note", id="review-note-raise-btn", className="ghost",
                        style={"width": "auto", "padding": "6px 16px"}),
        ], style={"display": "flex", "alignItems": "center", "flexWrap": "wrap", "gap": 6, "marginTop": 12}))
    return html.Div(children, className="panel", style={"marginTop": 16})


def _render_body(run: dict | None, run_id: str, narration: dict | None = None) -> html.Div:
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
        confirm_children = [
            html.H3("Proposed plan is ready for review", style={"margin": "0 0 6px"}),
            html.P("Confirm the plan to continue to execution.", className="sub"),
        ]
        run_inputs_lines = _run_inputs_lines(run.get("options", {}).get("run_inputs"))
        if run_inputs_lines:
            # UI-M1 (D-P7-2, docs/specs/P7_mapping_authoring_design.md
            # §1.4): a mandatory confirmation of an invisible mapping is not
            # a control -- every declared column mapping, parameter and
            # not_supplied source is listed below the existing "sub"
            # sentence, so the auditor confirms what they can see.
            confirm_children.append(html.P(
                "This run uses project-specific inputs, so the plan must be confirmed.",
                className="sub",
            ))
            confirm_children.append(html.Ul([html.Li(line) for line in run_inputs_lines]))
        confirm_children.append(
            html.Button("Confirm plan", id="run-confirm-plan-btn", className="btn-generate",
                        style={"width": "auto", "padding": "10px 24px"}),
        )
        blocks.append(html.Div(confirm_children, className="panel", style={"marginTop": 16}))

    elif status == "awaiting_signoff":
        findings = run.get("findings", [])
        n_high = sum(1 for f in findings if f.get("severity") == "High")
        # UI-R1/R2: review is None for a run that has not yet been touched by
        # this page (it defaults to 'preparation', exactly the stage
        # orchestrator.runs.prepare/mark_reviewed/sign_off themselves default
        # an absent RunState.review to -- CLAUDE.md §11 "Runs waiting at
        # sign-off when this ships enter the workflow at preparation").
        review = run.get("review") or {}
        stage = review.get("stage", "preparation")
        if stage == "preparation":
            action_button = html.Button(
                "Mark as prepared", id="run-mark-prepared-btn", className="btn-generate",
                style={"width": "auto", "padding": "10px 24px"},
            )
        elif stage == "review":
            action_button = html.Button(
                "Mark as reviewed", id="run-mark-reviewed-btn", className="btn-generate",
                style={"width": "auto", "padding": "10px 24px"},
            )
        else:
            action_button = html.Button(
                "Sign off findings", id="run-signoff-open-btn", className="btn-generate",
                style={"width": "auto", "padding": "10px 24px"},
            )

        panel_children = [
            html.H3("Findings are ready for sign-off", style={"margin": "0 0 6px"}),
            html.P(f"{len(findings)} finding(s), {n_high} High. Sign-off is required before export "
                   "— this is the control that matters for a defensible workpaper.",
                   className="sub"),
            html.P(_review_stage_text(review), className="sub"),
            action_button,
        ]
        if stage in ("review", "approval"):
            # UI-R3: a reviewer (stage 'review') or approver (stage
            # 'approval') may send the run back to the preparer.
            panel_children.append(html.Div([
                dcc.Input(id="run-return-reason-input", type="text", placeholder="Reason for returning",
                          style={"width": 300, "marginRight": 8}),
                html.Button("Return to preparer", id="run-return-btn", className="ghost",
                            style={"width": "auto", "padding": "10px 24px"}),
            ], style={"display": "flex", "alignItems": "center", "flexWrap": "wrap", "gap": 6, "marginTop": 10}))
        blocks.append(html.Div(panel_children, className="panel", style={"marginTop": 16}))

        if narration is not None:
            blocks.append(_narration_review_panel(narration))
            blocks.append(_candidates_panel(narration))
            blocks.append(html.Div([
                html.Button("Regenerate narration", id="run-regenerate-btn", className="ghost",
                            style={"width": "auto", "padding": "10px 24px"}),
            ], className="panel", style={"marginTop": 16}))

        # UI-R4: shown on every awaiting_signoff render, regardless of
        # whether narration is on -- review notes are a P7 feature, not a P6 one.
        blocks.append(_review_notes_panel(adapters.get_review_notes(run_id), findings, stage))

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


def _pending_run_view(run_id: str) -> dict | None:
    """CLAUDE.md §11 "Run start opens the run page at once" (2026-09-25):
    while the `runs` row src/run_setup.py's async-start worker is writing
    does not exist yet, this reads pending_runs' own in-process registry
    (never Delta -- nothing here costs a query) and renders EXACTLY what a
    real run already shows for the matching status, no new element or text:
    "pending" -> the same dict shape a real "queued" run has, so
    _render_body's existing queued block renders; "failed" -> the same
    shape a real "failed" run has, so _render_body's existing failed block
    (NN14: loud, visible -- the exception message, verbatim) renders. No
    registry entry at all (never registered, already completed then pruned,
    or the App restarted before the row was written) returns None, same as
    adapters.get_run for an unknown id -- the page's existing "Run not
    found" behaviour."""
    entry = pending_runs.status(run_id)
    if entry is None:
        return None
    state, error = entry
    if state == "failed":
        return {
            "run_id": run_id, "status": "failed", "status_label": "Failed",
            "status_reason": error or "No reason recorded.",
        }
    return {"run_id": run_id, "status": "queued", "status_label": "Queued", "queue_note": None}


def _run_and_narration(run_id: str) -> tuple[dict | None, dict | None]:
    """UI-1/UI-2: the narration review is only fetched (and only rendered)
    while a run sits at the one gate it applies to -- awaiting_signoff.
    Refetched after every action below so the panel a click just acted on
    (accept/reject/edit/regenerate) reflects that action immediately, the
    same "re-render from a fresh get_run" pattern every other button on
    this page already follows.

    adapters.get_run_and_narration loads this run's state once and reuses
    it for both (P3/P4 perf gap review 2026-09-25) -- calling adapters.get_run
    and adapters.get_narration_review here separately, as this used to,
    loaded it twice per render.

    When the `runs` row does not exist yet, falls back to
    _pending_run_view -- see its own docstring."""
    run, narration = adapters.get_run_and_narration(run_id)
    if run is None:
        run = _pending_run_view(run_id)
    return run, narration


def _refresh(run_id: str) -> html.Div:
    run, narration = _run_and_narration(run_id)
    return _render_body(run, run_id, narration)


def _refresh_from_state(run_id: str, state) -> html.Div:
    """BUG-PERF-2 (P3/P4 perf gap review 2026-09-25): confirm_plan/sign_off/
    resume_run/regenerate_narration each already return the RunState their
    own write just produced -- use it directly (adapters.
    get_run_and_narration_from_state) instead of `_refresh`'s fresh
    `persistence.load_state`, which reloads the exact same row this click's
    own write already has in hand."""
    run, narration = adapters.get_run_and_narration_from_state(run_id, state)
    return _render_body(run, run_id, narration)


def _stale_run_panel(exc: RunCodeRevisionStale) -> html.Div:
    return html.Div([
        _error_panel(exc),
        html.Div(
            html.Button("Start a fresh run", id="run-restart-stale-btn", className="btn-generate",
                        style={"width": "auto", "padding": "10px 24px"}),
            className="panel", style={"marginTop": 12},
        ),
    ])


def register_callbacks(app) -> None:

    @app.callback(
        Output("run-page-body", "children"),
        Output("run-poll", "disabled"),
        Input("run-poll", "n_intervals"),
        State("run-page-run-id", "data"),
    )
    def _poll(_n, run_id):
        run, narration = _run_and_narration(run_id)
        return _render_body(run, run_id, narration), _should_stop_polling(run, _n)

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
            state = adapters.confirm_plan(run_id, _request_actor())
        except adapters.MissingIdentityHeader as exc:
            return _error_panel(exc)
        except RunCodeRevisionStale as exc:
            # CLAUDE.md §11 "Paused runs across a code deploy" (independent
            # review 2026-09-24 gap #11): a run paused before its execute
            # phase completed cannot continue on new code -- one click
            # starts a fresh run with the same parameters (_restart_stale
            # below), never a silent run under a different setup.
            return _stale_run_panel(exc)
        return _refresh_from_state(run_id, state)

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
            state = adapters.sign_off(run_id, _request_actor())
        except (adapters.MissingIdentityHeader, CandidatesUndecided, ReviewActionRefused) as exc:
            # UI-3: CandidatesUndecided's own message is exactly "decide
            # every AI-proposed finding before sign-off" (orchestrator/
            # errors.py); UI-R6: ReviewActionRefused's own `reason` is one of
            # the exact strings the UI names -- both rendered verbatim, never
            # re-worded here.
            return _error_panel(exc)
        return _refresh_from_state(run_id, state)

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
            state = adapters.resume_run(run_id, _request_actor())
        except adapters.MissingIdentityHeader as exc:
            return _error_panel(exc)
        return _refresh_from_state(run_id, state)

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

    # ── UI-2: AI-proposed findings — Accept / Reject ─────────────────────

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input({"type": "candidate-accept-btn", "index": ALL}, "n_clicks"),
        State({"type": "candidate-severity-dropdown", "index": ALL}, "value"),
        State({"type": "candidate-severity-dropdown", "index": ALL}, "id"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _accept_candidate(n_clicks_list, severities, severity_ids, run_id):
        if not any(n for n in n_clicks_list if n) or not ctx.triggered_id:
            raise PreventUpdate
        candidate_id = ctx.triggered_id["index"]
        severity = next((v for v, i in zip(severities, severity_ids) if i["index"] == candidate_id), None)
        try:
            adapters.decide_candidate(
                run_id, candidate_id, decision="accepted", reason=None,
                decided_severity=severity, actor=_request_actor(),
            )
        except (adapters.MissingIdentityHeader, CandidateSeverityRequired, CandidateAlreadyDecided,
                CandidateSuperseded, CandidateNotFound, RunNotAwaitingSignoff, ReviewActionRefused) as exc:
            return _error_panel(exc)
        return _refresh(run_id)

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input({"type": "candidate-reject-btn", "index": ALL}, "n_clicks"),
        State({"type": "candidate-reason-input", "index": ALL}, "value"),
        State({"type": "candidate-reason-input", "index": ALL}, "id"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _reject_candidate(n_clicks_list, reasons, reason_ids, run_id):
        if not any(n for n in n_clicks_list if n) or not ctx.triggered_id:
            raise PreventUpdate
        candidate_id = ctx.triggered_id["index"]
        reason = next((v for v, i in zip(reasons, reason_ids) if i["index"] == candidate_id), None)
        try:
            adapters.decide_candidate(
                run_id, candidate_id, decision="rejected", reason=reason,
                decided_severity=None, actor=_request_actor(),
            )
        except (adapters.MissingIdentityHeader, CandidateReasonRequired, CandidateAlreadyDecided,
                CandidateSuperseded, CandidateNotFound, RunNotAwaitingSignoff, ReviewActionRefused) as exc:
            return _error_panel(exc)
        return _refresh(run_id)

    # ── UI-1: narrative edit ──────────────────────────────────────────────

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input({"type": "narrative-edit-save-btn", "index": ALL, "list": ALL}, "n_clicks"),
        State({"type": "narrative-edit-textarea", "index": ALL}, "value"),
        State({"type": "narrative-edit-textarea", "index": ALL}, "id"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _save_narrative_edit(n_clicks_list, values, value_ids, run_id):
        if not any(n for n in n_clicks_list if n) or not ctx.triggered_id:
            raise PreventUpdate
        narrative_id = ctx.triggered_id["index"]
        is_list = ctx.triggered_id["list"]
        value = next((v for v, i in zip(values, value_ids) if i["index"] == narrative_id), None)
        new_text = [p.strip() for p in (value or "").split("\n\n") if p.strip()] if is_list else (value or "")
        try:
            adapters.edit_narrative(run_id, narrative_id, new_text, _request_actor())
        except (adapters.MissingIdentityHeader, NarrativeEditRejected, NarrativeEditNotAllowed,
                NarrativeEditConflict, NarrativeNotFound, NarrativeTargetNotFound,
                ReviewActionRefused) as exc:
            # UI-1: NarrativeEditRejected's own message names the mismatched
            # number (orchestrator/errors.py's own N-H1 violation text);
            # UI-R6: ReviewActionRefused's "ask the reviewer to return the
            # run" text (D-P7-10) -- both rendered verbatim.
            return _error_panel(exc)
        return _refresh(run_id)

    # ── UI-4: Regenerate narration ───────────────────────────────────────

    @app.callback(
        Output("run-regenerate-confirm-dialog", "displayed"),
        Input("run-regenerate-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def _open_regenerate(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        return True

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input("run-regenerate-confirm-dialog", "submit_n_clicks"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _confirm_regenerate(submit_n_clicks, run_id):
        if not submit_n_clicks:
            raise PreventUpdate
        try:
            state = adapters.regenerate_narration(run_id, _request_actor())
        except (adapters.MissingIdentityHeader, NarrationDisabled, NarrationNodeUnavailable,
                RunNotAwaitingSignoff, ReviewActionRefused) as exc:
            return _error_panel(exc)
        return _refresh_from_state(run_id, state)

    # ── P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.8:
    # UI-R2/R3/R4) ────────────────────────────────────────────────────────

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input("run-mark-prepared-btn", "n_clicks"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _mark_prepared(n_clicks, run_id):
        if not n_clicks:
            raise PreventUpdate
        try:
            state = adapters.prepare_findings(run_id, _request_actor())
        except (adapters.MissingIdentityHeader, CandidatesUndecided, ReviewActionRefused,
                RunNotAwaitingSignoff) as exc:
            # UI-R6: ReviewActionRefused's own `reason` is one of the exact
            # strings the UI names (role, SoD, open notes, stage) --
            # _error_panel renders it verbatim, never re-worded here.
            return _error_panel(exc)
        return _refresh_from_state(run_id, state)

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input("run-mark-reviewed-btn", "n_clicks"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _mark_reviewed(n_clicks, run_id):
        if not n_clicks:
            raise PreventUpdate
        try:
            state = adapters.mark_reviewed(run_id, _request_actor())
        except (adapters.MissingIdentityHeader, ReviewActionRefused, RunNotAwaitingSignoff) as exc:
            return _error_panel(exc)
        return _refresh_from_state(run_id, state)

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input("run-return-btn", "n_clicks"),
        State("run-return-reason-input", "value"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _return_to_preparer(n_clicks, reason, run_id):
        if not n_clicks:
            raise PreventUpdate
        try:
            state = adapters.return_to_preparer(run_id, _request_actor(), reason)
        except (adapters.MissingIdentityHeader, ReviewActionRefused, RunNotAwaitingSignoff) as exc:
            return _error_panel(exc)
        return _refresh_from_state(run_id, state)

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input("review-note-raise-btn", "n_clicks"),
        State("review-note-raise-target", "value"),
        State("review-note-raise-body", "value"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _raise_note(n_clicks, target, body, run_id):
        if not n_clicks:
            raise PreventUpdate
        finding_id = None if not target or target == "__run__" else target
        try:
            adapters.raise_review_note(run_id, _request_actor(), body or "", finding_id)
        except (adapters.MissingIdentityHeader, ReviewActionRefused, RunNotAwaitingSignoff) as exc:
            return _error_panel(exc)
        return _refresh(run_id)

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input({"type": "review-note-respond-btn", "index": ALL}, "n_clicks"),
        State({"type": "review-note-response-input", "index": ALL}, "value"),
        State({"type": "review-note-response-input", "index": ALL}, "id"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _respond_note(n_clicks_list, responses, response_ids, run_id):
        if not any(n for n in n_clicks_list if n) or not ctx.triggered_id:
            raise PreventUpdate
        note_id = ctx.triggered_id["index"]
        response = next((v for v, i in zip(responses, response_ids) if i["index"] == note_id), None)
        try:
            adapters.respond_to_review_note(run_id, note_id, _request_actor(), response or "")
        except (adapters.MissingIdentityHeader, ReviewActionRefused, RunNotAwaitingSignoff) as exc:
            return _error_panel(exc)
        return _refresh(run_id)

    @app.callback(
        Output("run-page-body", "children", allow_duplicate=True),
        Input({"type": "review-note-clear-btn", "index": ALL}, "n_clicks"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _clear_note(n_clicks_list, run_id):
        if not any(n for n in n_clicks_list if n) or not ctx.triggered_id:
            raise PreventUpdate
        note_id = ctx.triggered_id["index"]
        try:
            adapters.clear_review_note(run_id, note_id, _request_actor())
        except (adapters.MissingIdentityHeader, ReviewActionRefused, RunNotAwaitingSignoff) as exc:
            return _error_panel(exc)
        return _refresh(run_id)

    # ── Stale-confirm restart ─────────────────────────────────────────────

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("run-page-body", "children", allow_duplicate=True),
        Input("run-restart-stale-btn", "n_clicks"),
        State("run-page-run-id", "data"),
        prevent_initial_call=True,
    )
    def _restart_stale(n_clicks, run_id):
        if not n_clicks:
            raise PreventUpdate
        try:
            new_run_id = adapters.restart_stale_run(run_id, _request_actor())
        except adapters.MissingIdentityHeader as exc:
            return no_update, _error_panel(exc)
        return f"/run/{new_run_id}", no_update
