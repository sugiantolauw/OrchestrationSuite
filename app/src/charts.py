"""Chart builders for /workspace/tne.

Pure functions over already-fetched run data (a DataFrame from
get_run_frames, a findings list, a reconciliation dict...) -- nothing here
calls a service, reads a run_id, or does I/O (CLAUDE.md §2.1: a callback may
filter/aggregate a completed run's population for a chart, but must not
produce audit evidence or take more than a couple of seconds). Every
function degrades to a small labelled "no data" figure rather than raising,
so a chart panel never crashes the page when a run's frame is missing an
expected column (CLAUDE.md NN14: an absent value is reported, never guessed
or defaulted into a number).

Chart TYPES, layout and the colour palette are ported from
reference_app/app.py's actual inline figure code (`_build_insights_figures`,
`update_page1`, `update_page2`, `update_page3` -- what /workspace/tne
literally renders), not from reference_app/src/charts.py: that module is
never imported by reference_app/app.py and builds figures against a
different, unused payload shape (`payload["tests"]["T3.1a"]["count"]`,
`payload["approver_metrics"]`, ...) left over from an earlier iteration, so
it is not a live behavioural reference (CLAUDE.md §0.2's "read this as
context, not as an audit of careless work" applies equally to orphaned
prototype code, not only to app.py's wired paths).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

MARGIN = dict(l=16, r=16, t=40, b=16)
PALETTE = ["#1e2761", "#1c7293", "#b85042", "#e0952a", "#2c7a4b", "#6b7283", "#7b5ea7", "#c76b32"]
SEVERITY_COLOR = {"High": "#b85042", "Medium": "#e0952a", "Low": "#2c7a4b"}
RECEIPT_STATUS_COLOR = {
    "Viewed Report Receipts": "#2c7a4b",
    "Viewed Entry Receipts": "#1c7293",
    "No Receipt Viewed": "#b85042",
}

RECEIPT_TIER_BINS = [-1, 75, 200, 500, 1000, 5000, np.inf]
RECEIPT_TIER_LABELS = ["$0-$75", "$75-$200", "$200-$500", "$500-$1K", "$1K-$5K", ">$5K"]


def empty_figure(title: str, height: int = 260) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(title=title, height=height, margin=MARGIN, paper_bgcolor="white", plot_bgcolor="white")
    return fig


def _layout(fig: go.Figure, title: str, height: int, **extra) -> go.Figure:
    fig.update_layout(title=title, margin=MARGIN, height=height, paper_bgcolor="white", plot_bgcolor="white", **extra)
    return fig


# ── Volume / spend ───────────────────────────────────────────────────────────

def monthly_volume_chart(
    df: pd.DataFrame | None,
    date_col: str = "Transaction Date",
    amount_col: str = "Expense Amount (reimbursement currency)",
    title: str = "Monthly T&E volume",
    height: int = 260,
) -> go.Figure:
    if df is None or df.empty or date_col not in df.columns:
        return empty_figure(f"{title} — no data", height)
    monthly = df.dropna(subset=[date_col]).copy()
    monthly[date_col] = pd.to_datetime(monthly[date_col], errors="coerce")
    monthly = monthly.dropna(subset=[date_col])
    if monthly.empty:
        return empty_figure(f"{title} — no data", height)
    monthly["Month"] = monthly[date_col].dt.to_period("M").dt.to_timestamp()
    agg = {"Claims": (date_col, "count")}
    if amount_col in monthly.columns:
        agg["Amount"] = (amount_col, "sum")
    m = monthly.groupby("Month", as_index=False).agg(**agg)
    fig = go.Figure()
    fig.add_bar(x=m["Month"], y=m["Claims"], name="Claims", marker_color=PALETTE[0])
    if "Amount" in m.columns:
        fig.add_trace(go.Scatter(x=m["Month"], y=m["Amount"], name="Amount", mode="lines+markers",
                                  yaxis="y2", line=dict(color=PALETTE[1], width=2)))
    return _layout(fig, title, height, yaxis2=dict(overlaying="y", side="right", showgrid=False),
                    legend=dict(orientation="h"))


def top_n_bar(
    df: pd.DataFrame | None, group_col: str, amount_col: str, title: str,
    n: int = 10, height: int = 260, color: str = PALETTE[1], x_prefix: str = "$",
) -> go.Figure:
    if df is None or df.empty or group_col not in df.columns or amount_col not in df.columns:
        return empty_figure(f"{title} — no data", height)
    agg = df.groupby(group_col, as_index=False)[amount_col].sum().nlargest(n, amount_col).sort_values(amount_col)
    if agg.empty:
        return empty_figure(f"{title} — no data", height)
    fig = go.Figure()
    fig.add_bar(x=agg[amount_col], y=agg[group_col], orientation="h", marker_color=color)
    return _layout(fig, title, height, xaxis_tickprefix=x_prefix, xaxis_tickformat=",.0f")


# ── Findings ──────────────────────────────────────────────────────────────────

def findings_by_severity_donut(findings: list[dict] | None, title: str = "Findings by risk level", height: int = 260) -> go.Figure:
    if not findings:
        return empty_figure(f"{title} — none", height)
    counts = pd.Series([f.get("severity") for f in findings]).value_counts()
    order = {"High": 0, "Medium": 1, "Low": 2}
    labels = sorted(counts.index, key=lambda s: order.get(s, 3))
    values = [int(counts[l]) for l in labels]
    fig = go.Figure(go.Pie(
        labels=labels, values=values, hole=0.55,
        marker_colors=[SEVERITY_COLOR.get(l, "#6b7283") for l in labels],
        textinfo="label+value", textfont_size=12,
    ))
    return _layout(fig, title, height, showlegend=False)


# ── Exceptions by flag ───────────────────────────────────────────────────────

def exceptions_by_group_chart(
    df: pd.DataFrame | None, flag_labels: dict[str, str], group_col: str, title: str, height: int = 260,
) -> go.Figure:
    """flag_labels: {flag_column: display_label}. Explodes rows flagged by
    more than one test so each contributes to its own label's count -- the
    same shape reference_app/app.py's 'Breach_Type_List' used, generalised to
    whatever flags this run actually produced on this frame (never a fixed
    BREACH_FLAG_GROUPS dict of prototype-specific column names)."""
    flags_present = [c for c in flag_labels if df is not None and c in df.columns]
    if df is None or df.empty or not flags_present or group_col not in df.columns:
        return empty_figure(f"{title} — no data", height)
    rows = []
    for col in flags_present:
        hit = df[pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int) == 1]
        if len(hit):
            rows.append(pd.DataFrame({group_col: hit[group_col], "Category": flag_labels[col]}))
    if not rows:
        return empty_figure(f"{title} — no exceptions", height)
    ex = pd.concat(rows, ignore_index=True)
    counts = ex.groupby([group_col, "Category"], as_index=False).size().rename(columns={"size": "Count"})
    totals = counts.groupby(group_col)["Count"].sum().sort_values(ascending=True).index.tolist()
    fig = px.bar(counts, x="Count", y=group_col, color="Category", orientation="h",
                 category_orders={group_col: totals}, title=title, color_discrete_sequence=PALETTE)
    return _layout(fig, title, height, legend=dict(orientation="h", y=-0.3, font_size=11))


def exceptions_by_flag_distribution(
    df: pd.DataFrame | None, flag_labels: dict[str, str], title: str = "Breach type distribution", height: int = 260,
) -> go.Figure:
    flags_present = [c for c in flag_labels if df is not None and c in df.columns]
    if df is None or df.empty or not flags_present:
        return empty_figure(f"{title} — no data", height)
    counts = {flag_labels[c]: int(pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int).sum()) for c in flags_present}
    s = pd.Series(counts).sort_values(ascending=True)
    s = s[s > 0]
    if s.empty:
        return empty_figure(f"{title} — no exceptions", height)
    fig = px.bar(x=s.values, y=s.index, orientation="h", title=title, color=s.values, color_continuous_scale="Blues")
    return _layout(fig, title, height, coloraxis_showscale=False)


# ── Receipt tiers / compliance ───────────────────────────────────────────────

def receipt_tier(amount_series: pd.Series) -> pd.Series:
    return pd.cut(pd.to_numeric(amount_series, errors="coerce"), bins=RECEIPT_TIER_BINS, labels=RECEIPT_TIER_LABELS)


def missing_by_tier_chart(
    df: pd.DataFrame | None, amount_col: str, missing_flag_col: str,
    title: str = "Missing receipts by claim-size tier", height: int = 260,
) -> go.Figure:
    if df is None or df.empty or missing_flag_col not in df.columns or amount_col not in df.columns:
        return empty_figure(f"{title} — no data", height)
    miss = df[pd.to_numeric(df[missing_flag_col], errors="coerce").fillna(0).astype(int) == 1].copy()
    if miss.empty:
        return empty_figure(f"{title} — none", height)
    miss["Tier"] = receipt_tier(miss[amount_col])
    mt = miss.groupby("Tier", as_index=False, observed=True).agg(Claims=(amount_col, "count"), Amount=(amount_col, "sum"))
    fig = go.Figure()
    fig.add_bar(x=mt["Tier"].astype(str), y=mt["Claims"], name="Claims", marker_color=PALETTE[0])
    fig.add_trace(go.Scatter(x=mt["Tier"].astype(str), y=mt["Amount"], name="Amount", mode="lines+markers",
                              yaxis="y2", line=dict(color=PALETTE[1], width=2)))
    return _layout(fig, title, height, yaxis2=dict(overlaying="y", side="right"), legend=dict(orientation="h"))


def grouped_count_chart(
    left_df: pd.DataFrame | None, right_df: pd.DataFrame | None, group_col: str,
    left_name: str, right_name: str, title: str, height: int = 260,
) -> go.Figure:
    """Two populations grouped by the same key column, side by side -- ports
    reference_app/app.py's 'Travel Pre-Request Compliance' (claims filed vs
    pre-approvals on file, per employee)."""
    if left_df is None or left_df.empty or group_col not in left_df.columns:
        return empty_figure(f"{title} — no data", height)
    left_ct = left_df.groupby(group_col, as_index=False).size().rename(columns={"size": left_name})
    if right_df is not None and group_col in right_df.columns:
        right_ct = right_df.groupby(group_col, as_index=False).size().rename(columns={"size": right_name})
    else:
        right_ct = pd.DataFrame({group_col: [], right_name: []})
    gp = left_ct.merge(right_ct, on=group_col, how="outer").fillna(0)
    if gp.empty:
        return empty_figure(f"{title} — no data", height)
    fig = go.Figure()
    fig.add_bar(x=gp[group_col], y=gp[left_name], name=left_name, marker_color=PALETTE[0])
    fig.add_bar(x=gp[group_col], y=gp[right_name], name=right_name, marker_color=PALETTE[1])
    return _layout(fig, title, height, barmode="group", xaxis_tickangle=-45)


def count_by_group_chart(
    df: pd.DataFrame | None, mask: pd.Series | None, group_col: str, title: str,
    height: int = 260, color_scale: str | None = "Teal",
) -> go.Figure:
    """Row count per group value, restricted to `mask` -- ports
    reference_app/app.py's 'Breach Count by Expense Type' (p2-breach-expense)."""
    if df is None or mask is None or group_col not in df.columns:
        return empty_figure(f"{title} — no data", height)
    sub = df[mask]
    if sub.empty:
        return empty_figure(f"{title} — no exceptions", height)
    counts = sub.groupby(group_col, as_index=False).size().rename(columns={"size": "Count"}).sort_values("Count")
    if color_scale:
        fig = px.bar(counts, x="Count", y=group_col, orientation="h", title=title,
                      color="Count", color_continuous_scale=color_scale)
        return _layout(fig, title, height, coloraxis_showscale=False)
    fig = go.Figure(go.Bar(x=counts["Count"], y=counts[group_col], orientation="h", marker_color=PALETTE[1]))
    return _layout(fig, title, height)


