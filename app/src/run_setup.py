"""Home — the run-setup page (CLAUDE.md: pick SKILL-001, bind its sources to
Unity Catalog tables, type an objective, click "Run audit analysis").

Nothing here fabricates a default: the audit period is unset until the
auditor picks it (only shown as placeholder text, CLAUDE.md §0.2), a missing
binding blocks the run, and the objective is required. `start_audit_run`
itself runs in a callback but only inserts a `runs` row and hands off to the
executor (CLAUDE.md §2.1) — it does not execute a node.
"""

from __future__ import annotations

from dash import ALL, Input, Output, State, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import request

from src.platform import adapters

_DEFAULT_SKILL_ID = "SKILL-001"


def _request_owner() -> str:
    """Verified-enough identity for a demo/pilot deployment: the identity
    header set by the platform's front door. CLAUDE.md §9A Q1 leaves the
    real verified-identity question open for the corporate migration; until
    then this is the best available run_owner and is never silently
    defaulted to an empty string."""
    owner = request.headers.get("X-Forwarded-Email") or request.headers.get("X-Forwarded-User")
    return owner or "local-user"


def _table_fqn(table: dict) -> str:
    # orchestrator.service.list_governed_tables rows use "table_fqn"; the
    # task brief's contract sketch used "fqn" — accept either so this
    # doesn't silently show an empty dropdown if that shape changes again.
    return table.get("table_fqn") or table.get("fqn") or ""


def _binding_row(source_name: str, columns: list, tables: list[dict], suggested: dict) -> html.Div:
    options = []
    for t in tables:
        fqn = _table_fqn(t)
        restricted = bool(t.get("restricted"))
        label = fqn + (" — Restricted" if restricted else "")
        if t.get("exists") is False:
            label += " (not found)"
        options.append({"label": label, "value": fqn, "disabled": restricted})

    suggested_fqn = suggested.get(source_name)
    valid_fqns = {_table_fqn(t) for t in tables if not t.get("restricted")}
    value = suggested_fqn if suggested_fqn in valid_fqns else None

    col_hint = f"{len(columns)} columns expected" if isinstance(columns, list) else ""

    return html.Div([
        html.Div([
            html.Span(source_name, style={"fontWeight": 700, "fontSize": 13, "color": "#1a1d26"}),
            html.Span(col_hint, style={"fontSize": 11, "color": "#6b7283", "marginLeft": 8}),
        ], style={"marginBottom": 4}),
        dcc.Dropdown(
            id={"type": "home-binding", "index": source_name},
            options=options,
            value=value,
            placeholder="Select a governed table…",
            style={"fontSize": 13},
        ),
    ], style={"marginBottom": 14})


def home_layout() -> html.Div:
    skills = adapters.list_skills()
    default_skill = next((s for s in skills if s.get("skill_id") == _DEFAULT_SKILL_ID), None)
    default_skill_id = default_skill["skill_id"] if default_skill else (skills[0]["skill_id"] if skills else None)

    return html.Div([
        html.Div([
            html.Div("AI AUDIT ANALYST", className="showcase-eyebrow"),
            html.H2("Start an audit analysis", className="showcase-headline"),
            html.P(
                "Pick a Skill, bind each of its data sources to a governed Unity Catalog table, "
                "set the audit period and objective, then run the audit.",
                className="showcase-supporting",
            ),
        ], className="showcase-hero"),

        html.Div([
            html.H3("Skill", style={"margin": "0 0 4px"}),
            html.P("The governed audit methodology this run will execute.", className="sub"),
            dcc.Dropdown(
                id="home-skill",
                options=[{"label": f"{s['skill_id']} — {s['name']}", "value": s["skill_id"]} for s in skills],
                value=default_skill_id,
                clearable=False,
                style={"maxWidth": 480, "fontSize": 13},
            ),
        ], className="panel", style={"marginBottom": 16}),

        html.Div([
            html.H3("Data sources", style={"margin": "0 0 4px"}),
            html.P("Every source the Skill's contract requires. A missing binding blocks the run.",
                   className="sub"),
            html.Div(id="home-bindings-container"),
        ], className="panel", style={"marginBottom": 16}),

        html.Div([
            html.H3("Audit configuration", style={"margin": "0 0 4px"}),
            html.P("Set the scope and objective for this run.", className="sub"),
            html.Div([
                html.Label("Audit period", style={"fontSize": 12, "fontWeight": 700, "color": "#1a1d26"}),
                html.Div(
                    dcc.DatePickerRange(
                        id="home-period",
                        start_date_placeholder_text="e.g. 2025-01-01",
                        end_date_placeholder_text="e.g. 2026-04-30",
                        display_format="DD MMM YYYY",
                    ),
                ),
            ], style={"marginBottom": 14}),
            html.Div([
                html.Label("Audit objective", style={"fontSize": 12, "fontWeight": 700, "color": "#1a1d26"}),
                dcc.Textarea(
                    id="home-objective",
                    placeholder="What is this audit assessing? e.g. Assess the selected population for "
                                "control exceptions and quantify the potential exposure.",
                    style={"width": "100%", "height": 80, "fontSize": 13, "borderRadius": 7,
                           "border": "1px solid #cfd5e2", "padding": "8px 12px", "resize": "vertical"},
                ),
            ], style={"marginBottom": 14}),
            dcc.Checklist(
                id="home-review-plan",
                options=[{"label": " Review plan before executing (Explorer requires this; optional in Playbook)",
                          "value": "review_plan_first"}],
                value=[],
                style={"fontSize": 13, "color": "#3b4150"},
            ),
        ], className="panel", style={"marginBottom": 16}),

        html.Div(id="home-validation-errors", style={"color": "#b85042", "fontSize": 13, "marginBottom": 8}),

        html.Div([
            html.Button("Run audit analysis", id="home-start-btn", className="btn-generate",
                        style={"fontSize": 15, "fontWeight": 700, "padding": "12px 32px", "width": "100%", "maxWidth": 360}),
        ], style={"textAlign": "center"}),

        dcc.Store(id="home-skill-sources"),
    ], className="shell dashboard-shell plat-landing")


