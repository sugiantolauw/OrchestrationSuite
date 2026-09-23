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
  * `_standardise_combined` / `_standardise_pre` / `_standardise_approval`
    (reference_app/app.py:107-226) fuzzy-matched columns and defaulted
    missing ones to "Unknown" / 0 / 2025-01-01 so the demo always rendered.
    This module never does that: every chart below reads the CONTRACT's own
    column names (skills/tne_exco/contract.yaml) directly, and degrades to a
    labelled "no data" panel (app/src/charts.py's `empty_figure`) when a
    frame is missing an expected column, rather than guessing a value
    (CLAUDE.md NN14).

Flag → catalogue-test-id mapping (needed for the exception drill-down and
for labelling breach categories by test) is not yet on get_skill()'s own
payload (the orchestrator/service.py owner is adding
`tests[].plan_tests`/top-level `flag_to_test` — see the phase brief). Until
it lands, `_skill_flag_meta` derives the same mapping by reading
skills/<skill_id>/plan.yaml directly via `orchestrator.skills.load_skill`
(read-only; orchestrator/ is not this module's to edit) behind one helper,
so switching to the service's own field later is a one-line change.
"""

from __future__ import annotations

from pathlib import Path

import dash
import pandas as pd
import dash_bootstrap_components as dbc
import yaml as _yaml
from dash import ALL, Input, Output, State, ctx, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from src import charts
from src.platform import adapters
from src.platform.components import kpi_card

# ── Per-run cache — CLAUDE.md §2.1: "a small per-run cache keyed by
# (run_id, state_version) is fine". Holds at most one run's bundle: a
# workspace tab is only ever looking at one run_id at a time. ─────────────────
_CACHE: dict[tuple, dict] = {}

# Skill *definitions* (plan.yaml/catalogue.yaml), not run data — static per
# skill_id for the life of this process, same shape as
# src.platform.adapters' module-level `_ctx` (a build-once handle, not audit
# evidence). A Skill's own plan never changes while this container is up;
# nothing here is a DataFrame or run-level number.
_SKILL_FLAG_CACHE: dict[str, dict] = {}

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_DIR = _REPO_ROOT / "skills"

_SEVERITY_COLOR = {"High": "#b85042", "Medium": "#e0952a", "Low": "#2c7a4b"}
_ACTION_STATUS_COLOR = {"draft": "#b85042", "open": "#b85042", "under_review": "#e0952a",
                         "agreed": "#1c7293", "remediated": "#2c7a4b", "closed": "#6b7283"}

_AMOUNT_COL = "Expense Amount (reimbursement currency)"
_DATE_COL = "Transaction Date"

_EXPENSE_FRAME_CANDIDATES = ["expense_report", "expense", "combined"]
_PRE_APPROVAL_FRAME_CANDIDATES = ["travel_requests_no_expense", "pre_approval", "preapproval"]
_APPROVAL_FRAME_CANDIDATES = ["approval_aging", "approval"]

# T4.2's own tested population (skills/tne_exco/plan.yaml `t42_pop`) — used
# here only to decide which claims count as "entertainment claims" for the
# missing-attendee-% denominator on the Executive-analysis tab; the actual
# exception signal is RF_ATT_Missing itself, produced by the real test.
_ENTERTAINMENT_EXPENSE_TYPES = [
    "Staff/Client Function: Offsite Food/Drink",
    "Staff/Client Function: Onsite Food/Drink",
    "Staff/Client Function: No Food/No Drink",
]


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
    try:
        actions = adapters.list_management_actions(filters={"run_id": run_id}) or []
    except Exception:
        actions = []

    bundle = {"run": run, "payload": payload, "frames": frames, "skill": skill or {}, "actions": actions}
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


# ── Skill plan → flag metadata (drill-down + breach labelling) ──────────────

def _skill_dir(skill_id: str) -> Path | None:
    if not _SKILLS_DIR.is_dir():
        return None
    for d in sorted(_SKILLS_DIR.iterdir()):
        manifest_path = d / "manifest.yaml"
        if manifest_path.is_file():
            try:
                data = _yaml.safe_load(manifest_path.read_text()) or {}
            except Exception:
                continue
            if data.get("id") == skill_id:
                return d
    return None


def _catalogue_id_for(plan_test_id: str, catalogue_ids: list[str]) -> str:
    for cid in catalogue_ids:
        if plan_test_id == cid or plan_test_id.startswith(cid + "_"):
            return cid
    return plan_test_id


def _skill_flag_meta(skill_id: str | None, catalogue_tests: list[dict]) -> dict[str, dict]:
    """{flag: {"test_id": <catalogue test_id>, "plan_test_id": <plan.yaml test_id>,
    "category": ..., "label": "T-id: name"}}.

    A finding's own `test_id` (skills/tne_exco/findings.yaml) is not always
    the catalogue id -- most rules cite the whole catalogue test ("T4.1"),
    but a few cite one specific plan.yaml sub-test directly (e.g.
    "T3.2a_air_dom", "T6.1d_dom", "T3.3a_dom", each with its own finding
    text). `plan_test_ids` (plural -- see below) lets `_flags_for_test` match
    both shapes without guessing.

    Prefers get_skill()'s own `tests[].plan_tests` (list form, per catalogue
    test) once the service exposes it; `flag_to_test` (a flat {flag:
    plan_test_id} dict) is used only as a fallback, because it is lossy: a
    flag can be shared by more than one plan.yaml sub-test (T6.1d_dom and
    T6.1d_int both set `flag: RF_CS_DailySpendOverLimit`), and a dict keyed
    by flag can hold only the last one written. `plan_tests` has no such
    collision -- it is a list, grouped by catalogue test already. Falls back
    to reading skills/<id>/plan.yaml directly when neither is present (see
    module docstring)."""
    if not skill_id:
        return {}
    cached = _SKILL_FLAG_CACHE.get(skill_id)
    if cached is not None:
        return cached

    catalogue_ids = [t.get("test_id") for t in catalogue_tests if t.get("test_id")]
    catalogue_by_id = {t.get("test_id"): t for t in catalogue_tests}

    skill_entry = adapters.get_skill(skill_id) or {}
    plan_entries: list[dict] = []  # [{"test_id": <plan.yaml id>, "flag": ...}, ...]
    for t in catalogue_tests:
        plan_entries.extend(t.get("plan_tests") or [])

    if not plan_entries:
        flag_to_test: dict[str, str] = skill_entry.get("flag_to_test") or {}
        if flag_to_test:
            plan_entries = [{"test_id": plan_test_id, "flag": flag} for flag, plan_test_id in flag_to_test.items()]
        else:
            d = _skill_dir(skill_id)
            if d is not None:
                try:
                    from orchestrator.skills import load_skill
                    skill = load_skill(d)
                    for t in skill.plan.get("tests", []):
                        if t.get("flag"):
                            plan_entries.append({"test_id": t.get("test_id", ""), "flag": t["flag"]})
                        elif "not_testable" in t:
                            for f in t["not_testable"].get("flags", []):
                                plan_entries.append({"test_id": t.get("test_id", ""), "flag": f})
                except Exception:
                    pass

    meta: dict[str, dict] = {}
    for e in plan_entries:
        flag = e.get("flag")
        plan_test_id = e.get("test_id", "")
        if not flag:
            continue
        cid = _catalogue_id_for(plan_test_id, catalogue_ids)
        entry = catalogue_by_id.get(cid, {})
        if flag not in meta:
            meta[flag] = {"test_id": cid, "plan_test_ids": [], "category": entry.get("category"),
                          "label": f"{cid}: {entry.get('test_name', cid)}"}
        if plan_test_id and plan_test_id not in meta[flag]["plan_test_ids"]:
            meta[flag]["plan_test_ids"].append(plan_test_id)

    _SKILL_FLAG_CACHE[skill_id] = meta
    return meta


def _flag_labels_on(df: pd.DataFrame | None, meta: dict[str, dict]) -> dict[str, str]:
    if df is None:
        return {}
    return {flag: m["label"] for flag, m in meta.items() if flag in df.columns}


def _flags_for_test(meta: dict[str, dict], test_id: str) -> set[str]:
    """Matches a finding's own test_id, which is either the catalogue id
    ("T4.1", matching m["test_id"]) or one specific plan.yaml sub-test id
    ("T3.2a_air_dom", "T6.1d_dom", matching one of m["plan_test_ids"]) --
    see _skill_flag_meta."""
    return {
        flag for flag, m in meta.items()
        if m.get("test_id") == test_id or test_id in m.get("plan_test_ids", ())
    }


def _frame_for_flags(frames: dict, flags: set[str]) -> tuple[str | None, pd.DataFrame | None]:
    for source, df in frames.items():
        if any(f in df.columns for f in flags):
            return source, df
    return None, None


def _any_flag_mask(df: pd.DataFrame) -> pd.Series:
    flag_cols = [c for c in df.columns if c.startswith("RF_")]
    if not flag_cols:
        return pd.Series(False, index=df.index)
    return df[flag_cols].apply(pd.to_numeric, errors="coerce").fillna(0).astype(int).sum(axis=1) > 0


def _filter_by_dates(df: pd.DataFrame, date_col: str, start_date, end_date) -> pd.DataFrame:
    out = df
    if date_col not in out.columns:
        return out
    if start_date:
        out = out[out[date_col] >= pd.to_datetime(start_date)]
    if end_date:
        out = out[out[date_col] <= pd.to_datetime(end_date)]
    return out


def _date_bounds(df: pd.DataFrame | None, date_col: str, fallback: tuple[str, str]) -> tuple[str, str]:
    if df is None or date_col not in df.columns or df.empty:
        return fallback
    s = pd.to_datetime(df[date_col], errors="coerce").dropna()
    if s.empty:
        return fallback
    return s.min().strftime("%Y-%m-%d"), s.max().strftime("%Y-%m-%d")


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
    """Mirrors orchestrator.findings.format_metric_value (the formatter a
    finding's own observation/recommendation prose is built with) so a
    metric reads the same way in the evidence drawer as it does in the
    finding text it came from."""
    if value is None:
        return "n/a"
    if unit == "AUD":
        return f"${value:,.2f}" if isinstance(value, (int, float)) else str(value)
    if unit == "%":
        return f"{value}%"
    if unit == "count" and isinstance(value, (int, float)):
        return _fmt_count(value)
    if isinstance(value, (int, float)):
        return f"{value:,}"
    return str(value)


def _fmt_currency(value) -> str:
    return f"${value:,.0f}" if isinstance(value, (int, float)) else "n/a"


def _fmt_count(value) -> str:
    """A count-unit metric (CLAUDE.md build brief P3 §2's total_records etc.)
    may come back as a float from a SQL aggregation (152921.0) even though
    it is conceptually an integer -- render it as one rather than
    "152,921.0"."""
    return f"{int(round(value)):,}" if isinstance(value, (int, float)) else str(value)


def _table_columns(df: pd.DataFrame) -> list[dict]:
    return [{"name": c, "id": c} for c in df.columns]


# ── Header ────────────────────────────────────────────────────────────────

def _metric_value(payload: dict | None, name: str):
    return ((payload or {}).get("metrics") or {}).get(name, {}).get("value")


def _build_header(run: dict, payload: dict | None = None) -> html.Div:
    period = run.get("audit_period") or ["—", "—"]

    # Run-level metrics (CLAUDE.md build brief P3 §2 "Run-level metrics (all
    # computed)"): total_records/total_files/months_covered — always read
    # from this run's own persisted metrics, "n/a" when a run predates them
    # rather than a hardcoded population count (CLAUDE.md §0.2, N6).
    total_records = _metric_value(payload, "total_records")
    total_files = _metric_value(payload, "total_files")
    months_covered = _metric_value(payload, "months_covered")
    scope_chips = []
    if total_records is not None:
        scope_chips.append(html.Span(f"{_fmt_count(total_records)} records", className="chip"))
    if total_files is not None:
        scope_chips.append(html.Span(f"{_fmt_count(total_files)} source files", className="chip"))
    if months_covered is not None:
        scope_chips.append(html.Span(f"{_fmt_count(months_covered)} months covered", className="chip"))

    return html.Div(
        html.Header([
            html.Div([
                html.Div([
                    html.H1("Travel & Entertainment Executive Diligence", className="page-title"),
                    html.P("Reproducible calculations · Evidence-linked findings · Management questions",
                           className="page-subtitle"),
                ]),
                html.Div([
                    html.Label("View mode", style={"fontSize": 10, "fontWeight": 700, "letterSpacing": "0.5px",
                                                    "textTransform": "uppercase", "color": "#6b7283", "marginBottom": 2}),
                    dcc.Dropdown(
                        id="tne-audience-mode",
                        options=[
                            {"label": "Executive", "value": "executive"},
                            {"label": "Audit Manager", "value": "audit"},
                            {"label": "Investigator", "value": "investigator"},
                        ],
                        value="executive", clearable=False, style={"width": 160, "fontSize": 13},
                    ),
                ], style={"display": "flex", "flexDirection": "column", "alignItems": "flex-end"}),
            ], style={"display": "flex", "justifyContent": "space-between", "alignItems": "flex-start"}),
            html.Div([
                html.Span(f"Run: {run.get('run_id', '')}", className="chip mono"),
                html.Span(f"Skill: {run.get('skill_id', '—')} v{run.get('skill_version', '—')}", className="chip"),
                html.Span(f"Audit period: {period[0]} – {period[1]}", className="chip"),
                html.Span(f"Owner: {run.get('run_owner', '—')}", className="chip"),
                html.Span(f"Status: {run.get('status_label') or run.get('status', '—')}", className="chip"),
                *scope_chips,
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


def _executive_tab(run: dict, findings: list[dict], payload: dict | None, actions: list[dict], frames: dict | None = None) -> html.Div:
    n_high = sum(1 for f in findings if f.get("severity") == "High")
    n_med = sum(1 for f in findings if f.get("severity") == "Medium")
    n_low = sum(1 for f in findings if f.get("severity") == "Low")
    open_actions = sum(1 for a in actions if str(a.get("status", "")).lower() not in ("closed", "remediated"))
    n_exception_tests = sum(1 for t in run.get("test_results", []) if t.get("status") == "exception")

    priority = sorted(findings, key=lambda f: (
        {"High": 0, "Medium": 1, "Low": 2}.get(f.get("severity"), 3),
        -(f.get("exposure_amount") or 0),
    ))[:5]
    th_style = {"fontSize": 10, "fontWeight": 700, "letterSpacing": "0.5px", "textTransform": "uppercase",
                "color": "#6b7283", "padding": "0 10px 7px", "borderBottom": "1px solid #e4e7ee"}
    priority_rows = [
        html.Tr([
            html.Td(html.Span(f.get("severity", "—"), className="chip",
                               style={"color": _SEVERITY_COLOR.get(f.get("severity"), "#6b7283"),
                                      "borderColor": _SEVERITY_COLOR.get(f.get("severity"), "#6b7283")})),
            html.Td(f.get("title", ""), style={"fontWeight": 600}),
            html.Td(_fmt_currency(f.get("exposure_amount")) if f.get("exposure_amount") is not None else "—",
                    style={"fontFamily": "monospace", "textAlign": "right"}),
        ])
        for f in priority
    ]

    action_status_counts: dict[str, int] = {}
    for a in actions:
        s = str(a.get("status") or "draft")
        action_status_counts[s] = action_status_counts.get(s, 0) + 1
    action_summary = html.Div([
        html.Div([
            html.Div(status.replace("_", " ").title(), className="action-summary-label"),
            html.Div(str(count), className="action-summary-value",
                      style={"color": _ACTION_STATUS_COLOR.get(status, "#6b7283")}),
        ], className="action-summary-item")
        for status, count in sorted(action_status_counts.items())
    ], className="action-summary") if actions else html.P(
        "No management actions drafted for this run yet.", style={"color": "#6b7283", "fontSize": 13})

    expense_df = _pick_frame(frames or {}, _EXPENSE_FRAME_CANDIDATES)
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
            kpi_card("Findings", str(len(findings))),
            kpi_card("High", str(n_high)),
            kpi_card("Medium", str(n_med)),
            kpi_card("Low", str(n_low)),
            kpi_card("Tests with exceptions", str(n_exception_tests)),
            kpi_card("Open management actions", str(open_actions)),
        ], className="plat-kpi-row", style={"marginBottom": 16}),

        html.Div([
            html.Div([
                html.H3("Risk distribution", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                html.P("Priority is determined from severity and de-duplicated financial exposure.", className="sub"),
                dcc.Graph(id="tne-exec-severity-donut", figure=charts.findings_by_severity_donut(findings),
                          config={"displayModeBar": False}),
            ], className="panel"),
            html.Div([
                html.H3("T&E volume trend", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                html.P("A change in activity helps frame the scale and timing of exceptions.", className="sub"),
                dcc.Graph(id="tne-exec-monthly", figure=charts.monthly_volume_chart(expense_df),
                          config={"displayModeBar": False}),
            ], className="panel"),
        ], className="grid-2", style={"marginBottom": 16}),

        html.Div([
            html.Div([
                html.H3("Matters requiring attention", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                html.P("Open Findings & Actions to inspect evidence, assign ownership, or prepare the executive pack.",
                       className="sub"),
                html.Table([
                    html.Thead(html.Tr([
                        html.Th("Severity", style=th_style), html.Th("Matter", style=th_style),
                        html.Th("Exposure", style={**th_style, "textAlign": "right"}),
                    ])),
                    html.Tbody(priority_rows),
                ], style={"width": "100%", "borderCollapse": "collapse", "fontSize": 12.5}),
            ], className="panel"),
            html.Div([
                html.H3("Management action status", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                html.P("Real, persisted management actions drafted from this run's findings.", className="sub"),
                action_summary,
            ], className="panel"),
        ], className="grid-2"),

        html.Div([
            html.H3("Run signoff", style={"margin": "0 0 6px", "fontSize": 14.5, "fontWeight": 700}),
            html.P(
                (f"Signed off by {run['signoff']['approver']} at {run['signoff']['timestamp']}"
                 if run.get("signoff") else "Not yet signed off."),
                className="sub",
            ),
        ], className="panel", style={"marginTop": 16}),
    ])


# ── Findings & Evidence sub-tab ──────────────────────────────────────────────

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


def _category_for_test(test_id: str, tests: list[dict]) -> str | None:
    for t in tests:
        tid = t.get("test_id", "")
        if tid == test_id or test_id.startswith(tid + "_") or tid.startswith(test_id + "_"):
            return t.get("category")
    return None


def _render_filtered_findings(findings: list[dict], severity_filter, category_filter, sort_by, tests: list[dict] | None = None) -> list:
    tests = tests or []
    filtered = [
        (idx, f) for idx, f in enumerate(findings)
        if (not severity_filter or f.get("severity") in severity_filter)
        and (not category_filter or _category_for_test(f.get("test_id", ""), tests) in category_filter)
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
            html.Summary(f"Show {len(remaining)} additional findings", className="ghost further-analysis"),
            html.Div(remaining, className="stack", style={"marginTop": 12}),
        ]))
    return children


def _findings_analytics(frames: dict, findings: list[dict], meta: dict[str, dict]) -> html.Div:
    """The prototype's 6-chart 'Analytics' grid + spend-outlier table
    (reference_app/app.py's `_build_insights_figures` / OUTLIER_DF), ported
    onto the run's own expense_report and approval_aging frames."""
    expense_df = _pick_frame(frames, _EXPENSE_FRAME_CANDIDATES)
    approval_df = _pick_frame(frames, _APPROVAL_FRAME_CANDIDATES)
    flag_labels = _flag_labels_on(expense_df, meta)

    approver_donut = charts.empty_figure("Approver review quality — no data")
    if approval_df is not None and {"Report Receipt Viewed", "All Entry Receipts Viewed"}.issubset(approval_df.columns):
        status, _viewed = charts.approver_receipt_status(approval_df, "Report Receipt Viewed", "All Entry Receipts Viewed")
        approver_donut = charts.approver_status_donut(status, title="Approver review quality")

    chart_specs = [
        ("tne-fe-monthly", "Monthly T&E volume", charts.monthly_volume_chart(expense_df)),
        ("tne-fe-severity", "Findings by risk level", charts.findings_by_severity_donut(findings)),
        ("tne-fe-top-type", "Top 10 expense types by spend",
         charts.top_n_bar(expense_df, "Expense Type", _AMOUNT_COL, "Top 10 expense types by spend")),
        ("tne-fe-by-member", "Policy exceptions by ExCo member",
         charts.exceptions_by_group_chart(expense_df, flag_labels, "Employee", "Policy exceptions by ExCo member")),
        ("tne-fe-approver", "Approver review quality", approver_donut),
        ("tne-fe-top-spenders", "Top 10 spenders",
         charts.top_n_bar(expense_df, "Employee", _AMOUNT_COL, "Top 10 spenders", color=charts.PALETTE[0])),
    ]
    chart_panels = [
        html.Div([
            html.H3(title, style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
            dcc.Graph(id=fig_id, figure=fig, config={"displayModeBar": False}),
        ], className="panel")
        for fig_id, title, fig in chart_specs
    ]

    outliers = charts.outliers_table(expense_df) if expense_df is not None else pd.DataFrame()
    outlier_panel = html.Div([
        html.H3("Spend outliers (Z-score > 2.0)", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
        html.P("Claims that are statistically unusual relative to the employee's own spending pattern.",
               className="sub"),
        dash_table.DataTable(
            data=outliers.to_dict("records"), columns=_table_columns(outliers),
            page_size=10, sort_action="native", style_table={"overflowX": "auto"},
            style_cell={"fontSize": 11.5, "padding": "5px 8px"},
            style_data_conditional=[{"if": {"filter_query": "{Z-Score} > 3"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"}],
        ),
    ], className="panel", style={"marginTop": 16}) if not outliers.empty else None

    return html.Div([
        html.H2("Analytics", style={"margin": "24px 0 12px", "fontSize": 17, "fontWeight": 700}),
        html.Div(chart_panels, className="grid-3", style={"alignItems": "start"}),
        outlier_panel,
    ])


def _findings_tab(bundle: dict) -> html.Div:
    findings = bundle["run"].get("findings", [])
    frames = bundle["frames"]
    tests = bundle["skill"].get("tests", [])
    payload = bundle["payload"]
    meta = _skill_flag_meta(bundle["run"].get("skill_id"), tests)

    n_high = sum(1 for f in findings if f.get("severity") == "High")
    n_med = sum(1 for f in findings if f.get("severity") == "Medium")
    n_low = sum(1 for f in findings if f.get("severity") == "Low")

    summary_bar = html.Div([
        html.Span(f"{len(findings)} findings", className="chip"),
        html.Span(f"{n_high} High · {n_med} Medium · {n_low} Low", className="chip"),
        html.Span(f"Exposure: {_exposure_summary(findings, payload)}", className="chip",
                   style={"color": "#b85042", "borderColor": "#b85042"}) if findings else None,
    ], className="chip-row", style={"marginBottom": 14})

    categories = sorted({c for c in (t.get("category") for t in tests) if c})
    severities = sorted({f.get("severity") for f in findings if f.get("severity")})
    filters_row = html.Div([
        dcc.Dropdown(id="tne-severity-filter",
                     options=[{"label": s, "value": s} for s in severities],
                     multi=True, placeholder="All severities", style={"minWidth": 160}),
        dcc.Dropdown(id="tne-category-filter",
                     options=[{"label": c, "value": c} for c in categories],
                     multi=True, placeholder="All categories", style={"minWidth": 180}),
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
        summary_bar,
        filters_row,
        export_row,
        html.Div(_render_filtered_findings(findings, [], [], "severity", tests),
                  id="tne-filtered-findings", className="stack"),
        _findings_analytics(frames, findings, meta),
    ])


# ── Management actions sub-tab ───────────────────────────────────────────────
#
# `management_actions` rows are real and persisted (orchestrator's `act`
# node drafts one per finding, status "draft"). There is no service-layer
# write path yet to update owner/status/target date/response from the UI
# (orchestrator/service.py's public API has no update_management_action —
# see this task's report), so — exactly like reference_app/app.py's own
# Management Action Tracker, which the prototype itself labelled "For
# showcase use · session-only data resets when the app restarts" — edits
# here are kept in a session dcc.Store and merged over the real rows for
# display, never written back as if persisted (CLAUDE.md NN13: no fake
# successful integration).

def _action_row_view(action: dict, override: dict | None) -> dict:
    override = override or {}
    return {
        "action_id": action.get("action_id"),
        "finding_id": action.get("finding_id"),
        "title": action.get("finding_title") or action.get("title", ""),
        "risk": action.get("risk", ""),
        "status": override.get("status") or action.get("status") or "draft",
        "owner": override.get("owner") if override.get("owner") is not None else (action.get("owner") or ""),
        "target_date": override.get("target_date") or action.get("target_date") or "",
        "response": override.get("response") if override.get("response") is not None else (action.get("description") or ""),
        "potential_exposure": action.get("potential_exposure"),
    }


def _render_mgmt_tracker(actions: list[dict], overrides: dict) -> html.Div:
    if not actions:
        return html.Div("No management actions drafted for this run yet.", className="panel",
                         style={"color": "#6b7283", "fontSize": 13})
    rows = []
    for a in actions:
        view = _action_row_view(a, overrides.get(a.get("action_id")))
        status = view["status"]
        status_color = _ACTION_STATUS_COLOR.get(status, "#6b7283")
        rows.append(html.Tr([
            html.Td(a.get("evidence_link") or "—", style={"fontFamily": "monospace", "fontSize": 11, "fontWeight": 600}),
            html.Td(view["title"], style={"fontSize": 12}),
            html.Td(html.Span(view["risk"], style={"color": _SEVERITY_COLOR.get(view["risk"], "#6b7283"), "fontWeight": 600}), style={"fontSize": 12}),
            html.Td(html.Span(status.replace("_", " ").title(), style={"color": status_color, "fontWeight": 600}), style={"fontSize": 12}),
            html.Td(view["owner"] or "—", style={"fontSize": 12, "color": "#6b7283"}),
            html.Td(view["target_date"] or "—", style={"fontSize": 12, "color": "#6b7283", "whiteSpace": "nowrap"}),
            html.Td(_fmt_currency(view["potential_exposure"]) if view["potential_exposure"] is not None else "—",
                    style={"fontSize": 12, "color": "#6b7283", "fontFamily": "monospace"}),
            html.Td(html.Button("Edit", id={"type": "tne-edit-action-btn", "index": a.get("action_id")},
                                className="ghost", style={"fontSize": 11})),
        ]))

    _th = {"fontSize": 10, "fontWeight": 700, "letterSpacing": "0.5px", "textTransform": "uppercase",
           "color": "#6b7283", "paddingBottom": 5, "borderBottom": "2px solid #e4e7ee", "paddingRight": 12}
    return html.Div([
        html.Table([
            html.Thead(html.Tr([
                html.Th("Test", style=_th), html.Th("Finding", style=_th), html.Th("Risk", style=_th),
                html.Th("Status", style=_th), html.Th("Owner", style=_th), html.Th("Target", style=_th),
                html.Th("Exposure", style=_th), html.Th("", style=_th),
            ])),
            html.Tbody(rows),
        ], style={"borderCollapse": "collapse", "width": "100%", "fontSize": 12.5}),
    ], style={"overflowX": "auto"})


def _actions_tab(bundle: dict) -> html.Div:
    actions = bundle.get("actions", [])
    return html.Div([
        html.Div([
            html.H2("Management Action Tracker", style={"margin": 0, "fontSize": 17, "fontWeight": 700}),
            html.Span("Rows are the run's own persisted management actions. Owner/status/target-date/response "
                      "edits are session-only (no update endpoint yet — see this workspace's build report) "
                      "and reset when the app restarts, same as the prototype's tracker.",
                      style={"fontSize": 12, "color": "#6b7283"}),
        ], style={"marginBottom": 16}),
        dcc.Store(id="tne-action-overrides", storage_type="session", data={}),
        dcc.Store(id="tne-selected-action", storage_type="memory", data=None),
        html.Div(_render_mgmt_tracker(actions, {}), id="tne-mgmt-tracker-body"),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle(id="tne-action-modal-title")),
            dbc.ModalBody([
                html.Div(id="tne-action-modal-finding", style={"fontSize": 12.5, "color": "#6b7283", "marginBottom": 14}),
                html.Label("Action owner", className="table-title"),
                dcc.Input(id="tne-action-owner", type="text", placeholder="Accountable executive or business unit",
                          style={"width": "100%", "marginBottom": 12}),
                html.Label("Status", className="table-title"),
                dcc.Dropdown(id="tne-action-status", options=[
                    {"label": "Draft", "value": "draft"}, {"label": "Open", "value": "open"},
                    {"label": "Under review", "value": "under_review"}, {"label": "Agreed", "value": "agreed"},
                    {"label": "Remediated", "value": "remediated"}, {"label": "Closed", "value": "closed"},
                ], value="draft", clearable=False, style={"marginBottom": 12}),
                html.Label("Target date", className="table-title"),
                dcc.DatePickerSingle(id="tne-action-target-date", display_format="DD MMM YYYY", style={"marginBottom": 12}),
                html.Label("Management response", className="table-title"),
                dcc.Textarea(id="tne-action-response", placeholder="Agreed response, next step, or rationale",
                             style={"width": "100%", "height": 110}),
            ]),
            dbc.ModalFooter([
                html.Button("Cancel", id="tne-action-cancel", className="ghost", style={"width": "auto"}),
                html.Button("Save action", id="tne-action-save", className="btn-generate"),
            ]),
        ], id="tne-action-modal", is_open=False, size="lg", centered=True),
    ])


# ── Audit Detail / Executive analysis (page 1) ───────────────────────────────

def _p1_layout(bundle: dict) -> html.Div:
    expense_df = _pick_frame(bundle["frames"], _EXPENSE_FRAME_CANDIDATES)
    period = bundle["run"].get("audit_period") or ["2025-01-01", "2026-04-30"]
    start, end = _date_bounds(expense_df, _DATE_COL, tuple(period))
    members = sorted(expense_df["Employee"].dropna().unique()) if expense_df is not None and "Employee" in expense_df.columns else []

    return html.Div([
        html.Div([
            dcc.DatePickerRange(id="tne-p1-date", start_date=start, end_date=end, display_format="DD MMM YYYY"),
            dcc.Dropdown(id="tne-p1-member", options=[{"label": m, "value": m} for m in members],
                         multi=True, placeholder="All ExCo members"),
        ], className="filter-row"),
        html.Div(id="tne-p1-kpis", className="grid-4 mb-3"),
        html.Div([
            html.Div([dcc.Graph(id="tne-p1-monthly")], className="panel"),
            html.Div([dcc.Graph(id="tne-p1-breach-by-member")], className="panel"),
        ], className="grid-2"),
        html.Div([
            html.Div([dcc.Graph(id="tne-p1-breach-dist")], className="panel"),
            html.Div([dcc.Graph(id="tne-p1-missing-tier")], className="panel"),
        ], className="grid-2 mt-3"),
        html.Div([
            html.Div([dcc.Graph(id="tne-p1-precomp")], className="panel"),
            html.Div([
                html.Div("Missing attendee table", className="table-title"),
                dash_table.DataTable(id="tne-p1-attendee-table", page_size=12, style_table={"overflowX": "auto"},
                                      style_cell={"fontSize": 11.5, "padding": "5px 8px"}),
            ], className="panel"),
        ], className="grid-2 mt-3"),
        html.Details([
            html.Summary("View evidence", className="ghost"),
            html.Div(id="tne-p1-evidence", className="panel mt-2"),
        ], className="mt-3"),
    ])


def _p1_update(bundle: dict, start_date, end_date, members, meta: dict[str, dict]):
    expense_df = _pick_frame(bundle["frames"], _EXPENSE_FRAME_CANDIDATES)
    pre_df = _pick_frame(bundle["frames"], _PRE_APPROVAL_FRAME_CANDIDATES)
    if expense_df is None:
        empty = charts.empty_figure("No data")
        return ([], empty, empty, empty, empty, empty, [], [], [],
                [html.P("No expense_report frame was returned for this run.", style={"color": "#6b7283"})])

    df = _filter_by_dates(expense_df, _DATE_COL, start_date, end_date)
    if members:
        df = df[df["Employee"].isin(members)]
    breach_mask = _any_flag_mask(df)
    flag_labels = _flag_labels_on(df, meta)

    amount_sum = df[_AMOUNT_COL].sum() if _AMOUNT_COL in df.columns else None
    breach_amount = df.loc[breach_mask, _AMOUNT_COL].sum() if _AMOUNT_COL in df.columns else None
    missing_col = "RF_CS_MissingReceipt"
    miss_mask = pd.to_numeric(df.get(missing_col, pd.Series(0, index=df.index)), errors="coerce").fillna(0).astype(int) == 1

    kpis = [
        kpi_card("Total T&E spend", _fmt_currency(amount_sum) if amount_sum is not None else "n/a"),
        kpi_card("Total breach count", f"{int(breach_mask.sum()):,}"),
        kpi_card("Breach amount ($)", _fmt_currency(breach_amount) if breach_amount is not None else "n/a"),
        kpi_card("Missing receipts", f"{int(miss_mask.sum()):,}"),
    ]

    f1 = charts.monthly_volume_chart(df, title="Monthly trend")
    f2 = charts.exceptions_by_group_chart(df, flag_labels, "Employee", "Breach count by ExCo member")
    f3 = charts.exceptions_by_flag_distribution(df, flag_labels, "Breach type distribution")
    f4 = charts.missing_by_tier_chart(df, _AMOUNT_COL, missing_col, "Missing receipts by claim-size tier")
    f5 = charts.grouped_count_chart(df, pre_df, "Employee", "Claims", "Pre-approvals", "Travel pre-request compliance")

    ent = df[df["Expense Type"].isin(_ENTERTAINMENT_EXPENSE_TYPES)] if "Expense Type" in df.columns else df.iloc[0:0]
    att_flag = "RF_ATT_Missing"
    if not ent.empty and "Employee" in ent.columns:
        ent_claims = ent.groupby("Employee", as_index=False).size().rename(columns={"size": "Entertainment Claims"})
        if att_flag in ent.columns:
            ent = ent.assign(_att_missing=pd.to_numeric(ent[att_flag], errors="coerce").fillna(0))
            missing_att = ent.groupby("Employee", as_index=False).agg(
                **{"Missing Attendee Count": ("_att_missing", "sum")}
            )
        else:
            missing_att = pd.DataFrame({"Employee": [], "Missing Attendee Count": []})
        tab = ent_claims.merge(missing_att, on="Employee", how="left").fillna(0)
        tab["Missing %"] = tab.apply(
            lambda r: round(r["Missing Attendee Count"] / r["Entertainment Claims"] * 100, 1) if r["Entertainment Claims"] else 0,
            axis=1,
        )
    else:
        tab = pd.DataFrame(columns=["Employee", "Entertainment Claims", "Missing Attendee Count", "Missing %"])

    style_cond = [{"if": {"filter_query": "{Missing %} > 80"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"}]
    cols = _table_columns(tab)

    evidence = [
        html.P(f"Rows in scope: {len(df):,}"),
        html.P(f"Rows flagged as breaches: {int(breach_mask.sum()):,}"),
        html.P("Breach category derivation uses this run's own RF_* flags, joined to the Skill's catalogue "
               "test names (skills/tne_exco/catalogue.yaml) — never a fixed, prototype-specific flag list."),
    ]

    return kpis, f1, f2, f3, f4, f5, tab.to_dict("records"), cols, style_cond, evidence


# ── Audit Detail / Detailed risk (page 2) ────────────────────────────────────

def _p2_layout(bundle: dict) -> html.Div:
    expense_df = _pick_frame(bundle["frames"], _EXPENSE_FRAME_CANDIDATES)
    members = sorted(expense_df["Employee"].dropna().unique()) if expense_df is not None and "Employee" in expense_df.columns else []
    expense_types = sorted(expense_df["Expense Type"].dropna().unique()) if expense_df is not None and "Expense Type" in expense_df.columns else []
    period = bundle["run"].get("audit_period") or ["2025-01-01", "2026-04-30"]
    start, end = _date_bounds(expense_df, _DATE_COL, tuple(period))

    return html.Div([
        html.Div([
            dcc.DatePickerRange(id="tne-p2-date", start_date=start, end_date=end, display_format="DD MMM YYYY"),
            dcc.Dropdown(id="tne-p2-member", options=[{"label": m, "value": m} for m in members],
                         multi=True, placeholder="All employees"),
            dcc.Dropdown(id="tne-p2-expense", options=[{"label": m, "value": m} for m in expense_types],
                         multi=True, placeholder="All expense types"),
        ], className="filter-row"),
        html.Div(id="tne-p2-kpis", className="grid-4 mb-3"),
        html.Div([
            html.Div([dcc.Graph(id="tne-p2-spend-by-employee")], className="panel"),
            html.Div([
                html.Div("Top vendors", className="table-title"),
                dash_table.DataTable(id="tne-p2-vendor-table", page_size=15, sort_action="native",
                                      style_table={"overflowX": "auto"}, style_cell={"fontSize": 11.5, "padding": "5px 8px"}),
            ], className="panel"),
        ], className="grid-2"),
        html.Div([
            html.Div([dcc.Graph(id="tne-p2-breach-expense")], className="panel"),
            html.Div([
                html.Div("Detailed exception table", className="table-title"),
                dash_table.DataTable(id="tne-p2-exception-table", page_size=20, sort_action="native",
                                      filter_action="native", export_format="csv",
                                      style_table={"overflowX": "auto"}, style_cell={"fontSize": 11.5, "padding": "5px 8px"}),
            ], className="panel"),
        ], className="grid-2 mt-3"),
        html.Div([
            html.Div([
                html.Div("Claims vs pre-approvals", className="table-title"),
                dash_table.DataTable(id="tne-p2-claim-pre-table", page_size=12, sort_action="native",
                                      style_table={"overflowX": "auto"}, style_cell={"fontSize": 11.5, "padding": "5px 8px"}),
            ], className="panel"),
            html.Div([
                html.Div("Missing receipt by employee", className="table-title"),
                dash_table.DataTable(id="tne-p2-missing-table", page_size=12, sort_action="native",
                                      style_table={"overflowX": "auto"}, style_cell={"fontSize": 11.5, "padding": "5px 8px"}),
            ], className="panel"),
        ], className="grid-2 mt-3"),
    ])


def _p2_update(bundle: dict, start_date, end_date, members, expense_types, meta: dict[str, dict]):
    expense_df = _pick_frame(bundle["frames"], _EXPENSE_FRAME_CANDIDATES)
    pre_df = _pick_frame(bundle["frames"], _PRE_APPROVAL_FRAME_CANDIDATES)
    if expense_df is None:
        empty = charts.empty_figure("No data")
        return ([], empty, [], [], [], empty, [], [], [], [], [], [], [], [])

    df = _filter_by_dates(expense_df, _DATE_COL, start_date, end_date)
    if members:
        df = df[df["Employee"].isin(members)]
    if expense_types:
        df = df[df["Expense Type"].isin(expense_types)]
    breach_mask = _any_flag_mask(df)
    flag_labels = _flag_labels_on(df, meta)

    avg_claim = df[_AMOUNT_COL].mean() if _AMOUNT_COL in df.columns and len(df) else None
    hv_count = int((df[_AMOUNT_COL] > 5000).sum()) if _AMOUNT_COL in df.columns else 0
    kpis = [
        kpi_card("Total claims", f"{len(df):,}"),
        kpi_card("Unique employees", f"{df['Employee'].nunique():,}" if "Employee" in df.columns else "n/a"),
        kpi_card("Avg claim amount", _fmt_currency(avg_claim) if avg_claim is not None else "n/a"),
        kpi_card("High-value claims (>$5K)", f"{hv_count:,}"),
    ]

    f1 = charts.top_n_bar(df, "Employee", _AMOUNT_COL, "Spend by employee", n=15)

    vendor = pd.DataFrame()
    if "Vendor" in df.columns and _AMOUNT_COL in df.columns:
        vendor = df.groupby("Vendor", as_index=False).agg(
            Claim_Count=("Vendor", "count"), Total_Amount=(_AMOUNT_COL, "sum"),
            Avg_Claim=(_AMOUNT_COL, "mean"),
            ExCo_Members_Using=("Employee", "nunique") if "Employee" in df.columns else ("Vendor", "count"),
        ).sort_values("Total_Amount", ascending=False).head(15)
        vendor["Total_Amount"] = vendor["Total_Amount"].round(2)
        vendor["Avg_Claim"] = vendor["Avg_Claim"].round(2)
    vendor_style = [{"if": {"filter_query": "{Avg_Claim} > 2000"}, "backgroundColor": "#fff7e6", "color": "#8a4b00"}]

    f3 = charts.count_by_group_chart(df, breach_mask, "Expense Type", "Breach count by expense type")

    ex_cols = [c for c in ["Employee", _DATE_COL, "Vendor", "Expense Type", _AMOUNT_COL] if c in df.columns]
    ex_table = charts.exploded_exceptions_table(df, flag_labels, ex_cols)
    if not ex_table.empty and _AMOUNT_COL in ex_table.columns:
        ex_table = ex_table.rename(columns={_AMOUNT_COL: "Amount ($)"})

    cp = pd.DataFrame()
    if "Employee" in df.columns:
        claim_ag = df.groupby("Employee", as_index=False).agg(
            Claims_Filed=("Employee", "count"), Claims_Amount=(_AMOUNT_COL, "sum") if _AMOUNT_COL in df.columns else ("Employee", "count"))
        if pre_df is not None and "Employee" in pre_df.columns:
            pre_amount_col = "Total Approved Amount (rpt)" if "Total Approved Amount (rpt)" in pre_df.columns else None
            agg = {"Pre_Approvals": ("Employee", "count")}
            if pre_amount_col:
                agg["Pre_Approvals_Amount"] = (pre_amount_col, "sum")
            pre_ag = pre_df.groupby("Employee", as_index=False).agg(**agg)
        else:
            pre_ag = pd.DataFrame({"Employee": []})
        cp = claim_ag.merge(pre_ag, on="Employee", how="outer").fillna(0)
        cp["Gap"] = cp["Claims_Filed"] - cp.get("Pre_Approvals", 0)
    cp_style = [{"if": {"filter_query": "{Gap} > 5"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"}]

    miss = pd.DataFrame()
    if "Employee" in df.columns:
        miss_flag = "RF_CS_MissingReceipt"
        miss_numeric = pd.to_numeric(df[miss_flag], errors="coerce").fillna(0) if miss_flag in df.columns else 0
        agg = {"Total_Claims": ("Employee", "count")}
        d = df.assign(_miss=miss_numeric)
        agg2 = d.groupby("Employee", as_index=False).agg(
            Total_Claims=("Employee", "count"),
            Claims_Missing_Receipt=("_miss", "sum"),
            **({"Total_At_Risk": (_AMOUNT_COL, "sum")} if _AMOUNT_COL in df.columns else {}),
        )
        agg2["Missing %"] = agg2.apply(
            lambda r: round(r["Claims_Missing_Receipt"] / r["Total_Claims"] * 100, 1) if r["Total_Claims"] else 0, axis=1)
        miss = agg2.sort_values("Missing %", ascending=False)
    miss_style = [{"if": {"filter_query": "{Missing %} > 50"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"}]

    return (
        kpis, f1,
        vendor.to_dict("records"), _table_columns(vendor), vendor_style,
        f3,
        ex_table.to_dict("records"), _table_columns(ex_table),
        cp.to_dict("records"), _table_columns(cp), cp_style,
        miss.to_dict("records"), _table_columns(miss), miss_style,
    )


# ── Audit Detail / Receipt & approver review (page 3) ────────────────────────
#
# skills/tne_exco/contract.yaml's approval_aging source carries no claimant
# identity or amount column (Report ID / Approver ID / Approver Name / Step /
# Approved Date-Time / Approver Received Date / receipt-viewed flags /
# minutes-to-approval only) -- unlike reference_app's DF_APPROVAL, which
# fuzzy-joined in "Employee Name" / "Report Amount" via `_find_col` with a
# silent default when absent (reference_app/app.py:171-226, the exact
# NN14 behaviour this build removes). This tab reports what the real
# contract supports and says "n/a" for the rest (see this task's report).

_RECEIPT_VIEWED_COL = "Report Receipt Viewed"
_ENTRY_VIEWED_COL = "All Entry Receipts Viewed"
_APPROVER_COL = "Approver Name"
_APPROVED_DT_COL = "Approved Date/Time"
_INSUFFICIENT_FLAG = "RF_APR_InsufficientReview"


def _p3_layout(bundle: dict) -> html.Div:
    approval_df = _pick_frame(bundle["frames"], _APPROVAL_FRAME_CANDIDATES)
    approvers = (sorted(approval_df[_APPROVER_COL].dropna().unique())
                 if approval_df is not None and _APPROVER_COL in approval_df.columns else [])
    period = bundle["run"].get("audit_period") or ["2025-01-01", "2026-04-30"]
    start, end = _date_bounds(approval_df, _APPROVED_DT_COL, tuple(period))

    return html.Div([
        html.Div([
            dcc.DatePickerRange(id="tne-p3-date", start_date=start, end_date=end, display_format="DD MMM YYYY"),
            dcc.Dropdown(id="tne-p3-approver", options=[{"label": m, "value": m} for m in approvers],
                         multi=True, placeholder="All approvers"),
        ], className="filter-row"),
        html.Div(id="tne-p3-kpis", className="grid-4 mb-3"),
        html.Div([
            html.Div([dcc.Graph(id="tne-p3-donut")], className="panel"),
            html.Div([dcc.Graph(id="tne-p3-by-approver")], className="panel"),
        ], className="grid-2"),
        html.Div([
            html.Div([dcc.Graph(id="tne-p3-combo")], className="panel"),
            html.Div([dcc.Graph(id="tne-p3-risk-profile")], className="panel"),
        ], className="grid-2 mt-3"),
        html.Div([
            html.Div("Approval detail table", className="table-title"),
            dash_table.DataTable(id="tne-p3-detail", page_size=20, sort_action="native", filter_action="native",
                                  export_format="csv", style_table={"overflowX": "auto"},
                                  style_cell={"fontSize": 11.5, "padding": "5px 8px"}),
        ], className="panel mt-3"),
    ])


def _p3_update(bundle: dict, start_date, end_date, approvers):
    approval_df = _pick_frame(bundle["frames"], _APPROVAL_FRAME_CANDIDATES)
    required = {_RECEIPT_VIEWED_COL, _ENTRY_VIEWED_COL, _APPROVER_COL}
    if approval_df is None or not required.issubset(approval_df.columns):
        empty = charts.empty_figure("No data")
        return [], empty, empty, empty, empty, [], [], []

    df = _filter_by_dates(approval_df, _APPROVED_DT_COL, start_date, end_date)
    if approvers:
        df = df[df[_APPROVER_COL].isin(approvers)]
    if df.empty:
        empty = charts.empty_figure("No rows in the selected range")
        return [], empty, empty, empty, empty, [], [], []

    status, viewed = charts.approver_receipt_status(df, _RECEIPT_VIEWED_COL, _ENTRY_VIEWED_COL)
    insufficient = (pd.to_numeric(df[_INSUFFICIENT_FLAG], errors="coerce").fillna(0)
                     if _INSUFFICIENT_FLAG in df.columns else pd.Series(0, index=df.index))

    kpis = [
        kpi_card("Total approvers (ExCo)", f"{df[_APPROVER_COL].nunique():,}"),
        kpi_card("Overall receipt-viewed %", f"{(viewed.mean() * 100 if len(df) else 0):.1f}%"),
        kpi_card("Insufficient-review exceptions", f"{int(insufficient.sum()):,}"),
        kpi_card("Claimant identity in approval_aging", "n/a — contract has no claimant column"),
    ]

    f1 = charts.approver_status_donut(status, title="How approvers review receipts")
    f2 = charts.approver_status_by_approver(df[_APPROVER_COL], status, title="Receipt review by approver")
    f3 = charts.approver_review_combo(df[_APPROVER_COL], viewed, insufficient,
                                       title="Receipt viewed % vs insufficient-review %")
    f4 = charts.approver_risk_profile(df[_APPROVER_COL], viewed, title="Approver risk profile")

    detail_cols = [c for c in [_APPROVER_COL, "Report ID", "Step", _APPROVED_DT_COL, "Approver Received Date",
                                _RECEIPT_VIEWED_COL, _ENTRY_VIEWED_COL, "Minutes of Approval from Receipt View",
                                "Receipts Viewed Date"] if c in df.columns]
    detail = df[detail_cols].copy()
    detail["Receipt_Status"] = status.values
    if _INSUFFICIENT_FLAG in df.columns:
        detail["Insufficient review (RF_APR_InsufficientReview)"] = insufficient.astype(int).values
    styles = [
        {"if": {"filter_query": "{Receipt_Status} = 'No Receipt Viewed'"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"},
        {"if": {"filter_query": "{Receipt_Status} contains 'Viewed'"}, "backgroundColor": "#edf8f1"},
    ]
    return kpis, f1, f2, f3, f4, detail.to_dict("records"), _table_columns(detail), styles


# ── Test catalogue sub-tab ────────────────────────────────────────────────────

_STATUS_LABEL = {"exception": "Exception", "pass": "Pass", "not_testable": "Not testable"}
_STATUS_PRIORITY = {"exception": 0, "pass": 1, "not_testable": 2}


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


def _combined_status(results: list[dict]) -> dict:
    if not results:
        return {}
    best = min(results, key=lambda r: _STATUS_PRIORITY.get(r.get("status"), 3))
    total_exceptions = sum(r.get("exception_units") or 0 for r in results if r.get("status") == "exception")
    return {**best, "exception_units": total_exceptions if best.get("status") == "exception" else best.get("exception_units")}


def _reconciliation_panel(reconciliation: dict | None) -> html.Div | None:
    if not reconciliation:
        return None
    chips = []
    for source, rec in sorted(reconciliation.items()):
        variance = rec.get("variance")
        variance_text = "reconciled" if variance in (0, None) else f"variance {variance}"
        engine_rows = rec.get("engine_rows")
        chips.append(html.Span(
            f"{source}: {_fmt_count(engine_rows)} rows · {variance_text}" if engine_rows is not None else f"{source}: n/a",
            className="chip", style={"color": "#2c7a4b" if variance in (0, None) else "#b85042"},
        ))
    return html.Div([
        html.Div("Population reconciliation (G6)", style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginBottom": 4}),
        html.Div(chips, className="chip-row"),
    ], className="panel", style={"marginBottom": 12})


def _claims_population_panel(payload: dict | None) -> html.Div | None:
    """The run's own claims_prepared/approved/combined metrics (rows +
    amount) -- ports reference_app/app.py's 'Population Reconciliation'
    panel, but every figure here is read from this run's persisted metrics
    (skills/tne_exco/plan.yaml), never recomputed or hardcoded (CLAUDE.md
    §0.2 N6 -- computation.py's `total_records: 152_921` is exactly the
    number this replaces)."""
    rows_chip = []
    for label, rows_metric, amount_metric in [
        ("Prepared", "claims_prepared_rows", "claims_prepared_amount"),
        ("Approved", "claims_approved_rows", "claims_approved_amount"),
        ("Combined", "claims_combined_rows", "claims_combined_amount"),
    ]:
        rows = _metric_value(payload, rows_metric)
        amount = _metric_value(payload, amount_metric)
        if rows is None and amount is None:
            continue
        text = f"{label}: "
        text += f"{_fmt_count(rows)} rows" if rows is not None else "n/a rows"
        text += f" · {_fmt_currency(amount)}" if amount is not None else ""
        rows_chip.append(html.Span(text, className="chip"))
    if not rows_chip:
        return None
    return html.Div([
        html.Div("Claims population (P_EXP role sets)", style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginBottom": 4}),
        html.Div(rows_chip, className="chip-row"),
    ], className="panel", style={"marginBottom": 12})


def _methodology_panel(payload: dict | None) -> html.Details:
    reconciliation = (payload or {}).get("reconciliation")
    return html.Details([
        html.Summary("Methodology & guardrails", className="ghost", style={"fontSize": 13, "fontWeight": 600}),
        html.Div([
            html.Div([
                html.Div("Control layer", style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginBottom": 4}),
                html.Table([
                    html.Thead(html.Tr([html.Th("Layer"), html.Th("What it proves")])),
                    html.Tbody([
                        html.Tr([html.Td("Population reconciliation"), html.Td("Source row count reconciled against an independent count (CLAUDE.md G6)")]),
                        html.Tr([html.Td("Deterministic rule"), html.Td("The test was applied consistently to every record — the same plan.yaml primitive, every run")]),
                        html.Tr([html.Td("Evidence citation"), html.Td("Each metric links to its source table/file version and row keys (source_ref)")]),
                        html.Tr([html.Td("Auditor judgement"), html.Td("Sign-off is required before a finding leaves the system (CLAUDE.md §2.4)")]),
                    ]),
                ], style={"fontSize": 12, "borderCollapse": "collapse", "width": "100%"}),
            ], className="panel", style={"marginBottom": 12}),
            _reconciliation_panel(reconciliation),
            _claims_population_panel(payload),
            html.Div([
                html.Div("Limitations", style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginBottom": 4}),
                html.Ul([
                    html.Li("Source data completeness has not been independently verified beyond the G6 row-count reconciliation."),
                    html.Li("Thresholds carrying provenance 'analyst-set' are pending policy confirmation — flagged wherever they drive a severity."),
                    html.Li("Tests marked 'Not testable' are declared gaps (e.g. no preferred-hotel list, no classification endpoint yet), never a silent zero."),
                    html.Li("Rule-based results indicate exceptions, not confirmed findings — sign-off is a human judgement."),
                ], style={"fontSize": 12.5, "color": "#2c3040", "margin": 0, "paddingLeft": 18}),
            ], className="panel"),
        ], style={"marginTop": 10}),
    ], className="mb-3")


def _catalogue_tab(tests: list[dict], test_results: list[dict], payload: dict | None) -> html.Div:
    n_exception = sum(1 for r in test_results if r.get("status") == "exception")
    n_pass = sum(1 for r in test_results if r.get("status") == "pass")
    n_na = sum(1 for r in test_results if r.get("status") == "not_testable")

    status_kpis = html.Div([
        kpi_card("Tests executed", str(len(test_results))),
        kpi_card("Exceptions", str(n_exception)),
        kpi_card("Pass", str(n_pass)),
        kpi_card("Not testable", str(n_na)),
    ], className="grid-4 mb-3")

    rows = []
    tooltips = []
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
        tooltips.append({"Test Name": {
            "value": f"**Objective:** {test.get('control_objective', '')}\n\n**Population:** {test.get('population', '')}\n\n**Rule:** {test.get('rule', '')}",
            "type": "markdown",
        }})

    return html.Div([
        status_kpis,
        _methodology_panel(payload),
        html.Div([
            html.Div("Test catalogue", className="table-title"),
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
                tooltip_data=tooltips,
                tooltip_duration=None,
            ),
        ], className="panel"),
    ])


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
    payload = bundle["payload"]
    actions = bundle["actions"]

    findings_and_actions = dbc.Tabs([
        dbc.Tab(_findings_tab(bundle), label="Findings & Evidence", tab_id="tne-sub-findings"),
        dbc.Tab(_actions_tab(bundle), label="Management Actions", tab_id="tne-sub-actions"),
    ], id="tne-findings-tabs", active_tab="tne-sub-findings")

    audit_detail = dbc.Tabs([
        dbc.Tab(_p1_layout(bundle), label="Executive analysis", tab_id="tne-sub-overview"),
        dbc.Tab(_p2_layout(bundle), label="Detailed risk", tab_id="tne-sub-risk"),
        dbc.Tab(_p3_layout(bundle), label="Receipt & approver review", tab_id="tne-sub-approval"),
        dbc.Tab(_catalogue_tab(tests, test_results, payload), label="Test catalogue", tab_id="tne-sub-catalogue"),
    ], id="tne-audit-tabs", active_tab="tne-sub-overview")

    return html.Div([
        _build_header(run, payload),
        dcc.Store(id="tne-run-id", data=run_id),
        dcc.Store(id="tne-findings-store", data=findings),
        html.Div([
            dbc.Tabs([
                dbc.Tab(_executive_tab(run, findings, payload, actions, bundle["frames"]), label="Executive Brief", tab_id="tne-tab-executive"),
                dbc.Tab(findings_and_actions, label="Findings & Actions", tab_id="tne-tab-findings"),
                dbc.Tab(audit_detail, label="Audit Detail", tab_id="tne-tab-audit"),
            ], id="tne-main-tabs", active_tab="tne-tab-executive"),
            *_offcanvases(),
        ], className="shell dashboard-shell"),
    ])


# ── Callbacks ─────────────────────────────────────────────────────────────

def register_callbacks(app) -> None:

    @app.callback(
        Output("tne-filtered-findings", "children"),
        Input("tne-severity-filter", "value"),
        Input("tne-category-filter", "value"),
        Input("tne-sort", "value"),
        State("tne-findings-store", "data"),
        State("tne-run-id", "data"),
        prevent_initial_call=True,
    )
    def _filter_findings(severity_filter, category_filter, sort_by, findings, run_id):
        bundle = _load_bundle(run_id)
        tests = (bundle or {}).get("skill", {}).get("tests", [])
        return _render_filtered_findings(findings or [], severity_filter or [], category_filter or [], sort_by, tests)

    @app.callback(
        Output("tne-evidence-offcanvas", "is_open"),
        Output("tne-evidence-body", "children"),
        Output("tne-evidence-offcanvas", "title"),
        Input({"type": "tne-view-evidence", "index": ALL}, "n_clicks"),
        State("tne-findings-store", "data"),
        prevent_initial_call=True,
    )
    def _open_evidence(n_clicks_list, findings):
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

        threshold_rows = []
        for t in finding.get("threshold_refs", []):
            pending = " (pending policy confirmation)" if t.get("pending_policy_confirmation") else ""
            threshold_rows.append(html.Li(
                f"{t.get('id')} = {t.get('value')} {t.get('unit') or ''} — {t.get('provenance_type') or 'unknown'}{pending}"
            ))

        content = html.Div([
            html.Div([
                html.Span(finding.get("severity", "—"), className="chip",
                          style={"color": _SEVERITY_COLOR.get(finding.get("severity"), "#6b7283"),
                                 "borderColor": _SEVERITY_COLOR.get(finding.get("severity"), "#6b7283")}),
                html.Span(finding.get("test_id", ""), className="chip mono", style={"color": "#6b7283"}),
                html.Span(f"rule {finding.get('rule_id', '')}", className="chip mono", style={"color": "#6b7283"}),
            ], style={"display": "flex", "gap": 8, "marginBottom": 14, "flexWrap": "wrap"}),
            html.P(finding.get("observation", ""), style={"fontSize": 13.5, "lineHeight": 1.55}),
            html.Div("Metrics cited — source provenance",
                     style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginTop": 14, "marginBottom": 6}),
            html.Table([
                html.Thead(html.Tr([html.Th("metric"), html.Th("value", style={"textAlign": "right"}), html.Th("source_ref")])),
                html.Tbody(rows),
            ], style={"width": "100%", "fontSize": 12.5, "borderCollapse": "collapse"}) if rows else
            html.P("No cited metrics recorded for this finding.", style={"color": "#6b7283", "fontSize": 12}),
            html.Div("Thresholds consulted", style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginTop": 14, "marginBottom": 6})
            if threshold_rows else None,
            html.Ul(threshold_rows, style={"fontSize": 12.5, "paddingLeft": 18}) if threshold_rows else None,
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
        tests = (bundle or {}).get("skill", {}).get("tests", [])
        meta = _skill_flag_meta((bundle or {}).get("run", {}).get("skill_id"), tests)
        flags = _flags_for_test(meta, test_id)
        source, df = _frame_for_flags(frames, flags)

        if df is None or not flags:
            content = html.Div([
                html.P("No transaction-level drill-down available for this test.", style={"color": "#6b7283", "fontSize": 13}),
                html.P(f"Test {test_id} — no matching flag column in the returned population frames.",
                       style={"color": "#6b7283", "fontSize": 12}),
            ])
            return True, content, f"{test_id}: Exception Records"

        present_flags = [f for f in flags if f in df.columns]
        mask = df[present_flags].apply(pd.to_numeric, errors="coerce").fillna(0).astype(int).sum(axis=1) > 0
        df_exc = df[mask]
        content = html.Div([
            html.Div([
                html.Span(f"{len(df_exc):,} exception records", className="chip"),
                html.Span(test_id, className="chip mono"),
                html.Span(f"source: {source}", className="chip", style={"color": "#6b7283"}),
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
            return None, f"PPTX export is not available for this run ({exc})"
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

    # ── Audit Detail / page 1 ────────────────────────────────────────────────

    @app.callback(
        Output("tne-p1-kpis", "children"),
        Output("tne-p1-monthly", "figure"),
        Output("tne-p1-breach-by-member", "figure"),
        Output("tne-p1-breach-dist", "figure"),
        Output("tne-p1-missing-tier", "figure"),
        Output("tne-p1-precomp", "figure"),
        Output("tne-p1-attendee-table", "data"),
        Output("tne-p1-attendee-table", "columns"),
        Output("tne-p1-attendee-table", "style_data_conditional"),
        Output("tne-p1-evidence", "children"),
        Input("tne-p1-date", "start_date"),
        Input("tne-p1-date", "end_date"),
        Input("tne-p1-member", "value"),
        State("tne-run-id", "data"),
    )
    def _update_p1(start_date, end_date, members, run_id):
        bundle = _load_bundle(run_id)
        if bundle is None:
            raise PreventUpdate
        tests = bundle["skill"].get("tests", [])
        meta = _skill_flag_meta(bundle["run"].get("skill_id"), tests)
        return _p1_update(bundle, start_date, end_date, members, meta)

    # ── Audit Detail / page 2 ────────────────────────────────────────────────

    @app.callback(
        Output("tne-p2-kpis", "children"),
        Output("tne-p2-spend-by-employee", "figure"),
        Output("tne-p2-vendor-table", "data"),
        Output("tne-p2-vendor-table", "columns"),
        Output("tne-p2-vendor-table", "style_data_conditional"),
        Output("tne-p2-breach-expense", "figure"),
        Output("tne-p2-exception-table", "data"),
        Output("tne-p2-exception-table", "columns"),
        Output("tne-p2-claim-pre-table", "data"),
        Output("tne-p2-claim-pre-table", "columns"),
        Output("tne-p2-claim-pre-table", "style_data_conditional"),
        Output("tne-p2-missing-table", "data"),
        Output("tne-p2-missing-table", "columns"),
        Output("tne-p2-missing-table", "style_data_conditional"),
        Input("tne-p2-date", "start_date"),
        Input("tne-p2-date", "end_date"),
        Input("tne-p2-member", "value"),
        Input("tne-p2-expense", "value"),
        State("tne-run-id", "data"),
    )
    def _update_p2(start_date, end_date, members, expense_types, run_id):
        bundle = _load_bundle(run_id)
        if bundle is None:
            raise PreventUpdate
        tests = bundle["skill"].get("tests", [])
        meta = _skill_flag_meta(bundle["run"].get("skill_id"), tests)
        return _p2_update(bundle, start_date, end_date, members, expense_types, meta)

    # ── Audit Detail / page 3 ────────────────────────────────────────────────

    @app.callback(
        Output("tne-p3-kpis", "children"),
        Output("tne-p3-donut", "figure"),
        Output("tne-p3-by-approver", "figure"),
        Output("tne-p3-combo", "figure"),
        Output("tne-p3-risk-profile", "figure"),
        Output("tne-p3-detail", "data"),
        Output("tne-p3-detail", "columns"),
        Output("tne-p3-detail", "style_data_conditional"),
        Input("tne-p3-date", "start_date"),
        Input("tne-p3-date", "end_date"),
        Input("tne-p3-approver", "value"),
        State("tne-run-id", "data"),
    )
    def _update_p3(start_date, end_date, approvers, run_id):
        bundle = _load_bundle(run_id)
        if bundle is None:
            raise PreventUpdate
        return _p3_update(bundle, start_date, end_date, approvers)

    # ── Management actions ───────────────────────────────────────────────────

    @app.callback(
        Output("tne-mgmt-tracker-body", "children"),
        Input("tne-action-overrides", "data"),
        State("tne-run-id", "data"),
    )
    def _render_actions(overrides, run_id):
        bundle = _load_bundle(run_id)
        actions = (bundle or {}).get("actions", [])
        return _render_mgmt_tracker(actions, overrides or {})

    @app.callback(
        Output("tne-action-modal", "is_open"),
        Output("tne-action-modal-title", "children"),
        Output("tne-action-modal-finding", "children"),
        Output("tne-action-owner", "value"),
        Output("tne-action-status", "value"),
        Output("tne-action-target-date", "date"),
        Output("tne-action-response", "value"),
        Output("tne-selected-action", "data"),
        Input({"type": "tne-edit-action-btn", "index": ALL}, "n_clicks"),
        Input("tne-action-cancel", "n_clicks"),
        State("tne-action-overrides", "data"),
        State("tne-run-id", "data"),
        prevent_initial_call=True,
    )
    def _open_action_editor(edit_clicks, cancel_clicks, overrides, run_id):
        if ctx.triggered_id == "tne-action-cancel":
            return (False,) + (dash.no_update,) * 7
        if not ctx.triggered_id or not isinstance(ctx.triggered_id, dict):
            raise PreventUpdate
        if not any(n for n in edit_clicks if n):
            raise PreventUpdate

        action_id = ctx.triggered_id["index"]
        bundle = _load_bundle(run_id)
        action = next((a for a in (bundle or {}).get("actions", []) if a.get("action_id") == action_id), None)
        if not action:
            raise PreventUpdate
        view = _action_row_view(action, (overrides or {}).get(action_id))
        return (
            True, f"Management action · {action.get('evidence_link', '')}", view["title"],
            view["owner"], view["status"], view["target_date"] or None, view["response"],
            {"action_id": action_id},
        )

    @app.callback(
        Output("tne-action-overrides", "data"),
        Output("tne-action-modal", "is_open", allow_duplicate=True),
        Input("tne-action-save", "n_clicks"),
        State("tne-selected-action", "data"),
        State("tne-action-owner", "value"),
        State("tne-action-status", "value"),
        State("tne-action-target-date", "date"),
        State("tne-action-response", "value"),
        State("tne-action-overrides", "data"),
        prevent_initial_call=True,
    )
    def _save_action(_, selected, owner, status, target_date, response, overrides):
        if not selected or not selected.get("action_id"):
            raise PreventUpdate
        action_id = selected["action_id"]
        overrides = dict(overrides or {})
        overrides[action_id] = {
            "owner": (owner or "").strip(), "status": status or "draft",
            "target_date": target_date or "", "response": (response or "").strip(),
        }
        return overrides, False

    # ── Audience mode ─────────────────────────────────────────────────────────

    @app.callback(
        Output("tne-main-tabs", "active_tab"),
        Input("tne-audience-mode", "value"),
        prevent_initial_call=True,
    )
    def _switch_audience_mode(mode):
        if mode == "executive":
            return "tne-tab-executive"
        if mode == "investigator":
            return "tne-tab-audit"
        return "tne-tab-findings"
