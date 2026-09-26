"""Home — the landing page ("Start an audit analysis"). Reproduces the
prototype's `landing_page()` layout (reference_app/src/platform/pages.py)
and its four landing-page callbacks (reference_app/app.py:
search_data_assets, render_workflow_preview, render_run_summary,
start_demo_run) element-for-element -- same ids, classes, text, default
values -- wired to the real backend (orchestrator.service via
src.platform.adapters) instead of the prototype's fixture adapters.

Two places the prototype's own behaviour could not honestly carry over
unchanged are called out inline below (search "DEVIATION") and in this
change's report: the Start button's destination, and the upload panel's
literal destination-path template. Nothing else in this file's DOM differs
from the prototype's landing_page().

The mode cards and skill cards are the prototype's own components
(skill_card/mode_card in src/platform/components.py) rendered with their
existing `selected` styling and, for skill_card, the same
{"type": "skill-select-card", "index": ...} pattern-matching id the
prototype ships but never wires to a callback. This module adds the
selection behaviour those ids were built for: clicking a card toggles its
`selected` look (no new classes, no new elements) and the run started below
uses whichever skill is selected. Selecting the "Explore a new audit" mode
card toggles the mode cards' own selected look and shows the prototype's
existing (still-inert) `explorer-section` panel in place of the skill grid;
its two buttons stay unwired, exactly as in the prototype -- Explorer Mode
itself is out of scope here.

The two dcc.Store components holding the current selection
(selected-skill-store/selected-mode-store) live in app/app.py's serve_layout()
shell, not in home_layout()'s own return value: home_layout() is covered by
tests/test_layout_parity.py's strict, zero-diff structural comparison
against the prototype's landing_page(), and a Store the prototype does not
render at all would fail that check even though it draws no DOM. Putting it
in the shell (present on every page, like platform_header()/platform_nav())
keeps home_layout() itself pixel-for-pixel the prototype's tree.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dash import ALL, Input, Output, State, callback_context, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import request

from src import pending_runs
from src.platform import adapters
from src.platform.components import (
    data_asset_card,
    demo_indicator,
    mode_card,
    skill_card,
    upload_file_row,
    workflow_stage,
)

_DEFAULT_SKILL_ID = "SKILL-001"

_MODE_CARD_SPECS = [
    ("playbook", "Use a proven Skill",
     "Run an established audit using a governed, versioned methodology. "
     "The Skill defines data sources, cleaning rules, tests, metrics, "
     "visualisations, evidence requirements, and recommended actions."),
    ("explorer", "Explore a new audit",
     "Start with an audit objective when no proven Skill exists. "
     "The agent profiles the data, proposes an approach, asks for "
     "auditor confirmation, and can save the approved methodology "
     "as a new Skill after the run."),
]


_EXPLORER_ACTIVE_STATUSES = {"queued", "running"}


def _mode_cards(selected_mode: str) -> list:
    return [mode_card(mode_id, title, desc, selected=(mode_id == selected_mode))
            for mode_id, title, desc in _MODE_CARD_SPECS]


def _skill_cards(skills: list[dict], selected_skill_id: str | None) -> list:
    return [skill_card(s, selected=(s["skill_id"] == selected_skill_id)) for s in skills]


def _request_owner() -> str:
    """Verified-enough identity for a demo/pilot deployment: the identity
    header set by the platform's front door. CLAUDE.md §9A Q1 leaves the
    real verified-identity question open for the corporate migration; until
    then this is the best available run_owner and is never silently
    defaulted -- "local-user" is an explicit label the LOCAL backend alone
    may use (nothing else forwards these headers there); the deployed
    backend must block rather than start a run under an unverified identity
    (CLAUDE.md §9A.1, P2/P3 gate review item 7)."""
    owner = request.headers.get("X-Forwarded-Email") or request.headers.get("X-Forwarded-User")
    if owner:
        return owner
    if adapters.is_local_backend():
        return "local-user"
    raise adapters.MissingIdentityHeader(
        "No verified identity header (X-Forwarded-Email / X-Forwarded-User) was present on "
        "this request. Refusing to start a run under an unverified identity."
    )


# ─── Landing page (ported from reference_app/src/platform/pages.py) ─────────

def home_layout(default_mode: str = "playbook") -> html.Div:
    """`default_mode` lets app.py preselect Explorer mode when the Skill
    Library's "Start Explorer Mode" button navigates here with
    `?mode=explorer` (D5.1) -- home_layout() called with no argument (every
    other route, and tests/test_layout_parity.py's own call) renders
    exactly the prototype's default "playbook" state, so the zero-diff
    landing-page comparison is unaffected. Only style values (never id or
    className -- tree.py's shape() does not even inspect style) change
    between the two, the same show/hide mechanism select_mode_card already
    uses at runtime for the same two sections."""
    skills = adapters.list_skills()
    # limit=4 matches the [:4] this page has always rendered -- passing it
    # through means row-count/classification lookups (real per-table
    # queries) only ever run for the cards actually shown here, not every
    # governed table (CLAUDE.md §5 UI item 3).
    assets = adapters.search_governed_data("", limit=4)
    playbook_style = {} if default_mode == "playbook" else {"display": "none"}
    explorer_style = {"display": "none"} if default_mode == "playbook" else {}

    return html.Div([
        # Hero section
        html.Div([
            html.Div("AI AUDIT ANALYST", className="showcase-eyebrow"),
            html.H2("Start an audit analysis", className="showcase-headline"),
            html.P(
                "Bring governed data or business-provided files. Select a proven Skill "
                "or explore a new audit objective. The platform takes you from data to "
                "evidence-linked findings and management action.",
                className="showcase-supporting",
            ),
        ], className="showcase-hero"),

        demo_indicator() if adapters.is_demo_mode() else None,

        # ── Data entry routes ────────────────────────────────────────────
        html.Div([
            # Route A: Governed data
            html.Div([
                html.Div([
                    html.H3("Find governed data", style={"margin": 0}),
                    html.P("Search Unity Catalog for tables, views, volumes, and approved data assets",
                           className="sub"),
                ]),
                dcc.Input(
                    id="data-search-input",
                    type="text",
                    placeholder="Search tables, views, volumes…",
                    debounce=True,
                    style={"width": "100%", "marginBottom": 12, "padding": "8px 12px",
                           "borderRadius": 7, "border": "1px solid #cfd5e2", "fontSize": 13},
                ),
                html.Div(
                    id="data-search-results",
                    children=[data_asset_card(a) for a in assets[:4]],
                    className="stack",
                    style={"maxHeight": 400, "overflowY": "auto"},
                ),
            ], className="panel", style={"flex": 1}),

            # Route B: Upload files
            html.Div([
                html.Div([
                    html.H3("Upload audit files", style={"margin": 0}),
                    html.P("Drag-and-drop business-provided audit data", className="sub"),
                ]),
                dcc.Upload(
                    id="file-upload-area",
                    children=html.Div([
                        html.Div("Drag & drop files here, or click to browse",
                                 style={"fontSize": 13, "color": "#6b7283"}),
                        html.Div("CSV, Excel, Parquet — max 200 MB per file",
                                 style={"fontSize": 11, "color": "#9fa4b3", "marginTop": 4}),
                    ], style={"textAlign": "center"}),
                    style={
                        "borderWidth": 2, "borderStyle": "dashed", "borderColor": "#cfd5e2",
                        "borderRadius": 10, "padding": "28px 20px", "background": "#fafbfc",
                        "cursor": "pointer", "marginBottom": 12,
                    },
                    multiple=True,
                ),
                html.Div([
                    html.Div("Destination volume", style={"fontSize": 11, "fontWeight": 700,
                                                           "textTransform": "uppercase", "color": "#6b7283",
                                                           "letterSpacing": "0.03em", "marginBottom": 2}),
                    html.Div(f"{adapters.get_upload_base_path()}/runs/{{run_id}}/input",
                             style={"fontSize": 12, "fontFamily": "monospace", "color": "#3b4150"}),
                ], style={"marginBottom": 10}),
                html.Div(id="uploaded-files-list"),
                html.Div([
                    html.Span("◆", style={"color": "#e0952a", "marginRight": 4}),
                    html.Span("Uploaded files are not analysed until you start a run",
                              style={"fontSize": 11.5, "color": "#6b4a00"}),
                ], style={"marginTop": 8}),
            ], className="panel", style={"flex": 1}),
        ], className="plat-two-col"),

        # ── Skill selection ──────────────────────────────────────────────
        html.Div([
            html.H3("How should the agent approach this audit?", style={"margin": "0 0 4px"}),
            html.P("Choose a proven Skill or explore a new audit objective", className="sub"),
        ], style={"marginTop": 24}),

        html.Div(
            _mode_cards(default_mode),
            className="plat-two-col", id="mode-selector-row",
        ),

        # Playbook skill cards
        html.Div([
            html.Div(
                _skill_cards(skills, None),
                className="plat-skill-grid",
                id="skill-cards-container",
            ),
        ], id="playbook-skills-section", style=playbook_style),

        # Explorer section (hidden by default)
        html.Div([
            html.Div([
                html.Div([
                    html.Span("◎", style={"color": "#e0952a", "fontSize": 18, "marginRight": 8}),
                    html.Span("Explorer Mode", style={"fontSize": 14, "fontWeight": 700, "color": "#1e2761"}),
                ], style={"display": "flex", "alignItems": "center", "marginBottom": 8}),
                html.P("No pre-existing Skill is required. The agent will profile your data "
                       "and propose an approach for your review.",
                       style={"fontSize": 13, "color": "#3b4150", "marginBottom": 8}),
                html.Div([
                    html.Span("◆", style={"color": "#e0952a", "marginRight": 4}),
                    html.Span("Explorer Mode requires auditor confirmation before execution",
                              style={"fontSize": 12, "color": "#6b4a00"}),
                ], style={"marginBottom": 10}),
                html.Label("Data sources for this objective (choose 1–5)",
                           style={"fontSize": 12, "fontWeight": 700, "color": "#1a1d26"}),
                dcc.Checklist(
                    id="explorer-source-checklist",
                    options=[],
                    value=[],
                    style={"fontSize": 13, "color": "#3b4150"},
                    labelStyle={"display": "block", "marginBottom": 6},
                ),
                html.Button("Start new objective", id="explorer-start-btn", className="btn-generate",
                            style={"marginRight": 8}),
                html.Button("Save completed approach as draft Skill", id="explorer-save-draft-btn",
                            className="ghost", style={"width": "auto"}),
            ], className="panel"),
        ], id="explorer-section", style=explorer_style),

        # ── Run configuration ────────────────────────────────────────────
        html.Div([
            html.H3("Audit configuration", style={"margin": "0 0 4px"}),
            html.P("Set the scope and parameters for this audit run", className="sub"),
        ], style={"marginTop": 24}),

        html.Div([
            html.Div([
                html.Label("Audit objective", style={"fontSize": 12, "fontWeight": 700, "color": "#1a1d26"}),
                dcc.Textarea(
                    id="audit-objective",
                    value=(
                        "Assess the selected population for control exceptions, quantify the "
                        "potential exposure, identify evidence-backed risk themes, and recommend "
                        "management actions."
                    ),
                    style={"width": "100%", "height": 80, "fontSize": 13, "borderRadius": 7,
                           "border": "1px solid #cfd5e2", "padding": "8px 12px", "resize": "vertical"},
                ),
            ], style={"marginBottom": 14}),

            html.Div([
                html.Div([
                    html.Label("Audit period", style={"fontSize": 12, "fontWeight": 700, "color": "#1a1d26"}),
                    dcc.DatePickerRange(
                        id="audit-period",
                        start_date="2025-01-01",
                        end_date="2026-06-30",
                        display_format="DD MMM YYYY",
                    ),
                ], style={"flex": 1}),
                html.Div([
                    html.Label("Business unit (optional)", style={"fontSize": 12, "fontWeight": 700, "color": "#1a1d26"}),
                    dcc.Input(id="audit-bu", type="text", placeholder="All",
                              style={"width": "100%", "padding": "8px 12px", "borderRadius": 7,
                                     "border": "1px solid #cfd5e2", "fontSize": 13}),
                ], style={"flex": 1}),
            ], style={"display": "flex", "gap": 16, "marginBottom": 14}),

            # Advanced parameters (collapsed)
            html.Details([
                html.Summary("Advanced parameters", style={"fontSize": 13, "fontWeight": 600,
                                                            "cursor": "pointer", "color": "#1e2761",
                                                            "marginBottom": 8}),
                html.Div([
                    html.Div([
                        html.Label("Materiality threshold", style={"fontSize": 12, "fontWeight": 700}),
                        dcc.Input(id="audit-materiality", type="number", placeholder="$0",
                                  style={"width": "100%", "padding": "8px 12px", "borderRadius": 7,
                                         "border": "1px solid #cfd5e2", "fontSize": 13}),
                    ], style={"flex": 1}),
                ], style={"display": "flex", "gap": 16}),
                html.Div([
                    dcc.Checklist(
                        id="audit-options",
                        options=[
                            {"label": " Show proposed approach before execution", "value": "preview_plan"},
                            {"label": " Generate management actions after review", "value": "gen_actions"},
                            {"label": " Prepare Jira ticket previews", "value": "jira_preview"},
                        ],
                        value=["preview_plan", "gen_actions"],
                        style={"fontSize": 13, "color": "#3b4150"},
                        labelStyle={"display": "block", "marginBottom": 6},
                    ),
                ], style={"marginTop": 12}),
            ]),
        ], className="panel"),

        # ── Agent plan preview ───────────────────────────────────────────
        html.Div([
            html.H3("Proposed workflow", style={"margin": "0 0 4px"}),
            html.P("Preview of the execution stages before starting the run", className="sub"),
        ], style={"marginTop": 24}),
        html.Div(id="workflow-preview-container"),

        # ── Start analysis ───────────────────────────────────────────────
        html.Div([
            html.Div(id="run-summary-preview"),
            html.Button("Start audit analysis", id="start-run-btn", className="btn-generate",
                        style={"fontSize": 15, "fontWeight": 700, "padding": "12px 32px",
                               "marginTop": 16, "width": "100%", "maxWidth": 360}),
        ], style={"marginTop": 24, "textAlign": "center"}),

    ], className="shell dashboard-shell plat-landing")


# ─── Binding (not a prototype concept -- see the change report) ────────────

_DEFAULT_ENGAGEMENT_ID = "ENG-DEFAULT"  # same seeded default every other caller here uses (CLAUDE.md §4.8)

# Independent review 2026-09-24 item 7: "governed tables win unless the
# upload was made in the current page session" -- this app has no real
# per-tab session id to check an upload against, so recency is the
# approximation: an upload from the last _RECENT_UPLOAD_WINDOW_S is treated
# as plausibly this session's own deliberate upload and allowed to
# override a governed table of the same name; an older one no longer
# overrides a governed table but is still usable when no governed table
# exists for that source at all (see _auto_bind's own docstring).
_RECENT_UPLOAD_WINDOW_S = 15 * 60


def _is_recent_upload(uploaded_at: str | None) -> bool:
    if not uploaded_at:
        return False
    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_s = (datetime.now(timezone.utc) - ts).total_seconds()
    return 0 <= age_s <= _RECENT_UPLOAD_WINDOW_S


def _auto_bind(skill_id: str) -> tuple[dict[str, str], list[str]]:
    """The prototype's landing page has no binding step at all: a run always
    used whichever fixed demo data was already loaded. A real run needs a
    concrete source per contract entry, so this binds by EXACT name match
    only, never fuzzy (CLAUDE.md NN14/§0.5):

      1. the governed-table/local-default suggestion (service.
         suggest_bindings, itself an exact short-name match), UNLESS
      2. an uploaded, Ready file whose filename (without extension) equals
         the contract source name exactly, scoped to the current user's own
         uploads in the current engagement (independent review 2026-09-24
         item 7: an unscoped match could silently bind a run to a DIFFERENT
         auditor's upload, in a different engagement, that happens to share
         a filename), AND uploaded recently enough to plausibly be this
         page session's own upload (_RECENT_UPLOAD_WINDOW_S -- an
         approximation: this app has no real per-tab session id to check
         against, so "governed wins unless the upload is from the current
         session" is approximated by "unless it was made in the last few
         minutes", never a literal session check) -- an upload this fresh
         wins over a governed table of the same name, on every backend (the
         earlier local-only gate here was an artificial restriction --
         VolumeUploadAwareDataSource already reads an uploaded file the
         same way regardless of data-source backend).

    Returns (bindings, missing_source_names). A source with neither is left
    out of `bindings` and named in `missing_source_names` -- the caller
    blocks the run rather than starting one with an incomplete contract."""
    # BUG-STARTRUN-1 (P3/P4 perf gap review 2026-09-25, live pass): fetched
    # once and threaded into suggest_bindings -- it used to call get_skill
    # AGAIN internally, a second skill_versions + list_runs round trip for
    # the exact same skill card, right on this callback's synchronous path.
    skill = adapters.get_skill(skill_id) or {}
    source_names = [s.get("source") for s in (skill.get("sources") or [])]
    suggested = adapters.suggest_bindings(skill_id, skill=skill) or {}
    # Independent review 2026-09-25 item 1 ("run inputs" -- docs/specs/
    # P7_mapping_authoring_design.md §1.3): a source SOURCE_BINDINGS declares
    # kind=not_supplied needs no physical binding at all -- treated as bound
    # here (never missing), with no value in `bindings` for it either;
    # start_audit_run resolves it the same way from the same config.
    not_supplied = adapters.not_supplied_sources(skill_id)

    current_owner = _request_owner()
    uploads_by_stem: dict[str, dict] = {}
    for row in adapters.list_uploaded_files(engagement_id=_DEFAULT_ENGAGEMENT_ID):
        if row.get("status") != "Ready" or row.get("uploaded_by") != current_owner:
            continue
        stem = Path(row["filename"]).stem
        # Newest upload wins if this user has more than one Ready upload
        # with the same stem (a re-upload superseding an earlier one).
        existing = uploads_by_stem.get(stem)
        if existing is None or (row.get("uploaded_at") or "") > (existing.get("uploaded_at") or ""):
            uploads_by_stem[stem] = row

    bindings: dict[str, str] = {}
    missing: list[str] = []
    for name in source_names:
        if name in not_supplied:
            continue
        upload_row = uploads_by_stem.get(name)
        governed = suggested.get(name)
        upload_is_recent = upload_row is not None and _is_recent_upload(upload_row.get("uploaded_at"))
        if governed and not upload_is_recent:
            bindings[name] = governed
        elif upload_row is not None:
            bindings[name] = upload_row["volume_path"]
        elif governed:
            bindings[name] = governed
        else:
            missing.append(name)
    return bindings, missing


_EXPLORER_SOURCE_SEARCH_LIMIT = 25  # governed-table matches offered per search keystroke in the
# checklist -- a human ticks these by hand, so a generous but bounded list is enough; distinct
# from the 1-5 SELECTION cap (_MAX_EXPLORER_SOURCES) start_new_objective enforces below.

_MAX_EXPLORER_SOURCES = 5  # matches orchestrator.service.start_explorer_run's own inline
# "1 <= len(sources) <= 5" bound -- no importable named constant exists there, so this must be
# kept in sync with it by hand if that bound ever changes.


def _explorer_source_options(query: str | None) -> list[dict]:
    """(D5a, 2026-09-25) The explorer-source-checklist's real option list --
    governed tables this identity can read (Available only, via
    adapters.search_governed_data narrowed by `query` exactly as Route A's
    own data-search-input search does, capped at
    _EXPLORER_SOURCE_SEARCH_LIMIT) plus this user's own Ready uploads
    (adapters.list_uploaded_files, filtered by status/uploaded_by exactly as
    _auto_bind filters Playbook uploads above). Restricted tables are never
    offered. Replaces this module's old auto-pick behaviour (formerly
    `_explorer_source_candidates`, which silently chose "the first 5
    available" with nothing shown for confirmation) -- D5a instead surfaces
    every eligible source as a checklist option and lets the auditor tick
    1-5 of them; nothing here is auto-selected.

    Each option's value is a JSON-encoded {"kind", "ref"} pair -- exactly
    the shape adapters.start_explorer_run's `sources` entries need -- so a
    ticked value round-trips back into a source dict with a plain
    json.loads, never a fuzzy re-lookup."""
    options: list[dict] = []
    is_local = adapters.is_local_backend()
    for asset in adapters.search_governed_data(query or "", limit=_EXPLORER_SOURCE_SEARCH_LIMIT):
        if asset.get("access") != "Available" or not asset.get("name"):
            continue
        value = json.dumps(
            {"kind": "local_file" if is_local else "uc_table", "ref": asset["name"]}, sort_keys=True
        )
        options.append({"label": asset["name"], "value": value})

    owner = _request_owner()
    for row in adapters.list_uploaded_files(engagement_id=_DEFAULT_ENGAGEMENT_ID):
        if row.get("status") != "Ready" or row.get("uploaded_by") != owner:
            continue
        value = json.dumps({"kind": "upload", "ref": row["upload_id"]}, sort_keys=True)
        options.append({"label": f"Upload: {row['filename']}", "value": value})
    return options


def _explorer_error_panel(message: str) -> html.Div:
    return html.Div([
        html.Div([
            html.Span("Mode", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                      "color": "#6b7283", "letterSpacing": "0.03em"}),
            html.Div(message, style={"fontSize": 13, "fontWeight": 600, "color": "#b85042"}),
        ]),
    ], className="panel", style={"marginTop": 12})


def _explorer_note_panel(message: str, *, ok: bool = True) -> html.Div:
    return html.Div([
        html.Div(message, style={"fontSize": 13, "fontWeight": 600, "color": "#2c7a4b" if ok else "#b85042"}),
    ], className="panel", style={"marginTop": 12})


def _explorer_workflow_children(review: dict) -> list:
    """docs/specs/P6_P8_explorer_llm_design.md §5.1: the existing nine
    `workflow_stage` rows from real Explorer status, plus one row per
    proposed test (ready for valid, pending/grey for greyed, reason as
    detail), plus (D4) one dcc.Checklist -- an existing prototype control --
    listing the valid tests for include/exclude editing."""
    tests = review.get("tests", [])
    n_valid, n_total = review.get("n_valid", 0), review.get("n_total", 0)

    if review.get("run_status") == "failed":
        # BUG-EXPLORER-PLAN-1 (independent review round 5, RUN-B68ACB9ED712):
        # a node exception (e.g. plan) must reach this panel visibly (NN14)
        # -- the prior fall-through ("Profiling data and proposing tests…")
        # rendered a genuinely dead run as if it were still in progress,
        # with no error anywhere the auditor could see.
        plan_detail = review.get("status_reason") or "The run failed — see /trace for details."
    elif review.get("llm_unavailable"):
        plan_detail = review.get("label") or "LLM unavailable — deterministic output only"
    elif review.get("plan_status") == "proposed":
        plan_detail = f"{n_valid} test(s) proposed · {n_total - n_valid} greyed"
    else:
        plan_detail = "Profiling data and proposing tests…"

    stages = [
        {"stage": "Source data", "status": "ready", "detail": None},
        {"stage": "Data quality & reconciliation", "status": "pending", "detail": None},
        {"stage": "Skill / Explorer plan", "status": "needs_confirmation", "detail": plan_detail},
        {"stage": "Deterministic audit tests", "status": "pending",
         "detail": f"{n_total} test(s) defined" if n_total else None},
        {"stage": "Exception classification", "status": "pending", "detail": None},
        {"stage": "Evidence-linked findings", "status": "pending", "detail": None},
        {"stage": "Insights & prioritisation", "status": "pending", "detail": None},
        {"stage": "Management actions", "status": "pending", "detail": None},
        {"stage": "Export & Jira preview", "status": "pending", "detail": None},
    ]
    for t in tests:
        if t["valid"]:
            detail = t.get("rationale") or "Valid"
        else:
            reasons = "; ".join(r.get("message", "") for r in t.get("reasons", []) if r.get("message"))
            detail = reasons or "Not valid for this data"
        stages.append({
            "stage": t.get("name") or t["key"],
            "status": "ready" if t["valid"] else "pending",
            "detail": detail,
        })

    total = len(stages)
    children = [
        html.Div([workflow_stage(s, i, total) for i, s in enumerate(stages)], style={"padding": "12px 0"}),
    ]
    if review.get("llm_unavailable"):
        children.append(demo_indicator(review.get("label") or "LLM unavailable — deterministic output only"))

    valid_tests = [t for t in tests if t["valid"]]
    if valid_tests:
        children.append(dcc.Checklist(
            id="explorer-test-checklist",
            options=[
                {"label": f" {t.get('name') or t['key']} — {t.get('rationale') or ''}", "value": t["key"]}
                for t in valid_tests
            ],
            value=[t["key"] for t in valid_tests if t.get("included")],
            style={"fontSize": 13, "color": "#3b4150", "marginTop": 12},
            labelStyle={"display": "block", "marginBottom": 6},
        ))
    if review.get("proposal_errors"):
        children.append(html.Div(
            "; ".join(
                e.get("message", "") if isinstance(e, dict) else str(e)
                for e in review["proposal_errors"]
            ),
            style={"fontSize": 11.5, "color": "#b85042", "marginTop": 8},
        ))
    return children


# ─── Callbacks (ported from reference_app/app.py) ───────────────────────────

def register_callbacks(app) -> None:

    @app.callback(
        Output("skill-cards-container", "children"),
        Output("selected-skill-store", "data"),
        Input({"type": "skill-select-card", "index": ALL}, "n_clicks"),
        State({"type": "skill-select-card", "index": ALL}, "id"),
        prevent_initial_call=True,
    )
    def select_skill_card(n_clicks_list, ids):
        """Wires the prototype's own skill_card `selected` styling and its
        {"type": "skill-select-card", ...} id (reference_app/src/platform/
        components.py) to a click -- the prototype never adds this callback,
        it just ships the id and the styling parameter unused."""
        if not any(n_clicks_list):
            raise PreventUpdate
        triggered = callback_context.triggered_id
        if not isinstance(triggered, dict):
            raise PreventUpdate
        selected_skill_id = triggered["index"]
        skills = adapters.list_skills()
        return _skill_cards(skills, selected_skill_id), selected_skill_id

    @app.callback(
        Output("mode-selector-row", "children"),
        Output("selected-mode-store", "data"),
        Output("playbook-skills-section", "style"),
        Output("explorer-section", "style"),
        Input({"type": "mode-select-card", "index": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def select_mode_card(n_clicks_list):
        """Same idea as select_skill_card, for mode_card's own id/selected
        pair. Selecting "explorer" swaps the skill grid for the prototype's
        existing (still-inert) explorer-section panel; its two buttons are
        not wired here -- Explorer Mode is out of scope for this change."""
        if not any(n_clicks_list):
            raise PreventUpdate
        triggered = callback_context.triggered_id
        if not isinstance(triggered, dict):
            raise PreventUpdate
        selected_mode = triggered["index"]
        playbook_style = {} if selected_mode == "playbook" else {"display": "none"}
        explorer_style = {"display": "none"} if selected_mode == "playbook" else {}
        return _mode_cards(selected_mode), selected_mode, playbook_style, explorer_style

    @app.callback(
        Output("data-search-results", "children"),
        Input("data-search-input", "value"),
        prevent_initial_call=True,
    )
    def search_data_assets(query):
        """Search governed data assets via adapter."""
        results = adapters.search_governed_data(query or "", limit=6)
        if not results:
            return html.P("No matching data assets found.", style={"color": "#6b7283", "fontSize": 13})
        return [data_asset_card(a) for a in results[:6]]

    @app.callback(
        Output("explorer-source-checklist", "options"),
        Output("explorer-source-checklist", "value"),
        Input("selected-mode-store", "data"),
        Input("data-search-input", "value"),
        Input("uploaded-files-list", "children"),
        State("explorer-source-checklist", "value"),
        prevent_initial_call=False,
    )
    def refresh_explorer_source_checklist(selected_mode, query, _uploaded_children, current_value):
        """(D5a) Rebuilds the explorer-source-checklist's options -- entering
        Explorer mode (selected-mode-store becomes "explorer"), every
        data-search-input keystroke (the same Route A search box), and every
        completed upload (uploaded-files-list changes) each retrigger this.
        A PreventUpdate while not in Explorer mode means this never queries
        governed data on page load or while browsing Playbook. Ticks the
        auditor already made are kept, but only for values still offered
        (an option that drops out of a narrower search or a since-restricted
        table cannot stay ticked); nothing is ever auto-picked."""
        if selected_mode != "explorer":
            raise PreventUpdate
        options = _explorer_source_options(query)
        offered = {opt["value"] for opt in options}
        kept_value = [v for v in (current_value or []) if v in offered]
        return options, kept_value

    @app.callback(
        Output("workflow-preview-container", "children"),
        Output("explorer-poll-interval", "disabled"),
        Input("audit-objective", "value"),
        Input("selected-mode-store", "data"),
        Input("explorer-run-store", "data"),
        Input("explorer-poll-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def render_workflow_preview(objective, selected_mode, explorer_store, _n_intervals):
        """Show the agent plan preview stages. In Explorer mode, once
        "Start new objective" has set explorer-run-store, this polls
        get_explorer_review (D3) instead -- and only THEN is
        explorer-poll-interval enabled (disabled=not active), so there is no
        idle polling before a run exists or after its plan phase finishes
        (CLAUDE.md §11 cost incident)."""
        explorer_store = explorer_store or {}
        run_id = explorer_store.get("run_id")
        if run_id:
            review = adapters.get_explorer_review(run_id)
            active = review["run_status"] in _EXPLORER_ACTIVE_STATUSES
            return html.Div(_explorer_workflow_children(review), className="panel"), not active

        if selected_mode == "explorer":
            plan = adapters.propose_plan({"mode": "explorer", "skill": None})
            stages = plan.get("stages", [])
            total = len(stages)
            return html.Div([
                html.Div([workflow_stage(s, i, total) for i, s in enumerate(stages)],
                         style={"padding": "12px 0"}),
            ], className="panel"), True

        plan = adapters.propose_plan({"mode": "playbook", "skill": adapters.get_skill(_DEFAULT_SKILL_ID), "sources_count": 3})
        stages = plan.get("stages", [])
        total = len(stages)
        return html.Div([
            html.Div(
                [workflow_stage(s, i, total) for i, s in enumerate(stages)],
                style={"padding": "12px 0"},
            ),
            demo_indicator("Preview only — workflow has not been executed") if plan.get("mock") else None,
        ], className="panel"), True

    @app.callback(
        Output("run-summary-preview", "children"),
        Input("audit-objective", "value"),
        prevent_initial_call=False,
    )
    def render_run_summary(objective):
        """Show compact confirmation summary before starting."""
        return html.Div([
            html.Div([
                html.Div([
                    html.Span("Mode", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                              "color": "#6b7283", "letterSpacing": "0.03em"}),
                    html.Div("Playbook", style={"fontSize": 13, "fontWeight": 600}),
                ], style={"flex": 1}),
                html.Div([
                    html.Span("Skill", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                               "color": "#6b7283", "letterSpacing": "0.03em"}),
                    html.Div("ExCo T&E Executive Diligence", style={"fontSize": 13, "fontWeight": 600}),
                ], style={"flex": 2}),
                html.Div([
                    html.Span("Data mode", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                                   "color": "#6b7283", "letterSpacing": "0.03em"}),
                    html.Div("Demo" if adapters.is_demo_mode() else "Live",
                             style={"fontSize": 13, "fontWeight": 600}),
                ], style={"flex": 1}),
                html.Div([
                    html.Span("Outputs", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                                 "color": "#6b7283", "letterSpacing": "0.03em"}),
                    html.Div("Findings, PPTX, Excel", style={"fontSize": 13, "fontWeight": 600}),
                ], style={"flex": 1}),
            ], style={"display": "flex", "gap": 16}),
        ], className="panel", style={"marginTop": 12})

    @app.callback(
        Output("uploaded-files-list", "children"),
        Input("file-upload-area", "contents"),
        State("file-upload-area", "filename"),
        State("uploaded-files-list", "children"),
        prevent_initial_call=True,
    )
    def handle_upload(contents_list, filename_list, existing_children):
        """Populates the (already-existing, previously always-empty)
        uploaded-files-list container -- no callback wired it in the
        prototype (reference_app/app.py has no Input on file-upload-area at
        all). Adds to `existing_children` rather than replacing it, so
        repeated drops accumulate."""
        if not contents_list:
            raise PreventUpdate
        import base64

        owner = _request_owner()
        rows = list(existing_children) if existing_children else []
        for contents, filename in zip(contents_list, filename_list or []):
            try:
                _header, b64data = contents.split(",", 1)
                content = base64.b64decode(b64data)
                result = adapters.upload_audit_file(filename, content, owner)
                rows.append(upload_file_row({
                    "filename": result["filename"],
                    "size_bytes": result["size_bytes"],
                    "destination": result["volume_path"],
                    "validation": result["status"],
                }))
            except Exception as exc:
                rows.append(upload_file_row({
                    "filename": filename,
                    "size_bytes": 0,
                    "destination": str(exc),
                    "validation": "Failed",
                }))
        return rows

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("run-summary-preview", "children", allow_duplicate=True),
        Input("start-run-btn", "n_clicks"),
        State("audit-objective", "value"),
        State("audit-period", "start_date"),
        State("audit-period", "end_date"),
        State("audit-bu", "value"),
        State("audit-materiality", "value"),
        State("audit-options", "value"),
        State("selected-skill-store", "data"),
        State("explorer-run-store", "data"),
        prevent_initial_call=True,
    )
    def start_run(n_clicks, objective, start_date, end_date, business_unit, materiality, options,
                   selected_skill_id, explorer_store):
        """Starts a real audit run and navigates to it AT ONCE (CLAUDE.md §11
        "Run start opens the run page at once", 2026-09-25): the actual
        adapters.start_audit_run(...) call (CLAUDE.md §2.1: it only inserts
        the `runs` row and hands off to the executor, but doing even that
        takes several sequential Delta writes -- ~10-20s, measured live) is
        submitted to pending_runs' own small background worker rather than
        awaited here, so this callback returns -- and the browser navigates
        to /run/<run_id> -- in about a second. Until the `runs` row actually
        exists, /run/<run_id> reads pending_runs' registry to show the same
        "Queued" state a real queued run shows, or the same error panel on
        failure (never silently -- NN14). See the change report for the
        alternative considered.

        DEVIATION from the prototype: `start_demo_run` always navigated to
        the fixed "/workspace/tne" dashboard, because it never started a
        real run -- there was nothing to wait for. A real run is not
        complete the instant it is created (it must clear the mandatory
        plan-confirmation and findings-sign-off gates, CLAUDE.md §2.4
        non-negotiable 3), so this navigates to the run's own status page
        instead.

        Explorer mode (D3): when explorer-run-store holds a run_id, this
        button CONFIRMS that run's proposal (with any plan_edits already
        applied) instead of starting a new Playbook run -- exactly the
        "Start audit analysis... confirms the plan" behaviour §5.1
        describes for "Start new objective" having already been clicked.
        confirm_plan is a single, already-fast state transition (not the
        multi-write run creation above), so it stays synchronous here, as
        it always has."""
        if not n_clicks:
            raise PreventUpdate

        explorer_run_id = (explorer_store or {}).get("run_id")
        if explorer_run_id:
            try:
                actor = _request_owner()
                adapters.confirm_plan(explorer_run_id, actor)
            except Exception as exc:  # NN14: fail loudly and visibly, never a silent default
                return no_update, _explorer_error_panel(f"Could not confirm the plan: {exc}")
            return f"/run/{explorer_run_id}", no_update

        options = options or []
        skill_id = selected_skill_id or _DEFAULT_SKILL_ID

        try:
            # _auto_bind and _request_owner still run synchronously, here,
            # before anything is submitted to the background worker:
            # _request_owner() reads flask.request, which only exists
            # inside this request -- it would raise RuntimeError if called
            # from the background thread after this callback has returned.
            # Both are already fast (no multi-write run creation), so
            # deferring them would buy nothing; a bad binding or an
            # unverified identity is shown in THIS response, exactly as
            # before, never deferred to the run page (independent review
            # 2026-09-24 item 7 -- it scopes uploads to the current user).
            bindings, missing = _auto_bind(skill_id)
            if missing:
                raise ValueError(
                    "no governed table or uploaded file matches contract source(s) "
                    f"{', '.join(missing)} by exact name."
                )
            run_owner = _request_owner()
        except Exception as exc:  # NN14: fail loudly and visibly, never a silent default
            return no_update, html.Div([
                html.Div([
                    html.Span("Mode", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                              "color": "#6b7283", "letterSpacing": "0.03em"}),
                    html.Div(f"Could not start the run: {exc}",
                             style={"fontSize": 13, "fontWeight": 600, "color": "#b85042"}),
                ]),
            ], className="panel", style={"marginTop": 12})

        run_kwargs = dict(
            skill_id=skill_id,
            bindings=bindings,
            audit_period=(start_date, end_date),
            objective=(objective or "").strip(),
            run_owner=run_owner,
            mode="playbook",
            review_plan_first="preview_plan" in options,
            business_unit=(business_unit or "").strip() or None,
            materiality=float(materiality) if materiality not in (None, "") else None,
            generate_management_actions="gen_actions" in options,
            jira_preview_requested="jira_preview" in options,
        )
        # A genuine double-click / resubmit (same auditor, same form state,
        # before the first click's background write has finished) reuses
        # that click's own pending run_id instead of starting a second run
        # -- "the button can't fire twice for one id".
        dedupe_key = hashlib.sha256(
            json.dumps(run_kwargs, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        existing = pending_runs.find_pending(dedupe_key)
        if existing:
            return f"/run/{existing}", no_update

        run_id = adapters.generate_run_id()
        pending_runs.start(
            run_id, dedupe_key,
            lambda: adapters.start_audit_run(run_id=run_id, **run_kwargs),
        )
        return f"/run/{run_id}", no_update

    @app.callback(
        Output("explorer-run-store", "data"),
        Output("run-summary-preview", "children", allow_duplicate=True),
        Input("explorer-start-btn", "n_clicks"),
        State("audit-objective", "value"),
        State("audit-period", "start_date"),
        State("audit-period", "end_date"),
        State("audit-bu", "value"),
        State("audit-materiality", "value"),
        State("explorer-run-store", "data"),
        State("explorer-source-checklist", "value"),
        prevent_initial_call=True,
    )
    def start_new_objective(n_clicks, objective, start_date, end_date, business_unit, materiality,
                             prior_store, selected_source_values):
        """(D5a) "Start new objective": starts Explorer planning over
        exactly the sources the auditor ticked in explorer-source-checklist,
        in the order shown -- superseding this session's own previous
        still-awaiting-confirmation Explorer run, if any, exactly as §5.1's
        own row describes. Explorer-run-store is the one D2 Store that
        render_workflow_preview then picks up (its own Input on this same
        store) to switch the "Proposed workflow" panel into polling the real
        Explorer run instead of showing the generic preview.

        0 ticked and >5 ticked each fail here with the stated message,
        before any run is started (D5a: "no selection gives a visible
        error" -- there is no silent auto-pick fallback any more)."""
        if not n_clicks:
            raise PreventUpdate
        selected = list(selected_source_values or [])
        if len(selected) == 0:
            return no_update, _explorer_error_panel("Select at least one data source for Explorer Mode.")
        if len(selected) > _MAX_EXPLORER_SOURCES:
            return no_update, _explorer_error_panel("Select at most 5 data sources.")
        try:
            sources = [json.loads(v) for v in selected]
            run_owner = _request_owner()
            prior_run_id = (prior_store or {}).get("run_id")
            run_id = adapters.start_explorer_run(
                objective=(objective or "").strip(),
                sources=sources,
                audit_period=(start_date, end_date),
                run_owner=run_owner,
                supersedes_run_id=prior_run_id,
                business_unit=(business_unit or "").strip() or None,
                materiality=float(materiality) if materiality not in (None, "") else None,
            )
        except Exception as exc:  # NN14: fail loudly and visibly, never a silent default
            return no_update, _explorer_error_panel(f"Could not start Explorer Mode: {exc}")
        return {"run_id": run_id}, no_update

    @app.callback(
        Output("run-summary-preview", "children", allow_duplicate=True),
        Input("explorer-test-checklist", "value"),
        State("explorer-run-store", "data"),
        prevent_initial_call=True,
    )
    def edit_explorer_checklist(included_keys, explorer_store):
        """D4: the one include/exclude edit control -- a dcc.Checklist of
        the proposal's currently VALID tests (greyed tests are never
        offered, they can never be included, §4.9). Diffs the checklist's
        new value against the review's own current included set and sends
        only the resulting include_test/exclude_test ops to
        edit_explorer_plan; a batch edit_explorer_plan rejects outright
        (ExplorerEditRejected) is shown here, never silently dropped."""
        run_id = (explorer_store or {}).get("run_id")
        if not run_id:
            raise PreventUpdate
        review = adapters.get_explorer_review(run_id)
        valid_keys = {t["key"] for t in review["tests"] if t["valid"]}
        currently_included = {t["key"] for t in review["tests"] if t["valid"] and t["included"]}
        new_included = set(included_keys or []) & valid_keys
        to_exclude = currently_included - new_included
        to_include = new_included - currently_included
        if not to_exclude and not to_include:
            raise PreventUpdate
        edits = (
            [{"op": "exclude_test", "test_key": k} for k in sorted(to_exclude)]
            + [{"op": "include_test", "test_key": k} for k in sorted(to_include)]
        )
        try:
            actor = _request_owner()
            adapters.edit_explorer_plan(run_id, edits, actor)
        except Exception as exc:  # NN14: fail loudly and visibly, never a silent default
            return _explorer_error_panel(f"Could not update the plan: {exc}")
        raise PreventUpdate

    @app.callback(
        Output("run-summary-preview", "children", allow_duplicate=True),
        Input("explorer-save-draft-btn", "n_clicks"),
        State("explorer-run-store", "data"),
        prevent_initial_call=True,
    )
    def save_explorer_draft(n_clicks, explorer_store):
        """"Save completed approach as draft Skill" (§5.1): enabled IN
        EFFECT only when this session's Explorer run is completed --
        otherwise this writes the reason to run-summary-preview rather than
        pretending the click did nothing."""
        if not n_clicks:
            raise PreventUpdate
        run_id = (explorer_store or {}).get("run_id")
        if not run_id:
            return _explorer_error_panel(
                "No Explorer run in this session yet -- start a new objective first."
            )
        run = adapters.get_run(run_id)
        if run is None or run.get("status") != "completed":
            status = (run or {}).get("status", "unknown")
            return _explorer_error_panel(
                f"This Explorer run is not yet completed (status: {status}) -- "
                "sign off and export before saving it as a draft Skill."
            )
        try:
            actor = _request_owner()
            saved = adapters.save_explorer_draft_skill(run_id, actor)
        except Exception as exc:  # NN14: fail loudly and visibly, never a silent default
            return _explorer_error_panel(f"Could not save draft Skill: {exc}")
        return _explorer_note_panel(
            f"Saved as draft Skill {saved['skill_id']} v{saved['version']} — "
            "it now appears in the Skill Library."
        )

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("url", "search", allow_duplicate=True),
        Input("start-explorer-from-library-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def start_explorer_from_library(n_clicks):
        """Skill Library's "Start Explorer Mode" (§5.1): navigates to `/`
        with Explorer mode preselected -- app.py's route_page reads
        `?mode=explorer` and renders home_layout(default_mode="explorer")."""
        if not n_clicks:
            raise PreventUpdate
        return "/", "?mode=explorer"