def exploded_exceptions_table(df: pd.DataFrame | None, flag_labels: dict[str, str], display_cols: list[str]) -> pd.DataFrame:
    """One row per (record, breach category) pair -- ports
    reference_app/app.py's exploded 'Breach Type(s)' detail table."""
    flags_present = [c for c in flag_labels if df is not None and c in df.columns]
    if df is None or df.empty or not flags_present:
        return pd.DataFrame()
    cols = [c for c in display_cols if c in df.columns]
    rows = []
    for col in flags_present:
        hit = df[pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int) == 1]
        if len(hit):
            part = hit[cols].copy()
            part["Breach Type(s)"] = flag_labels[col]
            rows.append(part)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


# ── Outliers ──────────────────────────────────────────────────────────────────

def outliers_table(
    df: pd.DataFrame | None, employee_col: str = "Employee",
    amount_col: str = "Expense Amount (reimbursement currency)",
    date_col: str = "Transaction Date", type_col: str = "Expense Type", vendor_col: str = "Vendor",
    top_n: int = 20,
) -> pd.DataFrame:
    """Z-score spend outliers per employee (reference_app/app.py's
    compute_outliers, unchanged logic). A display heuristic surfaced as-is
    for the auditor's attention -- not a Skill test, so it carries no
    RF_* flag and is never cited by a finding."""
    required = {employee_col, amount_col}
    if df is None or not required.issubset(df.columns) or len(df) < 10:
        return pd.DataFrame()
    stats = df.groupby(employee_col)[amount_col].agg(["mean", "std"]).reset_index()
    stats.columns = [employee_col, "Avg Claim", "Std Dev"]
    stats = stats[stats["Std Dev"] > 0]
    cols = [c for c in [employee_col, date_col, type_col, vendor_col, amount_col] if c in df.columns]
    out = df[cols].copy().merge(stats, on=employee_col, how="left")
    out = out.dropna(subset=["Std Dev"])
    if out.empty:
        return pd.DataFrame()
    out["Z-Score"] = ((out[amount_col] - out["Avg Claim"]) / out["Std Dev"]).round(2)
    out = out[out["Z-Score"] > 2.0].sort_values("Z-Score", ascending=False).head(top_n)
    return out.drop(columns=["Avg Claim", "Std Dev"])


