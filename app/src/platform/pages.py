"""Platform page layouts for AI Audit Analyst.

Each function returns a Dash layout (html.Div tree).  Callbacks that
wire up interactivity live in app.py alongside the existing T&E callbacks.
"""

from __future__ import annotations

from dash import dcc, html
import dash_bootstrap_components as dbc

from orchestrator.service import cross_run_totals
from src.platform import adapters
from src.platform.components import (
    demo_indicator,
    env_badge,
    kpi_card,
    run_card,
    skill_card,
    trace_event_row,
    action_row,
)


# ─── Global application shell ───────────────────────────────────────────────

def platform_header() -> html.Div:
    """Reusable header for the AI Audit Analyst platform."""
    return html.Div(
        html.Header([
            html.Div([
                html.Div([
                    html.H1("AI Audit Analyst", className="plat-logo"),
                    html.P("Evidence-linked audit analytics for Internal Audit", className="plat-tagline"),
                ]),
                html.Div([
                    env_badge(adapters.get_environment_label()),
                    html.Span("Demo user", className="chip", style={"fontSize": 11, "color": "#6b7283"}),
                ], style={"display": "flex", "gap": 8, "alignItems": "center"}),
            ], style={"display": "flex", "justifyContent": "space-between", "alignItems": "flex-start"}),
        ], className="plat-header"),
        className="shell",
    )


def platform_nav(active: str = "/") -> html.Nav:
    """Primary navigation strip."""
    links = [
        ("/", "Start an Audit"),
        ("/workspace/tne", "T&E Showcase"),
        ("/runs", "Audit Runs"),
        ("/skills", "Skill Library"),
        ("/actions", "Management Actions"),
        ("/trace", "Platform Trace"),
    ]
    items = []
    for href, label in links:
        is_active = (href == active)
        items.append(
            dcc.Link(
                label,
                href=href,
                className="plat-nav-link plat-nav-active" if is_active else "plat-nav-link",
            )
        )
    return html.Nav(
        html.Div(items, className="plat-nav-strip"),
        className="shell",
    )


# ─── Skill Library page ─────────────────────────────────────────────────────

def skill_library_page() -> html.Div:
    skills = adapters.list_skills()
    domains = sorted(set(s.get("domain", "") for s in skills))
    statuses = sorted(set(s.get("status", "") for s in skills))

    return html.Div([
        html.Div([
            html.H2("Skill Library", className="page-title"),
            html.P("Governed audit methodologies — searchable, versioned, reusable", className="page-subtitle"),
        ], className="page-header"),

        demo_indicator() if adapters.is_demo_mode() else None,

        html.Div([
            dcc.Input(id="skill-search", type="text", placeholder="Search skills…",
                      debounce=True, style={"flex": 2, "padding": "8px 12px", "borderRadius": 7,
                                            "border": "1px solid #cfd5e2", "fontSize": 13}),
            dcc.Dropdown(id="skill-domain-filter",
                         options=[{"label": d, "value": d} for d in domains],
                         placeholder="Domain", style={"flex": 1, "fontSize": 13}),
            dcc.Dropdown(id="skill-status-filter",
                         options=[{"label": s, "value": s} for s in statuses],
                         placeholder="Status", style={"flex": 1, "fontSize": 13}),
        ], className="filter-row"),

        html.Div(
            [skill_card(s) for s in skills],
            className="plat-skill-grid",
            id="skill-library-grid",
        ),

        html.Div([
            html.Button("Start Explorer Mode", id="start-explorer-from-library-btn", className="ghost",
                        style={"width": "auto", "marginTop": 16}),
        ], style={"textAlign": "center"}),
    ], className="shell dashboard-shell")


# ─── Audit Runs page ────────────────────────────────────────────────────────