def validate_form(skill_id, bindings: dict, start_date, end_date, objective) -> list[str]:
    """Pure validation, factored out of the callback so it's directly
    testable: no dependency on Dash's callback dispatcher."""
    errors = []
    if not skill_id:
        errors.append("Select a Skill.")
    missing = [name for name, value in bindings.items() if not value]
    if missing:
        errors.append(f"Bind every data source before running: missing {', '.join(missing)}.")
    if not start_date or not end_date:
        errors.append("Set both the audit period start and end date.")
    if not objective or not objective.strip():
        errors.append("Enter an audit objective.")
    return errors


def register_callbacks(app) -> None:

    @app.callback(
        Output("home-bindings-container", "children"),
        Output("home-skill-sources", "data"),
        Input("home-skill", "value"),
    )
    def _rebuild_bindings(skill_id):
        if not skill_id:
            return [], []
        skill = adapters.get_skill(skill_id)
        if not skill:
            return [html.P(f"Skill {skill_id} not found.", style={"color": "#b85042"})], []

        # skill["sources"] (orchestrator.service.get_skill) is a list of
        # {"source": name, "columns": [str, ...]}.
        items = [(s.get("source"), s.get("columns") or []) for s in (skill.get("sources") or [])]

        try:
            tables = adapters.list_governed_tables()
        except Exception as exc:
            return [html.P(f"Could not list governed tables: {exc}", style={"color": "#b85042"})], []
        suggested = adapters.suggest_bindings(skill_id) or {}

        rows = [_binding_row(name, columns, tables, suggested) for name, columns in items]
        if not rows:
            rows = [html.P("This Skill's contract declares no sources.", style={"color": "#6b7283"})]
        return rows, [name for name, _ in items]

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("home-validation-errors", "children"),
        Input("home-start-btn", "n_clicks"),
        State("home-skill", "value"),
        State({"type": "home-binding", "index": ALL}, "value"),
        State({"type": "home-binding", "index": ALL}, "id"),
        State("home-period", "start_date"),
        State("home-period", "end_date"),
        State("home-objective", "value"),
        State("home-review-plan", "value"),
        prevent_initial_call=True,
    )
    def _start_run(n_clicks, skill_id, binding_values, binding_ids, start_date, end_date, objective, review_plan):
        if not n_clicks:
            raise PreventUpdate

        bindings = {}
        for bid, value in zip(binding_ids or [], binding_values or []):
            bindings[bid["index"]] = value
        errors = validate_form(skill_id, bindings, start_date, end_date, objective)

        if errors:
            return no_update, html.Ul([html.Li(e) for e in errors])

        run_owner = _request_owner()
        try:
            run_id = adapters.start_audit_run(
                skill_id=skill_id,
                bindings=bindings,
                audit_period=(start_date, end_date),
                objective=objective.strip(),
                run_owner=run_owner,
                mode="playbook",
                review_plan_first=bool(review_plan and "review_plan_first" in review_plan),
            )
        except Exception as exc:  # NN14: fail loudly and visibly, never a silent default
            return no_update, html.Ul([html.Li(f"Could not start the run: {exc}")])
        return f"/run/{run_id}", ""