# ── Approver review (approval_aging) ─────────────────────────────────────────

def approver_receipt_status(df: pd.DataFrame, viewed_report_col: str, viewed_entry_col: str) -> tuple[pd.Series, pd.Series]:
    """Derives the same Receipt_Status / Receipt_Viewed shape
    reference_app/app.py's `_standardise_approval` computed, directly from
    the real contract columns (Report Receipt Viewed / All Entry Receipts
    Viewed) -- never a fuzzy `_find_col` match, and never a default for a
    missing column (CLAUDE.md NN14: the caller checks the columns exist
    before calling this)."""
    rep = df[viewed_report_col].astype(str).str.upper().str.strip()
    entry = df[viewed_entry_col].astype(str).str.upper().str.strip()
    status = np.where(rep == "Y", "Viewed Report Receipts",
                       np.where(entry == "Y", "Viewed Entry Receipts", "No Receipt Viewed"))
    viewed = ((rep == "Y") | (entry == "Y")).astype(int)
    return pd.Series(status, index=df.index, name="Receipt_Status"), pd.Series(viewed, index=df.index, name="Receipt_Viewed")


def approver_status_donut(status_series: pd.Series | None, title: str = "How approvers review receipts", height: int = 260) -> go.Figure:
    if status_series is None or status_series.empty:
        return empty_figure(f"{title} — no data", height)
    counts = status_series.value_counts()
    fig = go.Figure(go.Pie(
        labels=counts.index, values=counts.values, hole=0.55,
        marker_colors=[RECEIPT_STATUS_COLOR.get(s, "#6b7283") for s in counts.index],
        textinfo="label+value", textfont_size=11,
    ))
    return _layout(fig, title, height)