def audit_runs_page() -> html.Div:
    runs = adapters.list_audit_runs()
    skills_for_filter = sorted(set(r.get("skill_name", "") for r in runs))

    # Independent review 2026-09-24 gap #6: this used to sum high_risk_count
    # over EVERY run -- re-runs of the same Skill/period alongside their
    # predecessors, plus failed/queued/running runs that never produced a
    # trustworthy finding set. cross_run_totals applies the one rule shared
    # with /actions' exposure figure (orchestrator.service.cross_run_totals).
    high_risk_findings_total = cross_run_totals(runs)["high_risk_findings_total"]

    return html.Div([
        html.Div([
            html.H2("Audit Runs", className="page-title"),
            html.P("Persistent audit executions across all Skills and periods", className="page-subtitle"),
        ], className="page-header"),

        demo_indicator() if adapters.is_demo_mode() else None,

        # Summary KPIs
        html.Div([
            kpi_card("Total runs", str(len(runs))),
            kpi_card("Completed", str(sum(1 for r in runs if r["status"] == "Completed"))),
            kpi_card("High-risk findings", str(high_risk_findings_total)),
            kpi_card("Open actions", str(sum(r.get("open_actions", 0) for r in runs))),
        ], className="plat-kpi-row", style={"marginBottom": 16}),

        html.Div([
            dcc.Dropdown(id="runs-skill-filter",
                         options=[{"label": s, "value": s} for s in skills_for_filter],
                         placeholder="Filter by Skill", style={"flex": 1, "fontSize": 13}),
            dcc.Dropdown(id="runs-status-filter",
                         options=[{"label": s, "value": s} for s in ["Running", "Completed", "Failed", "Needs review"]],
                         placeholder="Status", style={"flex": 1, "fontSize": 13}),
        ], className="filter-row"),

        html.Div(
            [run_card(r) for r in runs],
            className="stack",
            id="runs-list",
        ),
    ], className="shell dashboard-shell")


# ─── Platform Trace page ────────────────────────────────────────────────────

def platform_trace_page() -> html.Div:
    runs = adapters.list_audit_runs()
    events = adapters.list_trace_events()

    run_options = [{"label": f"{r['run_id']} — {r.get('skill_name', '')}", "value": r["run_id"]} for r in runs]

    return html.Div([
        html.Div([
            html.H2("Platform Trace", className="page-title"),
            html.P("Observable execution events — no chain-of-thought reasoning", className="page-subtitle"),
        ], className="page-header"),

        demo_indicator() if adapters.is_demo_mode() else None,

        html.Div([
            dcc.Dropdown(id="trace-run-filter", options=run_options,
                         placeholder="Filter by run", style={"flex": 1, "fontSize": 13}),
        ], className="filter-row"),

        html.Div([
            html.Table([
                html.Thead(html.Tr([
                    html.Th("Timestamp"),
                    html.Th("Stage"),
                    html.Th("Status"),
                    html.Th("Message"),
                    html.Th("Duration", style={"textAlign": "right"}),
                ])),
                html.Tbody(
                    [trace_event_row(e) for e in events],
                    id="trace-events-body",
                ),
            ], className="plat-table"),
        ], className="panel", style={"overflowX": "auto"}),
    ], className="shell dashboard-shell")


# ─── Management Actions page (cross-Skill) ──────────────────────────────────

