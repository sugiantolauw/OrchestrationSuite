"""Reusable UI components for the AI Audit Analyst platform.

Every component is domain-agnostic: it accepts generic metadata dicts
rather than T&E-specific or GST-specific fields.
"""

from __future__ import annotations

from dash import dcc, html
import dash_bootstrap_components as dbc

from orchestrator.signoff_policy import SELF_APPROVED_LABEL


# ── KPI card ─────────────────────────────────────────────────────────────────

def kpi_card(label: str, value: str, unit: str = "", trend: str = "", status: str = "") -> html.Div:
    status_color = {"up": "#2c7a4b", "down": "#b85042", "neutral": "#6b7283"}.get(status, "#6b7283")
    children = [
        html.Div(label, className="kpi-title"),
        html.Div(value, className="kpi-value"),
    ]
    if unit:
        children.append(html.Span(unit, style={"fontSize": 11, "color": "#6b7283", "marginLeft": 4}))
    if trend:
        children.append(html.Span(trend, style={"fontSize": 11, "color": status_color, "marginLeft": 6}))
    return html.Div(children, className="kpi-tile")


# ── Skill card ───────────────────────────────────────────────────────────────

_SKILL_STATUS_STYLE = {
    "Published": {"background": "#eaf5ee", "color": "#2c7a4b"},
    "Draft": {"background": "#f0f1f4", "color": "#6b7283"},
    "Needs review": {"background": "#fffbf0", "color": "#6b4a00"},
    "Archived": {"background": "#f0f1f4", "color": "#6b7283"},
    "Unavailable": {"background": "#fef2f0", "color": "#7c2d26"},
}


