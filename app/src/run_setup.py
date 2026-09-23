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

The mode cards, skill cards and Explorer section are exactly as
reference_app renders them: no selection callback exists there in the
prototype (mode_card/skill_card's `selected` state is a hardcoded render-time
flag, not wired to any Input), so none is added here either -- the run this
page starts is always Playbook / SKILL-001, matching the run-summary-preview
panel's own hardcoded "Mode: Playbook" / "Skill: ExCo T&E Executive
Diligence" text, exactly as the prototype displays it regardless of which
card looks selected.
"""

from __future__ import annotations

from pathlib import Path

from dash import Input, Output, State, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import request

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

def home_layout() -> html.Div:
    skills = adapters.list_skills()
    assets = adapters.search_governed_data("")

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

        html.Div([
            mode_card("playbook", "Use a proven Skill",
                      "Run an established audit using a governed, versioned methodology. "
                      "The Skill defines data sources, cleaning rules, tests, metrics, "
                      "visualisations, evidence requirements, and recommended actions.",
                      selected=True),
            mode_card("explorer", "Explore a new audit",
                      "Start with an audit objective when no proven Skill exists. "
                      "The agent profiles the data, proposes an approach, asks for "
                      "auditor confirmation, and can save the approved methodology "
                      "as a new Skill after the run."),
        ], className="plat-two-col", id="mode-selector-row"),

        # Playbook skill cards
        html.Div([
            html.Div(
                [skill_card(s) for s in skills],
                className="plat-skill-grid",
                id="skill-cards-container",
            ),
        ], id="playbook-skills-section"),

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
                html.Button("Start new objective", className="btn-generate",
                            style={"marginRight": 8}),
                html.Button("Save completed approach as draft Skill", className="ghost",
                            style={"width": "auto"}),
            ], className="panel"),
        ], id="explorer-section", style={"display": "none"}),

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

def _auto_bind(skill_id: str) -> tuple[dict[str, str], list[str]]:
    """The prototype's landing page has no binding step at all: a run always
    used whichever fixed demo data was already loaded. A real run needs a
    concrete source per contract entry, so this binds by EXACT name match
    only, never fuzzy (CLAUDE.md NN14/§0.5):

      1. an uploaded, Ready file whose filename (without extension) equals
         the contract source name exactly -- local backend only (see the
         change report for why the UC backend is not wired here yet);
      2. otherwise the governed-table/local-default suggestion
         (service.suggest_bindings), itself an exact short-name match.

    Returns (bindings, missing_source_names). A source with neither is left
    out of `bindings` and named in `missing_source_names` -- the caller
    blocks the run rather than starting one with an incomplete contract."""
    skill = adapters.get_skill(skill_id) or {}
    source_names = [s.get("source") for s in (skill.get("sources") or [])]
    suggested = adapters.suggest_bindings(skill_id) or {}

    uploads_by_stem: dict[str, dict] = {}
    if adapters.is_local_backend():
        for row in adapters.list_uploaded_files():
            if row.get("status") == "Ready":
                stem = Path(row["filename"]).stem
                uploads_by_stem.setdefault(stem, row)

    bindings: dict[str, str] = {}
    missing: list[str] = []
    for name in source_names:
        upload_row = uploads_by_stem.get(name)
        if upload_row is not None:
            bindings[name] = upload_row["volume_path"]
        elif suggested.get(name):
            bindings[name] = suggested[name]
        else:
            missing.append(name)
    return bindings, missing


# ─── Callbacks (ported from reference_app/app.py) ───────────────────────────

def register_callbacks(app) -> None:

    @app.callback(
        Output("data-search-results", "children"),
        Input("data-search-input", "value"),
        prevent_initial_call=True,
    )
    def search_data_assets(query):
        """Search governed data assets via adapter."""
        results = adapters.search_governed_data(query or "")
        if not results:
            return html.P("No matching data assets found.", style={"color": "#6b7283", "fontSize": 13})
        return [data_asset_card(a) for a in results[:6]]

    @app.callback(
        Output("workflow-preview-container", "children"),
        Input("audit-objective", "value"),
        prevent_initial_call=False,
    )
    def render_workflow_preview(objective):
        """Show the agent plan preview stages."""
        plan = adapters.propose_plan({"mode": "playbook", "skill": adapters.get_skill(_DEFAULT_SKILL_ID), "sources_count": 3})
        stages = plan.get("stages", [])
        total = len(stages)
        return html.Div([
            html.Div(
                [workflow_stage(s, i, total) for i, s in enumerate(stages)],
                style={"padding": "12px 0"},
            ),
            demo_indicator("Preview only — workflow has not been executed") if plan.get("mock") else None,
        ], className="panel")

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
        prevent_initial_call=True,
    )
    def start_run(n_clicks, objective, start_date, end_date, business_unit, materiality, options):
        """Starts a real audit run (CLAUDE.md §2.1: start_audit_run only
        inserts the `runs` row and hands off to the executor).

        DEVIATION from the prototype: `start_demo_run` always navigated to
        the fixed "/workspace/tne" dashboard, because it never started a
        real run -- there was nothing to wait for. A real run is not
        complete the instant it is created (it must clear the mandatory
        plan-confirmation and findings-sign-off gates, CLAUDE.md §2.4
        non-negotiable 3), so this navigates to the run's own status page
        instead. See the change report for the alternative considered."""
        if not n_clicks:
            raise PreventUpdate

        options = options or []
        bindings, missing = _auto_bind(_DEFAULT_SKILL_ID)
        if missing:
            return no_update, html.Div([
                html.Div([
                    html.Span("Mode", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                              "color": "#6b7283", "letterSpacing": "0.03em"}),
                    html.Div(
                        "Could not start the run: no governed table or uploaded file matches "
                        f"contract source(s) {', '.join(missing)} by exact name.",
                        style={"fontSize": 13, "fontWeight": 600, "color": "#b85042"},
                    ),
                ]),
            ], className="panel", style={"marginTop": 12})

        try:
            run_owner = _request_owner()
            run_id = adapters.start_audit_run(
                skill_id=_DEFAULT_SKILL_ID,
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
        except Exception as exc:  # NN14: fail loudly and visibly, never a silent default
            return no_update, html.Div([
                html.Div([
                    html.Span("Mode", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                              "color": "#6b7283", "letterSpacing": "0.03em"}),
                    html.Div(f"Could not start the run: {exc}",
                             style={"fontSize": 13, "fontWeight": 600, "color": "#b85042"}),
                ]),
            ], className="panel", style={"marginTop": 12})

        return f"/run/{run_id}", no_update