def management_actions_page() -> html.Div:
    actions = adapters.list_management_actions()
    skills_for_filter = sorted(set(a.get("skill_name", "") for a in actions))

    # CLAUDE.md §0.3: findings' potential_exposure figures overlap the same
    # underlying spend (a claim can be cited by more than one finding), so
    # summing them here would double-count exactly the way the exec-brief
    # figure once did. Each eligible run already carries its own
    # de-duplicated headline (run_exposure_headline, one number per run,
    # see orchestrator/service.py list_runs "potential_exposure") -- sum
    # that, once per run, instead of once per finding.
    #
    # Independent review 2026-09-24 gap #6: a re-run of the SAME Skill over
    # the SAME engagement and audit period is the same underlying population
    # re-tested, not additional exposure (item 2), and two runs whose
    # periods merely OVERLAP without being identical are still the same
    # spend tested twice under two windows -- summing them double-counts it
    # just the same. orchestrator.service.cross_run_totals applies
    # the one rule shared with /runs' "High-risk findings" KPI: eligible,
    # non-superseded, latest-per-(skill, engagement, period), and never
    # summed across overlapping periods -- the latest run's own figure (or
    # "—", never a fabricated $0) stands in when it can't state an honest
    # total.
    total_exposure = cross_run_totals(adapters.list_audit_runs())["total_exposure"]
    # "Under Review" (title-cased) is what orchestrator.service.
    # list_management_actions() actually produces from the persisted
    # "under_review" status (str.replace("_", " ").title()) -- the prototype's
    # own literal, lowercase-r "Under review" never matches it, so this count
    # was always 0 on real data (CLAUDE.md NN14; independent review 2026-09-24
    # item 3).
    open_count = sum(1 for a in actions if a.get("status") in ("Open", "Under Review"))
    high_count = sum(1 for a in actions if a.get("risk") == "High")

    return html.Div([
        html.Div([
            html.H2("Management Actions", className="page-title"),
            html.P("Cross-Skill action tracker — all findings, all runs", className="page-subtitle"),
        ], className="page-header"),

        demo_indicator() if adapters.is_demo_mode() else None,

        html.Div([
            kpi_card("Total actions", str(len(actions))),
            kpi_card("Open / Under review", str(open_count)),
            kpi_card("High risk", str(high_count)),
            kpi_card("Total exposure", f"${total_exposure:,.0f}" if total_exposure is not None else "—"),
        ], className="plat-kpi-row", style={"marginBottom": 16}),

        html.Div([
            dcc.Dropdown(id="actions-skill-filter",
                         options=[{"label": s, "value": s} for s in skills_for_filter],
                         placeholder="Filter by Skill", style={"flex": 1, "fontSize": 13}),
            dcc.Dropdown(id="actions-risk-filter",
                         options=[{"label": r, "value": r} for r in ["High", "Medium", "Low"]],
                         placeholder="Risk level", style={"flex": 1, "fontSize": 13}),
            dcc.Dropdown(id="actions-status-filter",
                         options=[{"label": s, "value": s} for s in ["Open", "Under review", "Agreed", "Remediated", "Closed"]],
                         placeholder="Status", style={"flex": 1, "fontSize": 13}),
        ], className="filter-row"),

        html.Div([
            html.Table([
                html.Thead(html.Tr([
                    html.Th("Finding"),
                    html.Th("Skill"),
                    html.Th("Risk"),
                    html.Th("Owner"),
                    html.Th("Status"),
                    html.Th("Target date"),
                    html.Th("Exposure"),
                    html.Th("Evidence"),
                ])),
                html.Tbody(
                    [action_row(a) for a in actions],
                    id="actions-table-body",
                ),
            ], className="plat-table"),
        ], className="panel", style={"overflowX": "auto"}),
    ], className="shell dashboard-shell")


# ─── Skill Methodology Viewer ───────────────────────────────────────────────

def _method_section_nav(active: str) -> html.Div:
    """Left rail — anchor navigation for methodology sections."""
    sections = [
        ("overview", "Overview"),
        ("data-sources", "Data sources"),
        ("test-catalogue", "Test catalogue"),
        ("risk-scoring", "Risk scoring"),
        ("outputs", "Outputs & evidence"),
        ("history", "Version history"),
    ]
    items = []
    for anchor, label in sections:
        items.append(html.A(label, href=f"#{anchor}",
                             className="method-nav-link " + ("active" if anchor == active else "")))
    return html.Div(items, className="method-nav-rail")