def skill_card(skill: dict, selected: bool = False) -> html.Div:
    st = skill.get("status", "Draft")
    st_style = _SKILL_STATUS_STYLE.get(st, _SKILL_STATUS_STYLE["Draft"])
    border_color = "#1e2761" if selected else "#e4e7ee"
    box_shadow = "0 0 0 2px #1e2761" if selected else "none"

    return html.Div([
        html.Div([
            html.Span(skill["skill_id"], className="chip mono",
                      style={"fontSize": 10, "color": "#6b7283"}),
            html.Span(st, className="chip",
                      style={**st_style, "fontSize": 10, "border": "none", "fontWeight": 700}),
        ], style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": 6}),
        html.H4(skill["name"], style={"margin": "0 0 4px", "fontSize": 14, "fontWeight": 700, "color": "#1a1d26"}),
        html.Div([
            html.Span(skill.get("domain", ""), style={"fontSize": 11, "color": "#6b7283"}),
            html.Span(" · ", style={"color": "#cfd5e2"}),
            html.Span(f"v{skill.get('version', '?')}", style={"fontSize": 11, "color": "#6b7283"}),
            html.Span(" · ", style={"color": "#cfd5e2"}),
            html.Span(f"{skill.get('tests', 0)} tests", style={"fontSize": 11, "color": "#6b7283"}),
        ], style={"marginBottom": 6}),
        html.P(skill.get("description", ""), style={"margin": 0, "fontSize": 12.5, "color": "#3b4150", "lineHeight": 1.45}),
        html.Div([
            html.Span(f"Last run: {skill.get('last_run') or 'Never'}", style={"fontSize": 10.5, "color": "#6b7283"}),
            html.Span(f"{skill.get('previous_runs', 0)} runs", style={"fontSize": 10.5, "color": "#6b7283"}),
        ], style={"display": "flex", "justifyContent": "space-between", "marginTop": 8}),
        html.Div([
            dcc.Link("View methodology", href=f"/skills/{skill['skill_id']}",
                     className="skill-card-link"),
        ], style={"marginTop": 10, "paddingTop": 8, "borderTop": "1px solid #f0f1f4"}),
    ], className="panel skill-card", id={"type": "skill-select-card", "index": skill["skill_id"]},
       style={"cursor": "pointer", "borderColor": border_color, "boxShadow": box_shadow,
              "transition": "border-color 0.15s, box-shadow 0.15s"})



# ── Data asset card ──────────────────────────────────────────────────────────

_ACCESS_STYLE = {
    "Available": {"background": "#eaf5ee", "color": "#2c7a4b"},
    "Request access": {"background": "#fffbf0", "color": "#6b4a00"},
    "Restricted": {"background": "#fef2f0", "color": "#7c2d26"},
}


def data_asset_card(asset: dict) -> html.Div:
    access = asset.get("access", "Available")
    access_st = _ACCESS_STYLE.get(access, _ACCESS_STYLE["Available"])
    rows_text = f"{asset['rows']:,} rows" if asset.get("rows") else "File"
    return html.Div([
        html.Div([
            html.Span(asset.get("type", "Table"), className="chip mono",
                      style={"fontSize": 10, "color": "#6b7283"}),
            html.Span(access, className="chip",
                      style={**access_st, "fontSize": 10, "border": "none", "fontWeight": 700}),
        ], style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": 4}),
        html.Div(asset["name"], style={"fontSize": 13, "fontWeight": 600, "color": "#1a1d26",
                                        "fontFamily": "ui-monospace, SFMono-Regular, monospace",
                                        "wordBreak": "break-all", "marginBottom": 4}),
        html.P(asset.get("description", ""), style={"margin": "0 0 6px", "fontSize": 12, "color": "#3b4150", "lineHeight": 1.4}),
        html.Div([
            html.Span(f"Owner: {asset.get('owner', '—')}", style={"fontSize": 10.5, "color": "#6b7283"}),
            html.Span(rows_text, style={"fontSize": 10.5, "color": "#6b7283"}),
        ], style={"display": "flex", "justifyContent": "space-between"}),
        html.Div([
            html.Span(f"Refreshed: {asset.get('last_refreshed', '—')}", style={"fontSize": 10.5, "color": "#6b7283"}),
            html.Span(asset.get("classification", ""), style={"fontSize": 10.5, "color": "#6b7283"}),
        ], style={"display": "flex", "justifyContent": "space-between", "marginTop": 2}),
    ], className="panel data-asset-card")


# ── Run card ─────────────────────────────────────────────────────────────────

_RUN_STATUS_STYLE = {
    "Running": {"background": "#e8f4fd", "color": "#0a5e8a"},
    "Completed": {"background": "#eaf5ee", "color": "#2c7a4b"},
    "Failed": {"background": "#fef2f0", "color": "#7c2d26"},
    "Needs review": {"background": "#fffbf0", "color": "#6b4a00"},
}


def run_card(run: dict) -> html.Div:
    st = run.get("status", "Completed")
    st_style = _RUN_STATUS_STYLE.get(st, _RUN_STATUS_STYLE["Completed"])

    return html.Div([
        html.Div([
            html.Span(run["run_id"], className="chip mono",
                      style={"fontSize": 10, "color": "#6b7283"}),
            html.Span(st, className="chip",
                      style={**st_style, "fontSize": 10, "border": "none", "fontWeight": 700}),
            html.Span(run.get("data_mode", "Demo"), className="chip",
                      style={"fontSize": 10, "color": "#6b7283"}),
        ], style={"display": "flex", "gap": 6, "alignItems": "center", "marginBottom": 6}),
        html.H4(run.get("skill_name", "Unknown Skill"),
                style={"margin": "0 0 4px", "fontSize": 14, "fontWeight": 700, "color": "#1a1d26"}),
        html.Div([
            html.Span(f"Period: {run.get('audit_period', '—')}", style={"fontSize": 11.5, "color": "#3b4150"}),
        ], style={"marginBottom": 4}),
        html.Div([
            kpi_card("Findings", str(run.get("findings_count", 0))),
            kpi_card("High risk", str(run.get("high_risk_count", 0))),
            kpi_card("Exposure", f"${run.get('potential_exposure', 0):,.0f}"),
            kpi_card("Open actions", str(run.get("open_actions", 0))),
        ], className="plat-kpi-row"),
        html.Div([
            html.Span(f"Owner: {run.get('run_owner', '—')}", style={"fontSize": 10.5, "color": "#6b7283"}),
            html.Span(run.get("run_timestamp", "")[:16].replace("T", " "),
                      style={"fontSize": 10.5, "color": "#6b7283"}),
        ], style={"display": "flex", "justifyContent": "space-between", "marginTop": 8}),
        demo_indicator(SELF_APPROVED_LABEL) if run.get("self_approved") else None,
        html.Div([
            html.Button("View", id={"type": "run-view-btn", "index": run["run_id"]},
                        style={"fontSize": 12, "padding": "4px 12px"}),
            html.Button("Export", className="ghost",
                        style={"fontSize": 12, "padding": "4px 12px", "width": "auto"}),
            html.Button("Trace", className="ghost",
                        style={"fontSize": 12, "padding": "4px 12px", "width": "auto"}),
        ], style={"display": "flex", "gap": 8, "marginTop": 10}),
    ], className="panel run-card")


# ── Management action row ────────────────────────────────────────────────────

_ACTION_STATUS_STYLE = {
    "Open": {"background": "#fef2f0", "color": "#7c2d26"},
    "Under review": {"background": "#fffbf0", "color": "#6b4a00"},
    "Agreed": {"background": "#e8f4fd", "color": "#0a5e8a"},
    "Remediated": {"background": "#eaf5ee", "color": "#2c7a4b"},
    "Closed": {"background": "#f0f1f4", "color": "#6b7283"},
}

_RISK_COLOR = {"High": "#b85042", "Medium": "#e0952a", "Low": "#2c7a4b"}


def action_row(action: dict) -> html.Tr:
    risk = action.get("risk", "Medium")
    risk_c = _RISK_COLOR.get(risk, "#6b7283")
    st = action.get("status", "Open")
    st_style = _ACTION_STATUS_STYLE.get(st, _ACTION_STATUS_STYLE["Open"])

    return html.Tr([
        html.Td(action.get("finding_title", ""), style={"fontWeight": 600, "fontSize": 13}),
        html.Td(action.get("skill_name", ""), style={"fontSize": 12}),
        html.Td(html.Span(risk, style={"color": risk_c, "fontWeight": 700, "fontSize": 12})),
        html.Td(action.get("owner", ""), style={"fontSize": 12}),
        html.Td(html.Span(st, className="chip", style={**st_style, "fontSize": 10, "border": "none"})),
        html.Td(action.get("target_date", ""), style={"fontSize": 12}),
        html.Td(f"${action.get('potential_exposure', 0):,.0f}", style={"fontSize": 12, "fontFamily": "monospace"}),
        html.Td(action.get("evidence_link", ""), style={"fontSize": 11, "fontFamily": "monospace", "color": "#6b7283"}),
    ])


# ── Trace event row ─────────────────────────────────────────────────────────

_TRACE_STATUS_STYLE = {
    "complete": {"background": "#eaf5ee", "color": "#2c7a4b"},
    "running": {"background": "#e8f4fd", "color": "#0a5e8a"},
    "pending": {"background": "#f0f1f4", "color": "#6b7283"},
    "failed": {"background": "#fef2f0", "color": "#7c2d26"},
    "awaiting_confirmation": {"background": "#fffbf0", "color": "#6b4a00"},
}


def trace_event_row(event: dict) -> html.Tr:
    st = event.get("status", "pending")
    st_style = _TRACE_STATUS_STYLE.get(st, _TRACE_STATUS_STYLE["pending"])
    dur = event.get("duration_s")
    dur_text = f"{dur:.1f}s" if dur is not None else "—"

    return html.Tr([
        html.Td(event.get("timestamp", "")[:19].replace("T", " "), style={"fontSize": 12, "fontFamily": "monospace", "whiteSpace": "nowrap"}),
        html.Td(event.get("stage", ""), style={"fontSize": 12, "fontWeight": 600}),
        html.Td(html.Span(st.replace("_", " ").title(), className="chip",
                           style={**st_style, "fontSize": 10, "border": "none"})),
        html.Td(event.get("message", ""), style={"fontSize": 12, "color": "#3b4150"}),
        html.Td(dur_text, style={"fontSize": 12, "fontFamily": "monospace", "color": "#6b7283", "textAlign": "right"}),
    ])


# ── Workflow stage preview ───────────────────────────────────────────────────

_STAGE_ICONS = {
    "ready": "●",
    "pending": "○",
    "complete": "●",
    "running": "◉",
    "needs_confirmation": "◎",
}

_STAGE_COLORS = {
    "ready": "#2c7a4b",
    "pending": "#cfd5e2",
    "complete": "#2c7a4b",
    "running": "#0a5e8a",
    "needs_confirmation": "#e0952a",
}


def workflow_stage(stage: dict, idx: int, total: int) -> html.Div:
    st = stage.get("status", "pending")
    color = _STAGE_COLORS.get(st, "#cfd5e2")
    icon = _STAGE_ICONS.get(st, "○")
    connector = html.Div(style={
        "width": 2, "height": 24, "background": color if st != "pending" else "#e4e7ee",
        "marginLeft": 7, "marginTop": 2, "marginBottom": 2,
    }) if idx < total - 1 else None

    return html.Div([
        html.Div([
            html.Span(icon, style={"color": color, "fontSize": 14, "width": 16, "textAlign": "center",
                                    "flexShrink": 0, "lineHeight": 1}),
            html.Div([
                html.Span(stage["stage"], style={"fontSize": 13, "fontWeight": 600, "color": "#1a1d26"}),
                html.Span(stage.get("detail", ""), style={"fontSize": 11.5, "color": "#6b7283", "marginLeft": 8})
                if stage.get("detail") else None,
            ], style={"display": "flex", "alignItems": "baseline", "gap": 4, "flexWrap": "wrap"}),
        ], style={"display": "flex", "gap": 8, "alignItems": "flex-start"}),
        connector,
    ])


# ── Upload file row ─────────────────────────────────────────────────────────

_UPLOAD_STATUS_STYLE = {
    "Uploaded": {"background": "#e8f4fd", "color": "#0a5e8a"},
    "Profiling": {"background": "#fffbf0", "color": "#6b4a00"},
    "Ready": {"background": "#eaf5ee", "color": "#2c7a4b"},
    "Warning": {"background": "#fffbf0", "color": "#6b4a00"},
    "Failed": {"background": "#fef2f0", "color": "#7c2d26"},
}


def upload_file_row(file_info: dict) -> html.Div:
    val_status = file_info.get("validation", "Uploaded")
    val_style = _UPLOAD_STATUS_STYLE.get(val_status, _UPLOAD_STATUS_STYLE["Uploaded"])
    size = file_info.get("size_bytes", 0)
    size_text = f"{size / 1024:.0f} KB" if size < 1_048_576 else f"{size / 1_048_576:.1f} MB"

    return html.Div([
        html.Div([
            html.Span(file_info.get("filename", ""), style={"fontSize": 13, "fontWeight": 600, "color": "#1a1d26"}),
            html.Span(size_text, style={"fontSize": 11, "color": "#6b7283", "marginLeft": 8}),
            html.Span(val_status, className="chip",
                      style={**val_style, "fontSize": 10, "border": "none", "fontWeight": 700}),
        ], style={"display": "flex", "alignItems": "center", "gap": 6}),
        html.Div(file_info.get("destination", ""), style={"fontSize": 11, "color": "#6b7283",
                                                           "fontFamily": "monospace", "marginTop": 2}),
    ], className="upload-file-row", style={"padding": "8px 0", "borderBottom": "1px solid #e4e7ee"})


# ── Mode selector cards ─────────────────────────────────────────────────────

def mode_card(mode_id: str, title: str, description: str, selected: bool = False) -> html.Div:
    border_color = "#1e2761" if selected else "#e4e7ee"
    box_shadow = "0 0 0 2px #1e2761" if selected else "none"
    bg = "rgba(30, 39, 97, 0.02)" if selected else "#ffffff"

    return html.Div([
        html.H4(title, style={"margin": "0 0 6px", "fontSize": 15, "fontWeight": 700, "color": "#1e2761"}),
        html.P(description, style={"margin": 0, "fontSize": 12.5, "color": "#3b4150", "lineHeight": 1.45}),
    ], className="panel mode-card", id={"type": "mode-select-card", "index": mode_id},
       style={"cursor": "pointer", "borderColor": border_color, "boxShadow": box_shadow,
              "background": bg, "transition": "all 0.15s"})


# ── Environment badge ────────────────────────────────────────────────────────

def env_badge(label: str) -> html.Span:
    colors = {
        "Demo": {"background": "#fffbf0", "color": "#6b4a00"},
        "Non-production": {"background": "#e8f4fd", "color": "#0a5e8a"},
        "Live": {"background": "#eaf5ee", "color": "#2c7a4b"},
        "Local test data": {"background": "#fffbf0", "color": "#6b4a00"},
        "Unity Catalog": {"background": "#eaf5ee", "color": "#2c7a4b"},
    }
    st = colors.get(label, {"background": "#f0f1f4", "color": "#6b7283"})
    return html.Span(label, className="chip", style={
        **st, "fontSize": 10.5, "fontWeight": 700, "border": "none",
        "padding": "3px 10px", "letterSpacing": "0.03em",
    })


# ── Demo indicator ───────────────────────────────────────────────────────────

def demo_indicator(message: str = "Demo data — not connected to a live backend") -> html.Div:
    return html.Div([
        html.Span("◆", style={"color": "#e0952a", "marginRight": 6}),
        html.Span(message, style={"fontSize": 12, "color": "#6b4a00"}),
    ], className="demo-indicator")
