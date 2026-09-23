"""/workspace/tne — the T&E ExCo workspace, rebuilt on the service API.

CLAUDE.md §3 NN15 requires this workspace to stay *functionally* identical on
the same data, not bit-for-bit: every number here comes from a completed
run's persisted results (RunState.findings / test_results, plus
get_run_payload / get_run_frames), never recomputed or invented in this
module (NN2, NN14). There are no module-level data globals — everything is
fetched per request and kept in a small cache keyed by (run_id,
state_version), as CLAUDE.md §2.1 allows for chart-filtering callbacks.

Two things the prototype did that this module does not reproduce, and why:
  * The reference app built its 15 analytics figures (and the findings list)
    once at import time from files loaded straight into module globals
    (DF_COMBINED, DF_PRE, DF_APPROVAL, FINDINGS_SCORED, RUN_METADATA...).
    CLAUDE.md §3 NN14 and P4's "no module-level globals" forbid that shape
    outright, so every figure and every finding below is built inside a
    request-scoped function from a run's persisted results.
  * `financial_exposure` / `risk_score` do not exist yet on a finding
    (`orchestrator.findings.build_findings` sets `exposure_amount: None`,
    "pending P3 de-duplicated exposure" -- P3 is not done). This module
    never fabricates that number: it says so on the card instead of summing
    a column of Nones or reusing the prototype's double-counted figure
    (CLAUDE.md §0.3).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from src.platform import adapters

# ── Per-run cache — CLAUDE.md §2.1: "a small per-run cache keyed by
# (run_id, state_version) is fine". Holds at most one run's bundle: a
# workspace tab is only ever looking at one run_id at a time. ─────────────────
_CACHE: dict[tuple, dict] = {}

_SEVERITY_COLOR = {"High": "#b85042", "Medium": "#e0952a", "Low": "#2c7a4b"}

_EXPENSE_FRAME_CANDIDATES = ["expense_report", "expense", "combined"]
_PRE_APPROVAL_FRAME_CANDIDATES = ["travel_requests_no_expense", "pre_approval", "preapproval"]
_APPROVAL_FRAME_CANDIDATES = ["approval_aging", "approval"]


def _pick_frame(frames: dict, candidates: list[str]) -> pd.DataFrame | None:
    for name in candidates:
        df = frames.get(name)
        if df is not None:
            return df
    return None


def _load_bundle(run_id: str) -> dict | None:
    run = adapters.get_run(run_id)
    if not run:
        return None
    version = run.get("state_version", 0)
    key = (run_id, version)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        payload = adapters.get_run_payload(run_id) or {}
    except Exception:
        payload = {}
    try:
        frames = adapters.get_run_frames(run_id) or {}
    except Exception:
        frames = {}
    skill = adapters.get_skill(run.get("skill_id")) if run.get("skill_id") else None

    bundle = {"run": run, "payload": payload, "frames": frames, "skill": skill or {}}
    _CACHE.clear()
    _CACHE[key] = bundle
    return bundle


def latest_completed_run_id() -> str | None:
    runs = adapters.list_audit_runs()
    completed = [r for r in runs if str(r.get("status", "")).lower() == "completed"]
    if not completed:
        return None
    completed.sort(key=lambda r: r.get("last_updated") or r.get("run_timestamp") or "", reverse=True)
    return completed[0]["run_id"]


def _empty_state() -> html.Div:
    return html.Div([
        html.Div([
            html.H2("No completed run yet", className="page-title"),
            html.P("Start one from Home — pick SKILL-001, bind its sources and run the audit.",
                   className="page-subtitle"),
            dcc.Link("Go to Home", href="/", className="btn-generate",
                     style={"display": "inline-block", "width": "auto", "padding": "10px 24px", "marginTop": 12}),
        ], className="showcase-hero"),
    ], className="shell dashboard-shell")


def _fmt_source_ref(source_ref) -> str:
    """source_ref (orchestrator.primitives.common._base_source_ref) is
    {"sources": [{"name","version"}], "columns": [...], "grain", "population"} —
    render it as "source@version (grain)" rather than a raw dict repr."""
    if not isinstance(source_ref, dict):
        return "—"
    sources = source_ref.get("sources") or []
    names = ", ".join(f"{s.get('name')}@{s.get('version')}" for s in sources if s.get("name"))
    grain = source_ref.get("grain")
    return f"{names} ({grain})" if names and grain else (names or "—")


def _fmt_metric(value, unit) -> str:
    if value is None:
        return "n/a"
    if unit == "AUD":
        return f"${value:,.2f}" if isinstance(value, (int, float)) else str(value)
    if unit == "%":
        return f"{value}%"
    if isinstance(value, (int, float)):
        return f"{value:,}"
    return str(value)


# ── Header ────────────────────────────────────────────────────────────────

def _build_header(run: dict) -> html.Div:
    period = run.get("audit_period") or ["—", "—"]
    return html.Div(
        html.Header([
            html.Div([
                html.H1("Travel & Entertainment Executive Diligence", className="page-title"),
                html.P("Reproducible calculations · Evidence-linked findings · Management questions",
                       className="page-subtitle"),
            ]),
            html.Div([
                html.Span(f"Run: {run.get('run_id', '')}", className="chip mono"),
                html.Span(f"Skill: {run.get('skill_id', '—')} v{run.get('skill_version', '—')}", className="chip"),
                html.Span(f"Audit period: {period[0]} – {period[1]}", className="chip"),
                html.Span(f"Owner: {run.get('run_owner', '—')}", className="chip"),
                html.Span(f"Status: {run.get('status_label') or run.get('status', '—')}", className="chip"),
            ], className="chip-row", style={"marginTop": 8}),
        ], className="page-header", style={"marginBottom": 16}),
        className="shell",
    )


# ── Executive brief tab ──────────────────────────────────────────────────────

def _exposure_summary(findings: list[dict], payload: dict | None = None) -> str:
    """Prefers the run's own de-duplicated exposure headline
    (get_run_payload()["exposure"], CLAUDE.md §0.3) over summing each
    finding's own exposure_amount, which double-counts overlapping
    populations — exactly the bug §0.3 describes. Falls back to an honest
    "not yet computed" rather than either fabricating a number or reusing
    the double-counted one."""
    exposure = (payload or {}).get("exposure") or {}
    headline = exposure.get("headline")
    if headline is not None:
        basis = exposure.get("basis")
        return f"${headline:,.0f}" + (f" — {basis}" if basis else "")
    if not findings:
        return "No findings"
    amounts = [f.get("exposure_amount") for f in findings]
    if all(a is None for a in amounts):
        return "Not yet computed — pending de-duplicated exposure figure (CLAUDE.md §0.3, P3)"
    total = sum(a for a in amounts if a is not None)
    missing = sum(1 for a in amounts if a is None)
    note = f" ({missing} finding(s) still pending)" if missing else ""
    return f"${total:,.0f}{note}"


def _executive_tab(run: dict, findings: list[dict], payload: dict | None = None) -> html.Div:
    n_high = sum(1 for f in findings if f.get("severity") == "High")
    n_med = sum(1 for f in findings if f.get("severity") == "Medium")
    n_low = sum(1 for f in findings if f.get("severity") == "Low")

    return html.Div([
        html.Div([
            html.P("Internal Audit executive brief", className="showcase-eyebrow"),
            html.H2(
                f"{n_high} high-priority matter(s) across {len(findings)} finding(s)."
                if findings else "No control exceptions found in this run.",
                className="showcase-headline",
            ),
            html.P(
                f"Potential financial exposure: {_exposure_summary(findings, payload)}. "
                "Every number below is read from this run's persisted results.",
                className="showcase-supporting",
            ),
        ], className="showcase-hero"),

        html.Div([
            html.Div([html.Div("Findings", className="kpi-title"),
                      html.Div(str(len(findings)), className="kpi-value")], className="kpi-tile"),
            html.Div([html.Div("High", className="kpi-title"),
                      html.Div(str(n_high), className="kpi-value")], className="kpi-tile"),
            html.Div([html.Div("Medium", className="kpi-title"),
                      html.Div(str(n_med), className="kpi-value")], className="kpi-tile"),
            html.Div([html.Div("Low", className="kpi-title"),
                      html.Div(str(n_low), className="kpi-value")], className="kpi-tile"),
        ], className="plat-kpi-row", style={"marginBottom": 16}),

        html.Div([
            html.H3("Run signoff", style={"margin": "0 0 6px", "fontSize": 14.5, "fontWeight": 700}),
            html.P(
                (f"Signed off by {run['signoff']['approver']} at {run['signoff']['timestamp']}"
                 if run.get("signoff") else "Not yet signed off."),
                className="sub",
            ),
        ], className="panel"),
    ])


# ── Findings & Evidence tab ──────────────────────────────────────────────────

def _finding_card(idx: int, finding: dict) -> html.Article:
    severity = finding.get("severity", "—")
    color = _SEVERITY_COLOR.get(severity, "#6b7283")
    test_id = finding.get("test_id", "")
    questions = finding.get("management_questions", [])
    q1 = questions[0] if questions else ""
    exposure = finding.get("exposure_amount")

    summary_items = [
        html.Span(severity, className="chip", style={"color": color, "borderColor": color}),
        html.Span(test_id, className="chip mono", style={"color": "#6b7283"}),
    ]
    if finding.get("analyst_set_severity"):
        summary_items.append(html.Span(
            "Analyst-set threshold — pending policy confirmation", className="chip",
            style={"color": "#6b4a00", "borderColor": "#e0952a", "fontSize": 10.5},
        ))

    exposure_line = (
        f"Exposure: ${exposure:,.0f}" if exposure is not None
        else "Exposure: not yet computed (pending de-duplicated exposure figure, P3)"
    )

    detail = html.Div([
        html.P(finding.get("observation", ""),
               style={"margin": "0 0 10px", "fontSize": 13.5, "lineHeight": 1.55, "color": "#2c3040"}),
        html.Div(exposure_line, style={"marginBottom": 8, "fontSize": 11, "color": "#6b7283", "fontFamily": "monospace"}),
        html.Div([
            html.Span("Recommendation", style={"fontSize": 10.5, "fontWeight": 700, "letterSpacing": "0.5px",
                                                "color": "#1e2761", "textTransform": "uppercase",
                                                "display": "block", "marginBottom": 3}),
            finding.get("recommendation", ""),
        ], style={"fontSize": 12.5, "color": "#2c3040", "borderLeft": "2px solid #e4e7ee",
                  "paddingLeft": 10, "marginBottom": 10}) if finding.get("recommendation") else None,
        html.Div([
            html.Span("Ask management", style={"fontSize": 10.5, "fontWeight": 700, "letterSpacing": "0.5px",
                                                "color": "#6b7283", "textTransform": "uppercase",
                                                "display": "block", "marginBottom": 3}),
            q1,
        ], style={"fontSize": 13, "color": "#2c3040", "background": "#f5f6f8", "borderRadius": 7,
                  "padding": "9px 11px", "marginBottom": 12}) if q1 else None,
        html.Div([
            html.Button("View evidence", id={"type": "tne-view-evidence", "index": idx},
                        n_clicks=0, style={"fontSize": 13, "marginRight": 8}),
            html.Button("View exceptions", id={"type": "tne-view-exceptions", "index": idx},
                        n_clicks=0, className="ghost", style={"fontSize": 13}),
        ], style={"display": "flex", "gap": 4, "flexWrap": "wrap"}),
    ])

    return html.Article([
        html.Div(summary_items, style={"display": "flex", "gap": 8, "flexWrap": "wrap", "marginBottom": 8}),
        html.H3(finding.get("title", ""), style={"margin": "0 0 8px", "fontSize": 15.5, "fontWeight": 700, "lineHeight": 1.3}),
        detail,
    ], className="panel finding-card")


def _render_filtered_findings(findings: list[dict], severity_filter, sort_by) -> list:
    filtered = [
        (idx, f) for idx, f in enumerate(findings)
        if not severity_filter or f.get("severity") in severity_filter
    ]
    if sort_by == "exposure":
        filtered.sort(key=lambda item: item[1].get("exposure_amount") or 0, reverse=True)
    elif sort_by == "severity":
        order = {"High": 0, "Medium": 1, "Low": 2}
        filtered.sort(key=lambda item: order.get(item[1].get("severity"), 3))

    if not filtered:
        return [html.Div("No findings match the selected filters.", className="panel",
                          style={"color": "#6b7283", "fontSize": 13})]

    priority = [_finding_card(idx, f) for idx, f in filtered[:3]]
    remaining = [_finding_card(idx, f) for idx, f in filtered[3:]]
    children = [
        html.Div(f"Showing the top {min(3, len(filtered))} priority findings",
                  style={"fontSize": 12, "fontWeight": 600, "color": "#6b7283"}),
        *priority,
    ]
    if remaining:
        children.append(html.Details([
            html.Summary(f"Show {len(remaining)} additional findings", className="ghost"),
            html.Div(remaining, className="stack", style={"marginTop": 12}),
        ]))
    return children


def _findings_tab(findings: list[dict]) -> html.Div:
    severities = sorted({f.get("severity") for f in findings if f.get("severity")})
    filters_row = html.Div([
        dcc.Dropdown(id="tne-severity-filter",
                     options=[{"label": s, "value": s} for s in severities],
                     multi=True, placeholder="All severities", style={"minWidth": 160}),
        dcc.Dropdown(id="tne-sort",
                     options=[{"label": "As authored", "value": "authored"},
                              {"label": "Severity", "value": "severity"},
                              {"label": "Financial exposure", "value": "exposure"}],
                     value="severity", clearable=False, style={"minWidth": 180}),
    ], className="filter-row", style={"marginBottom": 12})

    export_row = html.Div([
        html.Button("Export PPTX", id="tne-export-pptx", className="ghost", style={"fontSize": 12.5}),
        html.Button("Export Excel", id="tne-export-excel", className="ghost", style={"fontSize": 12.5}),
        dcc.Download(id="tne-download-pptx"),
        dcc.Download(id="tne-download-excel"),
        html.Div(id="tne-export-error", style={"fontSize": 11.5, "color": "#b85042"}),
    ], style={"display": "flex", "gap": 8, "alignItems": "center", "marginBottom": 14})

    return html.Div([
        filters_row,
        export_row,
        html.Div(_render_filtered_findings(findings, [], "severity"),
                  id="tne-filtered-findings", className="stack"),
    ])


# ── Test catalogue tab ───────────────────────────────────────────────────────

_STATUS_LABEL = {"exception": "Exception", "pass": "Pass", "not_testable": "Not testable"}


def _results_for(catalogue_test_id: str, test_results: list[dict]) -> list[dict]:
    """orchestrator.service's catalogue-grain test_id (e.g. "T3.2a") can
    cover several plan-grain test_results (e.g. "T3.2a_air_dom",
    "T3.2a_car_int" — one per primitive instance, CLAUDE.md build brief P2
    §4.3): a test_result matches when its own id equals the catalogue id, or
    starts with it plus "_"."""
    return [
        r for r in test_results
        if r.get("test_id") == catalogue_test_id or str(r.get("test_id", "")).startswith(catalogue_test_id + "_")
    ]


_STATUS_PRIORITY = {"exception": 0, "pass": 1, "not_testable": 2}


def _combined_status(results: list[dict]) -> dict:
    if not results:
        return {}
    best = min(results, key=lambda r: _STATUS_PRIORITY.get(r.get("status"), 3))
    total_exceptions = sum(r.get("exception_units") or 0 for r in results if r.get("status") == "exception")
    return {**best, "exception_units": total_exceptions if best.get("status") == "exception" else best.get("exception_units")}


def _catalogue_tab(tests: list[dict], test_results: list[dict]) -> html.Div:
    n_exception = sum(1 for r in test_results if r.get("status") == "exception")
    n_pass = sum(1 for r in test_results if r.get("status") == "pass")
    n_na = sum(1 for r in test_results if r.get("status") == "not_testable")

    status_kpis = html.Div([
        html.Div([html.Div("Tests executed", className="kpi-title"), html.Div(str(len(test_results)), className="kpi-value")], className="kpi-tile"),
        html.Div([html.Div("Exceptions", className="kpi-title"), html.Div(str(n_exception), className="kpi-value")], className="kpi-tile"),
        html.Div([html.Div("Pass", className="kpi-title"), html.Div(str(n_pass), className="kpi-value")], className="kpi-tile"),
        html.Div([html.Div("Not testable", className="kpi-title"), html.Div(str(n_na), className="kpi-value")], className="kpi-tile"),
    ], className="grid-4 mb-3")

    rows = []
    for test in tests:
        test_id = test.get("test_id", "")
        result = _combined_status(_results_for(test_id, test_results))
        status = result.get("status")
        status_label = _STATUS_LABEL.get(status, "Not run")
        reason = result.get("reason")
        rows.append({
            "Test ID": test_id,
            "Category": test.get("category", ""),
            "Test Name": test.get("test_name", test.get("title", "")),
            "Threshold": test.get("threshold", ""),
            "Exceptions": result.get("exception_units", "—"),
            "Status": status_label + (f" — {reason}" if status == "not_testable" and reason else ""),
        })

    return html.Div([
        status_kpis,
        html.Div([
            html.Div("Test Catalogue", className="table-title"),
            dash_table.DataTable(
                id="tne-catalogue-table",
                data=rows,
                columns=[{"name": c, "id": c} for c in ["Test ID", "Category", "Test Name", "Threshold", "Exceptions", "Status"]],
                page_size=20, sort_action="native", filter_action="native",
                style_table={"overflowX": "auto"},
                style_cell={"fontSize": 12, "padding": "6px 10px", "textAlign": "left"},
                style_data_conditional=[
                    {"if": {"filter_query": "{Status} contains 'Exception'"}, "color": "#b85042", "fontWeight": 600},
                    {"if": {"filter_query": "{Status} = 'Pass'"}, "color": "#2c7a4b", "fontWeight": 600},
                    {"if": {"filter_query": "{Status} contains 'Not testable'"}, "color": "#6b7283"},
                ],
            ),
        ], className="panel"),
    ])


# ── Audit Detail tab — population charts from get_run_frames ────────────────

def _detail_tab(frames: dict) -> html.Div:
    df = _pick_frame(frames, _EXPENSE_FRAME_CANDIDATES)
    if df is None or df.empty:
        return html.Div(
            "No row-level population frame was returned for this run "
            "(get_run_frames) — the analytics charts below need it.",
            className="panel", style={"color": "#6b7283", "fontSize": 13},
        )

    figs = []

    if "Transaction Date" in df.columns and "Employee" in df.columns:
        monthly = df.dropna(subset=["Transaction Date"]).copy()
        monthly["Transaction Date"] = pd.to_datetime(monthly["Transaction Date"], errors="coerce")
        monthly = monthly.dropna(subset=["Transaction Date"])
        monthly["Month"] = monthly["Transaction Date"].dt.to_period("M").dt.to_timestamp()
        amount_col = "Expense Amount (reimbursement currency)"
        agg = {"Claims": ("Employee", "count")}
        if amount_col in monthly.columns:
            agg["Amount"] = (amount_col, "sum")
        m = monthly.groupby("Month", as_index=False).agg(**agg)
        f1 = go.Figure()
        f1.add_bar(x=m["Month"], y=m["Claims"], name="Claims", marker_color="#1e2761")
        if "Amount" in m.columns:
            f1.add_trace(go.Scatter(x=m["Month"], y=m["Amount"], name="Amount", mode="lines+markers",
                                     yaxis="y2", line=dict(color="#1c7293", width=2)))
        f1.update_layout(title="Monthly T&E volume", yaxis2=dict(overlaying="y", side="right"),
                          legend=dict(orientation="h"), margin=dict(l=16, r=16, t=40, b=16), height=260)
        figs.append(("p1-monthly", f1))

    flag_cols = [c for c in df.columns if c.startswith("RF_")]
    if flag_cols and "Employee" in df.columns:
        breach_mask = df[flag_cols].fillna(0).astype(int).sum(axis=1) > 0
        exploded = []
        for col in flag_cols:
            hit = df[df[col].fillna(0).astype(int) == 1]
            if len(hit):
                exploded.append(pd.DataFrame({"Employee": hit["Employee"], "Flag": col}))
        if exploded:
            ex = pd.concat(exploded, ignore_index=True)
            bmem = ex.groupby(["Employee", "Flag"], as_index=False).size().rename(columns={"size": "Count"})
            totals = bmem.groupby("Employee")["Count"].sum().sort_values(ascending=True).index.tolist()
            f2 = px.bar(bmem, x="Count", y="Employee", color="Flag", orientation="h",
                        category_orders={"Employee": totals}, title="Exceptions by employee")
            f2.update_layout(margin=dict(l=16, r=16, t=40, b=16), height=260, legend=dict(orientation="h"))
            figs.append(("p1-by-employee", f2))

    amount_col = "Expense Amount (reimbursement currency)"
    if "Expense Type" in df.columns and amount_col in df.columns:
        by_type = df.groupby("Expense Type", as_index=False)[amount_col].sum().nlargest(10, amount_col)
        f3 = px.bar(by_type.sort_values(amount_col), x=amount_col, y="Expense Type", orientation="h",
                    title="Top 10 expense types by spend")
        f3.update_layout(margin=dict(l=16, r=16, t=40, b=16), height=260, xaxis_tickprefix="$")
        figs.append(("p1-by-type", f3))

    if "Employee" in df.columns and amount_col in df.columns:
        by_emp = df.groupby("Employee", as_index=False)[amount_col].sum().nlargest(10, amount_col)
        f4 = go.Figure()
        f4.add_bar(x=by_emp[amount_col], y=by_emp["Employee"], orientation="h", marker_color="#1e2761")
        f4.update_layout(title="Top 10 spenders", margin=dict(l=16, r=16, t=40, b=16), height=260, xaxis_tickprefix="$")
        figs.append(("p1-top-spenders", f4))

    if not figs:
        return html.Div(
            "The returned population frame does not have the expected columns "
            "(Employee, Transaction Date, Expense Type, Expense Amount) to build these charts.",
            className="panel", style={"color": "#6b7283", "fontSize": 13},
        )

    chart_panels = [html.Div([dcc.Graph(id=fig_id, figure=fig, config={"displayModeBar": False})], className="panel")
                     for fig_id, fig in figs]

    detail_table = html.Div([
        html.Div("Row-level population", className="table-title"),
        dash_table.DataTable(
            data=df.head(500).to_dict("records"),
            columns=[{"name": c, "id": c} for c in df.columns],
            page_size=25, sort_action="native", filter_action="native", export_format="csv",
            style_table={"overflowX": "auto"}, style_cell={"fontSize": 11.5, "padding": "5px 8px"},
        ),
        html.P(f"Showing first 500 of {len(df):,} rows." if len(df) > 500 else "",
               style={"fontSize": 11, "color": "#6b7283", "marginTop": 8}),
    ], className="panel", style={"marginTop": 16})

    return html.Div([html.Div(chart_panels, className="grid-2"), detail_table])


# ── Management actions tab ───────────────────────────────────────────────────

def _actions_tab(run_id: str) -> html.Div:
    actions = [a for a in adapters.list_management_actions() if a.get("run_id") == run_id]
    if not actions:
        return html.Div("No management actions drafted for this run yet.", className="panel",
                         style={"color": "#6b7283", "fontSize": 13})
    rows = []
    for a in actions:
        rows.append(html.Tr([
            html.Td(a.get("finding_title", "")),
            html.Td(a.get("risk", "")),
            html.Td(a.get("owner", "")),
            html.Td(a.get("status", "")),
            html.Td(a.get("target_date", "")),
            html.Td(f"${a.get('potential_exposure', 0):,.0f}" if a.get("potential_exposure") is not None else "—"),
        ]))
    return html.Div([
        html.Table([
            html.Thead(html.Tr([html.Th("Finding"), html.Th("Risk"), html.Th("Owner"),
                                 html.Th("Status"), html.Th("Target date"), html.Th("Exposure")])),
            html.Tbody(rows),
        ], className="plat-table"),
    ], className="panel", style={"overflowX": "auto"})


# ── Offcanvases (evidence / exception drill-down) ────────────────────────────

def _offcanvases() -> list:
    return [
        dbc.Offcanvas(id="tne-evidence-offcanvas", title="Evidence", placement="end", is_open=False,
                      style={"width": "min(620px, 94vw)"}, children=html.Div(id="tne-evidence-body")),
        dbc.Offcanvas(id="tne-exception-offcanvas", title="Exception Records", placement="end", is_open=False,
                      style={"width": "min(800px, 96vw)"}, children=html.Div(id="tne-exception-body")),
    ]


# ── Layout entry point ───────────────────────────────────────────────────────

def tne_workspace_layout(run_id: str | None) -> html.Div:
    if not run_id:
        run_id = latest_completed_run_id()
        if not run_id:
            return _empty_state()

    bundle = _load_bundle(run_id)
    if bundle is None:
        return _empty_state()

    run = bundle["run"]
    findings = run.get("findings", [])
    tests = bundle["skill"].get("tests", [])
    test_results = run.get("test_results", [])
    frames = bundle["frames"]
    payload = bundle["payload"]

    return html.Div([
        _build_header(run),
        dcc.Store(id="tne-run-id", data=run_id),
        dcc.Store(id="tne-findings-store", data=findings),
        html.Div([
            dbc.Tabs([
                dbc.Tab(_executive_tab(run, findings, payload), label="Executive Brief", tab_id="tab-executive"),
                dbc.Tab(_findings_tab(findings), label="Findings & Evidence", tab_id="tab-findings"),
                dbc.Tab(_catalogue_tab(tests, test_results), label="Test catalogue", tab_id="tab-catalogue"),
                dbc.Tab(_detail_tab(frames), label="Audit Detail", tab_id="tab-detail"),
                dbc.Tab(_actions_tab(run_id), label="Management Actions", tab_id="tab-actions"),
            ], id="tne-main-tabs", active_tab="tab-executive"),
            *_offcanvases(),
        ], className="shell dashboard-shell"),
    ])


# ── Callbacks ─────────────────────────────────────────────────────────────

def register_callbacks(app) -> None:

    @app.callback(
        Output("tne-filtered-findings", "children"),
        Input("tne-severity-filter", "value"),
        Input("tne-sort", "value"),
        State("tne-findings-store", "data"),
        prevent_initial_call=True,
    )
    def _filter_findings(severity_filter, sort_by, findings):
        return _render_filtered_findings(findings or [], severity_filter or [], sort_by)

    @app.callback(
        Output("tne-evidence-offcanvas", "is_open"),
        Output("tne-evidence-body", "children"),
        Output("tne-evidence-offcanvas", "title"),
        Input({"type": "tne-view-evidence", "index": ALL}, "n_clicks"),
        State("tne-findings-store", "data"),
        State("tne-run-id", "data"),
        prevent_initial_call=True,
    )
    def _open_evidence(n_clicks_list, findings, run_id):
        if not any(n for n in n_clicks_list if n) or not ctx.triggered_id:
            raise PreventUpdate
        idx = ctx.triggered_id["index"]
        finding = (findings or [])[idx]
        metrics_cited = finding.get("metrics_cited", {})

        rows = []
        for name, m in metrics_cited.items():
            rows.append(html.Tr([
                html.Td(name, style={"fontFamily": "monospace", "fontSize": 11, "color": "#6b7283"}),
                html.Td(_fmt_metric(m.get("value"), m.get("unit")),
                        style={"fontSize": 12.5, "fontWeight": 600, "fontFamily": "monospace", "textAlign": "right"}),
                html.Td(_fmt_source_ref(m.get("source_ref")), style={"fontSize": 11, "color": "#6b7283"}),
            ]))

        content = html.Div([
            html.P(finding.get("observation", ""), style={"fontSize": 13.5, "lineHeight": 1.55}),
            html.Div("Metrics cited — source provenance",
                     style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginTop": 14, "marginBottom": 6}),
            html.Table([
                html.Thead(html.Tr([html.Th("metric"), html.Th("value", style={"textAlign": "right"}), html.Th("source_ref")])),
                html.Tbody(rows),
            ], style={"width": "100%", "fontSize": 12.5, "borderCollapse": "collapse"}) if rows else
            html.P("No cited metrics recorded for this finding.", style={"color": "#6b7283", "fontSize": 12}),
            html.Div("Management questions", style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginTop": 14, "marginBottom": 6}),
            html.Ul([html.Li(q) for q in finding.get("management_questions", [])],
                    style={"fontSize": 13, "paddingLeft": 18}),
        ])
        return True, content, finding.get("title", "Evidence")

    @app.callback(
        Output("tne-exception-offcanvas", "is_open"),
        Output("tne-exception-body", "children"),
        Output("tne-exception-offcanvas", "title"),
        Input({"type": "tne-view-exceptions", "index": ALL}, "n_clicks"),
        State("tne-findings-store", "data"),
        State("tne-run-id", "data"),
        prevent_initial_call=True,
    )
    def _open_exceptions(n_clicks_list, findings, run_id):
        if not any(n for n in n_clicks_list if n) or not ctx.triggered_id:
            raise PreventUpdate
        idx = ctx.triggered_id["index"]
        finding = (findings or [])[idx]
        test_id = finding.get("test_id", "")

        bundle = _load_bundle(run_id)
        frames = (bundle or {}).get("frames", {})
        df = _pick_frame(frames, _EXPENSE_FRAME_CANDIDATES)

        tests = (bundle or {}).get("skill", {}).get("tests", [])
        flag_col = next((t.get("flag") for t in tests if t.get("test_id") == test_id and t.get("flag")), None)

        if df is None or not flag_col or flag_col not in df.columns:
            content = html.Div([
                html.P("No transaction-level drill-down available for this test.", style={"color": "#6b7283", "fontSize": 13}),
                html.P(f"Test {test_id} — no matching flag column in the returned population frame.",
                       style={"color": "#6b7283", "fontSize": 12}),
            ])
            return True, content, f"{test_id}: Exception Records"

        df_exc = df[df[flag_col].fillna(0).astype(int) == 1]
        content = html.Div([
            html.Div([
                html.Span(f"{len(df_exc):,} exception records", className="chip"),
                html.Span(test_id, className="chip mono"),
            ], className="chip-row", style={"marginBottom": 12}),
            dash_table.DataTable(
                data=df_exc.head(200).to_dict("records"),
                columns=[{"name": c, "id": c} for c in df_exc.columns],
                page_size=25, sort_action="native", filter_action="native", export_format="csv",
                style_table={"overflowX": "auto"}, style_cell={"fontSize": 11.5, "padding": "5px 8px"},
            ),
            html.P(f"Showing first 200 of {len(df_exc):,} records." if len(df_exc) > 200 else "",
                   style={"fontSize": 11, "color": "#6b7283", "marginTop": 8}),
        ])
        return True, content, f"{test_id}: Exception Records"

    @app.callback(
        Output("tne-download-pptx", "data"),
        Output("tne-export-error", "children", allow_duplicate=True),
        Input("tne-export-pptx", "n_clicks"),
        State("tne-run-id", "data"),
        prevent_initial_call=True,
    )
    def _export_pptx(n_clicks, run_id):
        if not n_clicks:
            raise PreventUpdate
        try:
            filename, blob = adapters.get_export(run_id, "pptx")
        except Exception as exc:  # NN13 — never a broken button, say why
            return None, f"PPTX export arrives in P6 ({exc})"
        return dcc.send_bytes(blob, filename), ""

    @app.callback(
        Output("tne-download-excel", "data"),
        Output("tne-export-error", "children", allow_duplicate=True),
        Input("tne-export-excel", "n_clicks"),
        State("tne-run-id", "data"),
        prevent_initial_call=True,
    )
    def _export_excel(n_clicks, run_id):
        if not n_clicks:
            raise PreventUpdate
        try:
            filename, blob = adapters.get_export(run_id, "xlsx")
        except Exception as exc:
            return None, f"Excel export is not available for this run ({exc})"
        return dcc.send_bytes(blob, filename), ""