def _method_test_row(test: dict) -> html.Tr:
    return html.Tr([
        html.Td(test["test_id"], className="mono",
                style={"fontWeight": 700, "color": "#1e2761", "whiteSpace": "nowrap"}),
        html.Td([
            html.Div(test["test_name"], style={"fontWeight": 600, "marginBottom": 4}),
            html.Div(test["control_objective"], style={"fontSize": 11.5, "color": "#6b7283"}),
        ]),
        html.Td(test["population"], style={"fontSize": 12}),
        html.Td(test["rule"], style={"fontSize": 12}),
        html.Td(test["threshold"], style={"fontSize": 12, "whiteSpace": "nowrap"}),
    ])


def _tne_methodology_body(m: dict) -> list:
    """Full T&E methodology content (real, pulled from live catalogue)."""
    from src.platform.components import kpi_card

    sections = []

    # ── Overview ───────────────────────────────────────
    sections.append(html.Section([
        html.A(id="overview"),
        html.H3("Overview", className="method-h3"),
        html.P(m["purpose"], className="method-para"),
        html.Div([
            kpi_card("Version", f"v{m['version']}"),
            kpi_card("Tests", str(m["total_tests"])),
            kpi_card("Data sources", str(len(m["data_sources"]))),
            kpi_card("Categories", str(len(m["categories"]))),
        ], className="plat-kpi-row", style={"marginTop": 12}),
        html.Div([
            html.Div([
                html.Span("Population", className="method-label"),
                html.Div(m["population"], style={"fontSize": 13, "color": "#3b4150"}),
            ]),
        ], style={"marginTop": 14}),
    ], className="method-section"))

    # ── Data sources ───────────────────────────────────
    src_rows = []
    for s in m["data_sources"]:
        rows = f"{s['expected_rows']:,} rows" if s.get("expected_rows") else "—"
        src_rows.append(html.Tr([
            html.Td(s["key"], className="mono", style={"fontWeight": 700, "color": "#1e2761"}),
            html.Td(s["filename"], style={"fontSize": 12, "fontFamily": "ui-monospace, monospace"}),
            html.Td(s.get("sheet") or "(first sheet)", style={"fontSize": 12, "color": "#6b7283"}),
            html.Td(rows, style={"fontSize": 12, "textAlign": "right", "whiteSpace": "nowrap"}),
        ]))
    sections.append(html.Section([
        html.A(id="data-sources"),
        html.H3("Data sources", className="method-h3"),
        html.P("Files consumed by the Skill. Each test cites which of these it draws from.",
               className="method-para"),
        html.Div([
            html.Table([
                html.Thead(html.Tr([
                    html.Th("Key"), html.Th("Filename"),
                    html.Th("Sheet"), html.Th("Expected rows", style={"textAlign": "right"}),
                ])),
                html.Tbody(src_rows),
            ], className="plat-table"),
        ], className="panel", style={"overflowX": "auto"}),
    ], className="method-section"))

    # ── Test catalogue ─────────────────────────────────
    cat_blocks = []
    for cat in m["categories"]:
        tests = m["tests_by_category"].get(cat, [])
        if not tests:
            continue
        cat_blocks.append(html.Div([
            html.Div([
                html.Span(cat, style={"fontWeight": 700, "fontSize": 13, "color": "#1a1d26"}),
                html.Span(f"{len(tests)} tests", className="chip",
                          style={"fontSize": 10, "marginLeft": 8, "color": "#6b7283"}),
            ], style={"marginBottom": 8, "display": "flex", "alignItems": "center"}),
            html.Div([
                html.Table([
                    html.Thead(html.Tr([
                        html.Th("ID", style={"width": 70}),
                        html.Th("Test"),
                        html.Th("Population"),
                        html.Th("Rule"),
                        html.Th("Threshold"),
                    ])),
                    html.Tbody([_method_test_row(t) for t in tests]),
                ], className="plat-table"),
            ], className="panel", style={"overflowX": "auto", "marginBottom": 16}),
        ]))
    sections.append(html.Section([
        html.A(id="test-catalogue"),
        html.H3("Test catalogue", className="method-h3"),
        html.P("The complete set of deterministic tests, grouped by control category. "
               "Each test has an explicit population, rule, and threshold — no black-box logic.",
               className="method-para"),
        html.Div(cat_blocks),
    ], className="method-section"))

    # ── Risk scoring ───────────────────────────────────
    scoring_items = []
    for label, desc in m["risk_scoring"].items():
        color = {"High": "#b85042", "Medium": "#e0952a", "Low": "#2c7a4b"}.get(label, "#6b7283")
        scoring_items.append(html.Div([
            html.Span(label, style={"color": color, "fontWeight": 700, "fontSize": 13,
                                     "minWidth": 60, "display": "inline-block"}),
            html.Span(desc, style={"fontSize": 13, "color": "#3b4150"}),
        ], style={"padding": "8px 0", "borderBottom": "1px solid #e4e7ee"}))
    sections.append(html.Section([
        html.A(id="risk-scoring"),
        html.H3("Risk scoring", className="method-h3"),
        html.P("Findings are classified by risk band. The rule is deterministic and "
               "encoded in the Skill definition — the same input always produces the same score.",
               className="method-para"),
        html.Div(scoring_items, className="panel"),
    ], className="method-section"))

    # ── Outputs & evidence ─────────────────────────────
    sections.append(html.Section([
        html.A(id="outputs"),
        html.H3("Outputs & evidence traceability", className="method-h3"),
        html.P("Every run produces these outputs. Every metric is source-cited so the "
               "reader can trace any number back to its originating file.",
               className="method-para"),
        html.Div([
            html.Ul([html.Li(o, style={"marginBottom": 6, "fontSize": 13, "color": "#3b4150"})
                     for o in m["outputs"]], style={"paddingLeft": 20, "margin": 0}),
        ], className="panel"),
        html.Div([
            html.Span("Evidence traceability", className="method-label"),
            html.Div(m["evidence_traceability"], style={"fontSize": 13, "color": "#3b4150"}),
        ], style={"marginTop": 14}),
    ], className="method-section"))

    # ── Version history ────────────────────────────────
    # Read from the real skill_versions ledger (orchestrator.service.
    # list_skill_versions) -- never fabricated dates. The ledger records
    # status/created_by, not a free-text change narrative, so the "Change"
    # column (same header as the prototype) is composed from those two real
    # fields rather than inventing prose.
    hist_rows = []
    for v in m["version_history"]:
        hist_rows.append(html.Tr([
            html.Td(f"v{v['version']}", className="mono",
                    style={"fontWeight": 700, "color": "#1e2761", "whiteSpace": "nowrap"}),
            html.Td(v["date"], style={"fontSize": 12, "color": "#6b7283", "whiteSpace": "nowrap"}),
            html.Td(f"{v.get('status', '')} — recorded by {v.get('created_by', '')}",
                    style={"fontSize": 12.5, "color": "#3b4150"}),
        ]))
    sections.append(html.Section([
        html.A(id="history"),
        html.H3("Version history", className="method-h3"),
        html.P("Every change to the Skill methodology is versioned and audit-logged.",
               className="method-para"),
        html.Div([
            html.Table([
                html.Thead(html.Tr([html.Th("Version"), html.Th("Date"), html.Th("Change")])),
                html.Tbody(hist_rows),
            ], className="plat-table"),
        ], className="panel", style={"overflowX": "auto"}),
    ], className="method-section"))

    return sections


