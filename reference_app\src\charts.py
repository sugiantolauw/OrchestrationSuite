"""Chart Builders — Plotly interactive visualisations for the audit dashboard.

All charts are built from pre-computed data (DataFrames or payload dicts).
Charts are Plotly figures returned for embedding in Dash.
"""

import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import pandas as pd
from typing import Dict, Any, List, Optional

# Colour palette
COLORS = {
    "primary": "#1a237e",
    "secondary": "#0d47a1",
    "accent": "#ff6f00",
    "danger": "#c62828",
    "warning": "#f57f17",
    "success": "#2e7d32",
    "muted": "#546e7a",
    "light_bg": "#f5f5f5",
}

BREACH_COLORS = {
    "Pre-approvals Not Linked": "#c62828",
    "Travel Without Pre-approval": "#ad1457",
    "Non-Preferred Supplier": "#6a1b9a",
    "Late Travel Bookings": "#283593",
    "Entertainment Over Limit": "#0277bd",
    "Missing Receipts": "#00695c",
    "Missing Attendee List": "#2e7d32",
    "Personal Expense (GenAI)": "#ef6c00",
    "Reimbursement >$5K": "#4e342e",
    "Split Claims": "#37474f",
    "Duplicates": "#880e4f",
    "Approver Review": "#1565c0",
    "Daily Spend": "#4527a0",
}


def create_kpi_banner(payload: Dict[str, Any]) -> go.Figure:
    """Create the Executive KPI Banner.

    Shows: Total spend, breach count, breach amount, missing receipts count.
    """
    tests = payload["tests"]

    total_spend = payload["total_spend_tor"]
    total_claims = payload["claims_combined"]["rows"]

    # Aggregate breach counts
    breach_count = sum([
        tests["T3.1a"].get("count", 0),
        tests["T3.1b"].get("count", 0),
        tests["T3.3a"].get("count", 0),
        tests["T4.4"].get("count", 0),
        tests["T5.1"].get("same_day", 0),
        tests["T5.2"].get("count", 0),
    ])

    missing_receipts = tests["T4.1"].get("count", 0)
    no_review_pct = tests["T6.1a"].get("pct", 0)

    fig = go.Figure()

    # Use indicator traces for KPIs
    fig.add_trace(go.Indicator(
        mode="number",
        value=total_spend,
        number={"prefix": "$", "valueformat": ",.0f"},
        title={"text": "Total ExCo Spend"},
        domain={"row": 0, "column": 0},
    ))

    fig.add_trace(go.Indicator(
        mode="number",
        value=total_claims,
        number={"valueformat": ","},
        title={"text": "Total Claims"},
        domain={"row": 0, "column": 1},
    ))

    fig.add_trace(go.Indicator(
        mode="number",
        value=breach_count,
        number={"valueformat": ","},
        title={"text": "Total Exceptions"},
        domain={"row": 0, "column": 2},
    ))

    fig.add_trace(go.Indicator(
        mode="number",
        value=no_review_pct,
        number={"suffix": "%", "valueformat": ".1f"},
        title={"text": "No Receipt Review"},
        domain={"row": 0, "column": 3},
    ))

    fig.update_layout(
        grid={"rows": 1, "columns": 4, "pattern": "independent"},
        height=200,
        margin=dict(t=40, b=20, l=20, r=20),
        paper_bgcolor=COLORS["light_bg"],
    )

    return fig