def approver_status_by_approver(
    approver_series: pd.Series | None, status_series: pd.Series | None,
    title: str = "Receipt review by approver", height: int = 260,
) -> go.Figure:
    if approver_series is None or approver_series.empty:
        return empty_figure(f"{title} — no data", height)
    d = pd.DataFrame({"Approver": approver_series.values, "Status": status_series.values})
    by_app = d.groupby(["Approver", "Status"], as_index=False).size().rename(columns={"size": "Count"})
    totals = by_app.groupby("Approver")["Count"].sum().sort_values(ascending=False).index.tolist()
    fig = px.bar(by_app, x="Approver", y="Count", color="Status", barmode="stack",
                 category_orders={"Approver": totals}, title=title, color_discrete_map=RECEIPT_STATUS_COLOR)
    return _layout(fig, title, height, xaxis_tickangle=-35)


def approver_review_combo(
    approver_series: pd.Series | None, viewed_series: pd.Series | None, insufficient_series: pd.Series | None,
    title: str = "Receipt viewed % vs insufficient-review %", height: int = 260,
) -> go.Figure:
    """Combo chart shaped like reference_app/app.py's 'Receipt Viewed % +
    Instant Approval %' -- but the second series is the run's own
    RF_APR_InsufficientReview test outcome rather than a UI-side re-derived
    'instant approval' proxy against a re-typed minute threshold (the
    prototype's own literal `< 1 minute`, duplicating a number the T6.1a
    primitive already applies with the Skill's real instant_approval_minutes
    threshold -- CLAUDE.md §8 G8 requires one copy of a threshold, not two)."""
    if approver_series is None or approver_series.empty:
        return empty_figure(f"{title} — no data", height)
    d = pd.DataFrame({
        "Approver": approver_series.values,
        "Viewed": viewed_series.values if viewed_series is not None else 0,
        "Insufficient": insufficient_series.values if insufficient_series is not None else 0,
    })
    combo = d.groupby("Approver", as_index=False).agg(
        Receipt_Viewed_Pct=("Viewed", lambda s: round(float(pd.to_numeric(s, errors="coerce").fillna(0).mean() * 100), 1)),
        Insufficient_Pct=("Insufficient", lambda s: round(float(pd.to_numeric(s, errors="coerce").fillna(0).mean() * 100), 1)),
    ).sort_values("Receipt_Viewed_Pct", ascending=False)
    fig = go.Figure()
    fig.add_bar(x=combo["Approver"], y=combo["Receipt_Viewed_Pct"], name="Receipt viewed %", marker_color="#1c7293")
    fig.add_trace(go.Scatter(x=combo["Approver"], y=combo["Insufficient_Pct"], name="Insufficient review %",
                              mode="lines+markers", yaxis="y2", line=dict(color="#b85042", width=3)))
    return _layout(fig, title, height, yaxis=dict(range=[0, 100]),
                    yaxis2=dict(range=[0, 100], overlaying="y", side="right"), xaxis_tickangle=-35)


def approver_risk_profile(approver_series: pd.Series | None, viewed_series: pd.Series | None, title: str = "Approver risk profile", height: int = 260) -> go.Figure:
    if approver_series is None or approver_series.empty:
        return empty_figure(f"{title} — no data", height)
    d = pd.DataFrame({"Approver": approver_series.values, "Viewed": viewed_series.values if viewed_series is not None else 0})
    rp = d.groupby("Approver", as_index=False).agg(Viewed=("Viewed", "sum"), Total=("Viewed", "count"))
    rp["Viewed %"] = np.where(rp["Total"] > 0, rp["Viewed"] / rp["Total"] * 100, 0)
    rp["No View %"] = 100 - rp["Viewed %"]
    rp = rp.sort_values("No View %", ascending=False)
    fig = go.Figure()
    fig.add_bar(y=rp["Approver"], x=rp["Viewed %"], name="Viewed", orientation="h", marker_color="#2c7a4b")
    fig.add_bar(y=rp["Approver"], x=rp["No View %"], name="No view", orientation="h", marker_color="#b85042")
    return _layout(fig, title, height, barmode="stack", xaxis=dict(range=[0, 100]))