def _stub_methodology_body(m: dict) -> list:
    """Placeholder content for Skills not yet implemented."""
    sections = []

    sections.append(html.Section([
        html.A(id="overview"),
        html.H3("Overview", className="method-h3"),
        html.Div([
            html.Span("Methodology under development", className="chip",
                      style={"background": "#fffbf0", "color": "#6b4a00",
                             "fontWeight": 700, "border": "none", "fontSize": 11}),
        ], style={"marginBottom": 10}),
        html.P(m.get("purpose", ""), className="method-para"),
        html.Div([
            html.Span(f"Domain: {m.get('domain', '—')}", className="chip",
                      style={"fontSize": 11, "marginRight": 6, "color": "#6b7283"}),
            html.Span(f"Owner: {m.get('owner', '—')}", className="chip",
                      style={"fontSize": 11, "marginRight": 6, "color": "#6b7283"}),
            html.Span(f"Status: {m.get('status', '—')}", className="chip",
                      style={"fontSize": 11, "color": "#6b7283"}),
        ], style={"marginTop": 12}),
    ], className="method-section"))

    sections.append(html.Section([
        html.A(id="test-catalogue"),
        html.H3("Planned test catalogue", className="method-h3"),
        html.P("The tests below are the target set for this Skill. They are not yet "
               "implemented in the platform.", className="method-para"),
        html.Div([
            html.Ul([html.Li(t, style={"marginBottom": 6, "fontSize": 13, "color": "#3b4150"})
                     for t in m.get("planned_tests", [])] or
                    [html.Li("No planned tests recorded yet.", style={"color": "#6b7283"})],
                    style={"paddingLeft": 20, "margin": 0}),
        ], className="panel"),
    ], className="method-section"))

    sections.append(html.Section([
        html.A(id="data-sources"),
        html.H3("Planned data sources", className="method-h3"),
        html.Div([
            html.Ul([html.Li(s, style={"marginBottom": 6, "fontSize": 13, "color": "#3b4150"})
                     for s in m.get("planned_sources", [])] or
                    [html.Li("No planned sources recorded yet.", style={"color": "#6b7283"})],
                    style={"paddingLeft": 20, "margin": 0}),
        ], className="panel"),
    ], className="method-section"))

    sections.append(html.Section([
        html.A(id="outputs"),
        html.H3("Outputs & evidence", className="method-h3"),
        html.P("Once implemented, this Skill will use the same output specification as "
               "any other Skill in the platform: findings register, exec brief, "
               "PowerPoint/Excel exports, and management action tracker. Each metric "
               "will be source-cited for full evidence traceability.",
               className="method-para"),
    ], className="method-section"))

    return sections