def create_monthly_trend(df_combined: pd.DataFrame) -> go.Figure:
    """Monthly trend chart: bars for claim count, line for dollar amount."""
    amount_col = "Expense Amount (reimbursement currency)"
    if amount_col not in df_combined.columns:
        candidates = [c for c in df_combined.columns if "amount" in c.lower() and "reimb" in c.lower()]
        amount_col = candidates[0] if candidates else None

    if amount_col is None:
        return go.Figure()

    df = df_combined.copy()
    df["Transaction Date"] = pd.to_datetime(df["Transaction Date"], errors="coerce")
    df[amount_col] = pd.to_numeric(df[amount_col], errors="coerce")
    df["Month"] = df["Transaction Date"].dt.to_period("M").astype(str)

    monthly = df.groupby("Month").agg(
        claim_count=(amount_col, "count"),
        total_amount=(amount_col, "sum"),
    ).reset_index()

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(
        go.Bar(
            x=monthly["Month"],
            y=monthly["claim_count"],
            name="Claim Count",
            marker_color=COLORS["primary"],
            opacity=0.7,
        ),
        secondary_y=False,
    )

    fig.add_trace(
        go.Scatter(
            x=monthly["Month"],
            y=monthly["total_amount"],
            name="Total Amount ($)",
            line=dict(color=COLORS["accent"], width=3),
            mode="lines+markers",
        ),
        secondary_y=True,
    )

    fig.update_layout(
        title="Monthly Expense Trend (ExCo Combined)",
        xaxis_title="Month",
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.update_yaxes(title_text="Claim Count", secondary_y=False)
    fig.update_yaxes(title_text="Amount ($)", secondary_y=True)

    return fig


def create_breach_by_test(payload: Dict[str, Any]) -> go.Figure:
    """Breach type distribution: horizontal bar chart."""
    tests = payload["tests"]

    data = [
        {"test": "T3.1a: Pre-approvals Not Linked", "count": tests["T3.1a"].get("count", 0)},
        {"test": "T3.1b: Travel No Pre-approval", "count": tests["T3.1b"].get("count", 0)},
        {"test": "T3.3a: Late Bookings", "count": tests["T3.3a"].get("count", 0)},
        {"test": "T4.2: Missing Attendee", "count": tests["T4.2"].get("missing", 0)},
        {"test": "T4.3: Personal Expense", "count": tests["T4.3"].get("flagged", 0)},
        {"test": "T4.4: >$5K Claims", "count": tests["T4.4"].get("count", 0)},
        {"test": "T5.1: Split Claims", "count": tests["T5.1"].get("same_day", 0)},
        {"test": "T5.2: Duplicates", "count": tests["T5.2"].get("count", 0)},
    ]

    df = pd.DataFrame(data).sort_values("count", ascending=True)

    fig = go.Figure(go.Bar(
        x=df["count"],
        y=df["test"],
        orientation="h",
        marker_color=COLORS["danger"],
        text=df["count"],
        textposition="outside",
    ))

    fig.update_layout(
        title="Exception Count by Test",
        xaxis_title="Count",
        height=400,
        margin=dict(l=200),
    )

    return fig


def create_approver_review_chart(
    approver_metrics: List[Dict[str, Any]]
) -> go.Figure:
    """Approver review sufficiency: combo chart.

    Bars for Receipt Viewed %, line for Instant Approval %.
    """
    df = pd.DataFrame(approver_metrics)
    df = df.sort_values("receipt_viewed_pct", ascending=False)

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(
        go.Bar(
            x=df["name"],
            y=df["receipt_viewed_pct"],
            name="Receipt Viewed %",
            marker_color=COLORS["success"],
            opacity=0.8,
        ),
        secondary_y=False,
    )

    fig.add_trace(
        go.Scatter(
            x=df["name"],
            y=df["instant_pct"],
            name="Instant Approval %",
            line=dict(color=COLORS["danger"], width=3),
            mode="lines+markers",
            marker=dict(size=8),
        ),
        secondary_y=True,
    )

    fig.update_layout(
        title="ExCo Approver Review Behaviour",
        xaxis_tickangle=-45,
        height=450,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.update_yaxes(title_text="Receipt Viewed %", range=[0, 100], secondary_y=False)
    fig.update_yaxes(title_text="Instant Approval %", range=[0, 100], secondary_y=True)

    return fig


def create_split_duplicates_table(
    payload: Dict[str, Any]
) -> go.Figure:
    """Split claims & duplicates summary table."""
    tests = payload["tests"]

    data = [
        ["Same-Day Splits", tests["T5.1"]["same_day"], "⚠️" if tests["T5.1"]["same_day"] > 0 else "✅"],
        ["Window Splits (±2 days)", tests["T5.1"]["window"], "⚠️" if tests["T5.1"]["window"] > 0 else "✅"],
        ["Potential Duplicates", tests["T5.2"]["count"], "⚠️" if tests["T5.2"]["count"] > 0 else "✅"],
        ["OOP vs AMEX", tests["T5.2"]["oop_vs_amex"], "🚨" if tests["T5.2"]["oop_vs_amex"] > 0 else "✅"],
    ]

    fig = go.Figure(data=[go.Table(
        header=dict(
            values=["Test", "Count", "Status"],
            fill_color=COLORS["primary"],
            font=dict(color="white", size=13),
            align="left",
        ),
        cells=dict(
            values=list(zip(*data)),
            fill_color=[COLORS["light_bg"]],
            align="left",
            height=30,
        ),
    )])

    fig.update_layout(
        title="Split Claims & Duplicate Analysis",
        height=250,
        margin=dict(t=40, b=10),
    )

    return fig


def create_preapproval_compliance(
    payload: Dict[str, Any]
) -> go.Figure:
    """Pre-approval compliance grouped bar."""
    tests = payload["tests"]

    categories = ["Pre-approvals\nNot Linked", "Travel Without\nPre-approval"]
    counts = [tests["T3.1a"]["count"], tests["T3.1b"]["count"]]

    fig = go.Figure(go.Bar(
        x=categories,
        y=counts,
        marker_color=[COLORS["danger"], COLORS["warning"]],
        text=counts,
        textposition="outside",
    ))

    fig.update_layout(
        title="Pre-Approval Compliance Exceptions",
        yaxis_title="Count",
        height=350,
    )

    return fig


def create_all_charts(
    payload: Dict[str, Any],
    df_combined: Optional[pd.DataFrame] = None,
) -> Dict[str, go.Figure]:
    """Build all charts and return as a dictionary.

    Args:
        payload: The results_payload
        df_combined: The combined population DataFrame (for trend chart)

    Returns:
        Dict mapping chart name to Plotly figure
    """
    charts = {
        "kpi_banner": create_kpi_banner(payload),
        "breach_by_test": create_breach_by_test(payload),
        "approver_review": create_approver_review_chart(payload["approver_metrics"]),
        "split_duplicates": create_split_duplicates_table(payload),
        "preapproval_compliance": create_preapproval_compliance(payload),
    }

    if df_combined is not None:
        charts["monthly_trend"] = create_monthly_trend(df_combined)

    return charts