def skill_methodology_page(skill_id: str) -> html.Div:
    """Full methodology viewer for a single Skill."""
    from src.platform.methodology import get_methodology

    m = get_methodology(skill_id)
    is_stub = m.get("is_stub", False)

    if is_stub:
        body_sections = _stub_methodology_body(m)
    else:
        body_sections = _tne_methodology_body(m)

    # Header block
    status_styles = {
        "Published": {"background": "#eaf5ee", "color": "#2c7a4b"},
        "Draft": {"background": "#f0f1f4", "color": "#6b7283"},
        "Needs review": {"background": "#fffbf0", "color": "#6b4a00"},
    }
    st = m.get("status", "Draft")
    st_style = status_styles.get(st, status_styles["Draft"])

    header = html.Div([
        dcc.Link("← Back to Skill Library", href="/skills",
                 className="method-back-link"),
        html.Div([
            html.Div([
                html.Span(m["skill_id"], className="chip mono",
                          style={"fontSize": 10, "color": "#6b7283"}),
                html.Span(st, className="chip",
                          style={**st_style, "fontSize": 10, "border": "none", "fontWeight": 700}),
                html.Span(f"v{m.get('version', '?')}", className="chip mono",
                          style={"fontSize": 10, "color": "#6b7283"}),
                html.Span(m.get("domain", ""), className="chip",
                          style={"fontSize": 10, "color": "#6b7283"}),
            ], style={"display": "flex", "gap": 6, "flexWrap": "wrap", "marginBottom": 8}),
            html.H2(m["name"], className="page-title", style={"marginBottom": 4}),
            html.P(f"Owner: {m.get('owner', '—')} · Last updated: {m.get('last_updated', '—')}",
                   className="page-subtitle"),
        ], className="page-header"),
    ])

    return html.Div([
        header,
        html.Div([
            _method_section_nav("overview"),
            html.Div(body_sections, className="method-content"),
        ], className="method-layout"),
    ], className="shell dashboard-shell")



