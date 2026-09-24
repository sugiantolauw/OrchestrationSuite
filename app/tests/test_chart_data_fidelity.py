"""Chart data fidelity: every chart /workspace/tne renders must show the
exact numbers its own run's persisted data implies -- never a number the
chart-building code merely happens to produce. This is a testing task only
(see this task's brief); it changes no production code.

For every chart this file computes its own expected series directly from
the run's persisted frames/payload using plain pandas (never by calling
src/charts.py or the _pX_update helpers under test -- see each test's own
comment for exactly what it recomputes), then asserts the ACTUAL rendered
figure's trace data (x/y/labels/values) equals that expectation exactly:
same categories, same order where the chart's own code makes an ordering
choice that carries meaning (an explicit .sort_values()/nlargest()), and a
plain set/dict comparison where two categories tie and the code's own
ordering is not guaranteed (pandas value_counts()/groupby() ties are not a
documented stable order).

Uses app/tests/fake_service.py's fixed, hand-inspectable frames (the same
fixture test_workspace_tne.py already exercises), reached through the real
adapters.start_audit_run()/sign_off() + workspace_tne._load_bundle() path,
never a second, ad hoc population.
"""

from __future__ import annotations

import pandas as pd
import pytest
from dash import dcc, html

from src import workspace_tne
from src.platform import adapters

_AMT = workspace_tne._AMOUNT_COL
_DATE = workspace_tne._DATE_COL


def _completed_run() -> str:
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess spend.",
        run_owner="auditor@example.com",
    )
    adapters.sign_off(run_id, "auditor@example.com")
    return run_id


@pytest.fixture()
def bundle():
    run_id = _completed_run()
    return workspace_tne._load_bundle(run_id)


@pytest.fixture()
def meta(bundle):
    tests = bundle["skill"].get("tests", [])
    return workspace_tne._skill_flag_meta(bundle["run"].get("skill_id"), tests)


# ── generic figure-reading helpers (no charts.py logic here, only trace
# extraction) ─────────────────────────────────────────────────────────────


def _pie_dict(fig) -> dict:
    trace = fig.data[0]
    return dict(zip(trace.labels, trace.values))


def _months(x) -> list:
    return list(pd.to_datetime(list(x)))


def _multi_trace_h_triples(fig) -> set[tuple]:
    """Every (category, series-name, value) triple across all traces of a
    colour-split HORIZONTAL bar (px.bar(..., orientation="h", color=...)):
    x=value, y=category, trace.name=series."""
    triples = set()
    for trace in fig.data:
        for cat, val in zip(trace.y, trace.x):
            triples.add((cat, trace.name, round(float(val), 6)))
    return triples


def _multi_trace_v_triples(fig) -> set[tuple]:
    """Same as above for a colour-split VERTICAL bar: x=category, y=value."""
    triples = set()
    for trace in fig.data:
        for cat, val in zip(trace.x, trace.y):
            triples.add((cat, trace.name, round(float(val), 6)))
    return triples


def _collect_titled_graphs(component) -> dict[str, object]:
    """Walks a Dash component tree and returns {nearest-preceding-H3-text:
    figure} for every dcc.Graph found -- used for the Executive Brief and
    Analytics tabs, where figures are built inline (not returned as
    separate callback outputs the way _pX_update's are)."""
    out: dict[str, object] = {}

    def walk(node, current_title):
        children = getattr(node, "children", None)
        if children is None:
            return
        seq = children if isinstance(children, (list, tuple)) else [children]
        for child in seq:
            if isinstance(child, html.H3):
                txt = child.children
                if isinstance(txt, str):
                    current_title = txt
            if isinstance(child, dcc.Graph):
                out[current_title] = child.figure
            walk(child, current_title)

    walk(component, None)
    return out


# ── independent expected-value computation (plain pandas, no charts.py) ────


def _expected_monthly(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d[_DATE] = pd.to_datetime(d[_DATE])
    d["Month"] = d[_DATE].dt.to_period("M").dt.to_timestamp()
    g = d.groupby("Month").agg(Claims=(_DATE, "count"), Amount=(_AMT, "sum")).reset_index()
    return g.sort_values("Month")


def _expected_top_n(df: pd.DataFrame, group_col: str) -> pd.Series:
    return df.groupby(group_col)[_AMT].sum().sort_values()


def _expected_missing_receipt_tiers(df: pd.DataFrame) -> pd.DataFrame:
    miss = df[df["RF_CS_MissingReceipt"] == 1].copy()
    bins = [-1, 75, 200, 500, 1000, 5000, float("inf")]
    labels = ["$0-$75", "$75-$200", "$200-$500", "$500-$1K", "$1K-$5K", ">$5K"]
    miss["Tier"] = pd.cut(miss[_AMT], bins=bins, labels=labels)
    g = miss.groupby("Tier", observed=True).agg(Claims=(_AMT, "count"), Amount=(_AMT, "sum"))
    return g.reset_index().sort_values("Tier")


# ── Executive Brief tab ──────────────────────────────────────────────────


def test_executive_brief_risk_distribution_donut_matches_findings_severities(bundle):
    findings = bundle["payload"]["findings"]
    expected = {}
    for f in findings:
        expected[f["severity"]] = expected.get(f["severity"], 0) + 1

    tab = workspace_tne._executive_tab(
        bundle["run"], findings, bundle["payload"], bundle["actions"], bundle["frames"]
    )
    graphs = _collect_titled_graphs(tab)
    assert _pie_dict(graphs["Risk distribution"]) == expected


def test_executive_brief_volume_trend_matches_full_unfiltered_population(bundle):
    expense_df = bundle["frames"]["expense_report"]
    expected = _expected_monthly(expense_df)

    tab = workspace_tne._executive_tab(
        bundle["run"], bundle["payload"]["findings"], bundle["payload"], bundle["actions"], bundle["frames"]
    )
    fig = _collect_titled_graphs(tab)["T&E volume trend"]
    claims_trace = fig.data[0]
    amount_trace = fig.data[1]
    assert _months(claims_trace.x) == list(expected["Month"])
    assert list(claims_trace.y) == list(expected["Claims"])
    assert list(amount_trace.y) == list(expected["Amount"])


# ── Findings & Actions / Analytics grid ──────────────────────────────────


def test_analytics_findings_by_risk_level_matches_executive_brief_donut(bundle):
    """Consistency across views (task requirement 2, bullet 4): the same
    findings feed both the Executive Brief's 'Risk distribution' donut and
    the Analytics grid's 'Findings by risk level' donut -- they must agree."""
    findings = bundle["payload"]["findings"]
    tests = bundle["skill"].get("tests", [])
    meta = workspace_tne._skill_flag_meta(bundle["run"].get("skill_id"), tests)
    analytics = workspace_tne._findings_analytics(bundle["frames"], findings, meta)
    exec_tab = workspace_tne._executive_tab(
        bundle["run"], findings, bundle["payload"], bundle["actions"], bundle["frames"]
    )
    analytics_graphs = _collect_titled_graphs(analytics)
    exec_graphs = _collect_titled_graphs(exec_tab)
    assert _pie_dict(analytics_graphs["Findings by risk level"]) == _pie_dict(exec_graphs["Risk distribution"])


def test_analytics_top_10_expense_types_matches_independent_groupby(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    expected = _expected_top_n(expense_df, "Expense Type")

    analytics = workspace_tne._findings_analytics(bundle["frames"], bundle["payload"]["findings"], meta)
    fig = _collect_titled_graphs(analytics)["Top 10 expense types by spend"]
    assert list(fig.data[0].x) == list(expected.values)
    assert list(fig.data[0].y) == list(expected.index)


def test_analytics_top_10_spenders_matches_independent_groupby(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    expected = _expected_top_n(expense_df, "Employee")

    analytics = workspace_tne._findings_analytics(bundle["frames"], bundle["payload"]["findings"], meta)
    fig = _collect_titled_graphs(analytics)["Top 10 spenders"]
    assert list(fig.data[0].x) == list(expected.values)
    assert list(fig.data[0].y) == list(expected.index)


def test_analytics_policy_exceptions_by_exco_member_matches_flagged_rows(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    flagged = expense_df[expense_df["RF_CS_MissingReceipt"] == 1]
    expected = flagged.groupby("Employee").size().to_dict()

    analytics = workspace_tne._findings_analytics(bundle["frames"], bundle["payload"]["findings"], meta)
    fig = _collect_titled_graphs(analytics)["Policy exceptions by ExCo member"]
    triples = _multi_trace_h_triples(fig)
    actual = {cat: val for cat, _name, val in triples}
    assert actual == expected
    for _cat, name, _val in triples:
        assert name == "T4.1: Missing receipts"


def test_analytics_approver_review_quality_matches_receipt_status_counts(bundle, meta):
    approval_df = bundle["frames"]["approval_aging"]
    rep = approval_df["Report Receipt Viewed"].astype(str).str.upper().str.strip()
    entry = approval_df["All Entry Receipts Viewed"].astype(str).str.upper().str.strip()
    status = [
        "Viewed Report Receipts" if r == "Y" else ("Viewed Entry Receipts" if e == "Y" else "No Receipt Viewed")
        for r, e in zip(rep, entry)
    ]
    expected = pd.Series(status).value_counts().to_dict()

    analytics = workspace_tne._findings_analytics(bundle["frames"], bundle["payload"]["findings"], meta)
    fig = _collect_titled_graphs(analytics)["Approver review quality"]
    assert _pie_dict(fig) == expected


# ── Audit Detail / page 1 (Executive analysis) -- default view ──────────


def test_p1_unfiltered_kpis_and_charts_match_full_population(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    expected_amount = expense_df[_AMT].sum()
    flag_cols = [c for c in expense_df.columns if c.startswith("RF_")]
    breach_mask = (expense_df[flag_cols].apply(pd.to_numeric, errors="coerce").fillna(0).astype(int).sum(axis=1) > 0)
    expected_breach_count = int(breach_mask.sum())
    expected_breach_amount = expense_df.loc[breach_mask, _AMT].sum()
    expected_missing_count = int((expense_df["RF_CS_MissingReceipt"] == 1).sum())

    result = workspace_tne._p1_update(bundle, None, None, None, meta)
    kpis, f1, f2, f3, f4, f5 = result[0], result[1], result[2], result[3], result[4], result[5]

    kpi_texts = [str(k) for k in kpis]
    assert f"${expected_amount:,.0f}" in kpi_texts[0]
    assert f"{expected_breach_count:,}" in kpi_texts[1]
    assert f"${expected_breach_amount:,.0f}" in kpi_texts[2]
    assert f"{expected_missing_count:,}" in kpi_texts[3]

    # f1: monthly trend, same independent computation as the Executive Brief
    # chart above (unfiltered == same population).
    expected_monthly = _expected_monthly(expense_df)
    assert _months(f1.data[0].x) == list(expected_monthly["Month"])
    assert list(f1.data[0].y) == list(expected_monthly["Claims"])
    assert list(f1.data[1].y) == list(expected_monthly["Amount"])

    # f2: breach count by ExCo member -- same independent flagged-row
    # groupby as the Analytics 'Policy exceptions by ExCo member' chart.
    flagged = expense_df[expense_df["RF_CS_MissingReceipt"] == 1]
    expected_by_member = flagged.groupby("Employee").size().to_dict()
    actual_by_member = {cat: val for cat, _name, val in _multi_trace_h_triples(f2)}
    assert actual_by_member == expected_by_member

    # f4: missing receipts by claim-size tier.
    expected_tiers = _expected_missing_receipt_tiers(expense_df)
    assert list(f4.data[0].x) == [str(t) for t in expected_tiers["Tier"]]
    assert list(f4.data[0].y) == list(expected_tiers["Claims"])
    assert list(f4.data[1].y) == list(expected_tiers["Amount"])

    # f5: claims vs pre-approvals, unfiltered -- travel_requests_no_expense
    # is never itself filtered by _p1_update (see the xfail test below for
    # what happens once a member filter is applied), so with NO filter
    # active this must equal the plain, unfiltered merge of both frames.
    pre_df = bundle["frames"]["travel_requests_no_expense"]
    left = expense_df.groupby("Employee").size()
    right = pre_df.groupby("Employee").size()
    merged = pd.DataFrame({"Claims": left, "Pre-approvals": right}).fillna(0)
    claims_triples = {(cat, round(float(val), 6)) for cat, val in zip(f5.data[0].x, f5.data[0].y)}
    pre_triples = {(cat, round(float(val), 6)) for cat, val in zip(f5.data[1].x, f5.data[1].y)}
    assert claims_triples == {(emp, row["Claims"]) for emp, row in merged.iterrows()}
    assert pre_triples == {(emp, row["Pre-approvals"]) for emp, row in merged.iterrows()}


def test_p1_missing_receipts_kpi_agrees_with_missing_receipt_tier_chart_total(bundle, meta):
    """Consistency across views (task requirement 2, bullet 4): the
    'Missing receipts' KPI tile and the sum of the 'Missing receipts by
    claim-size tier' chart's own Claims series are the same underlying
    quantity (rows where RF_CS_MissingReceipt == 1) and must agree."""
    result = workspace_tne._p1_update(bundle, None, None, None, meta)
    kpis, f4 = result[0], result[4]
    expense_df = bundle["frames"]["expense_report"]
    expected_missing_count = int((expense_df["RF_CS_MissingReceipt"] == 1).sum())
    assert sum(f4.data[0].y) == expected_missing_count
    assert f"{expected_missing_count:,}" in str(kpis[3])


def test_p1_total_spend_kpi_agrees_with_monthly_chart_amount_total(bundle, meta):
    result = workspace_tne._p1_update(bundle, None, None, None, meta)
    kpis, f1 = result[0], result[1]
    expense_df = bundle["frames"]["expense_report"]
    expected_total = expense_df[_AMT].sum()
    assert abs(sum(f1.data[1].y) - expected_total) < 1e-6
    assert f"${expected_total:,.0f}" in str(kpis[0])


# ── Audit Detail / page 1 -- filtered view ───────────────────────────────
#
# The member (ExCo) filter alone, deliberately with NO date-range value --
# see test_date_range_filter_crashes_on_every_audit_detail_page below for
# why a real start_date/end_date string cannot be exercised here.


def test_p1_filtered_by_member_matches_independently_filtered_population(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    filtered = expense_df[expense_df["Employee"] == "Alice Wu"]
    assert len(filtered) == 3  # sanity: the fixture rows this test depends on

    result = workspace_tne._p1_update(bundle, None, None, ["Alice Wu"], meta)
    kpis, f1, f2, f4 = result[0], result[1], result[2], result[4]

    expected_amount = filtered[_AMT].sum()
    expected_missing = int((filtered["RF_CS_MissingReceipt"] == 1).sum())
    assert f"${expected_amount:,.0f}" in str(kpis[0])
    assert f"{expected_missing:,}" in str(kpis[3])

    expected_monthly = _expected_monthly(filtered)
    assert _months(f1.data[0].x) == list(expected_monthly["Month"])
    assert list(f1.data[0].y) == list(expected_monthly["Claims"])

    expected_by_member = filtered[filtered["RF_CS_MissingReceipt"] == 1].groupby("Employee").size().to_dict()
    actual_by_member = {cat: val for cat, _name, val in _multi_trace_h_triples(f2)}
    assert actual_by_member == expected_by_member

    expected_tiers = _expected_missing_receipt_tiers(filtered)
    assert list(f4.data[0].y) == list(expected_tiers["Claims"])
    assert list(f4.data[1].y) == list(expected_tiers["Amount"])


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: app/src/workspace_tne.py _p1_update (around line 1042/1070) never filters "
        "travel_requests_no_expense (pre_df) by the page's own member/date filters before "
        "passing it into charts.grouped_count_chart -- only the expense_report side (df) is "
        "filtered. Filtering 'Travel pre-request compliance' to a single ExCo member still "
        "shows every OTHER employee's pre-approval count (e.g. Bob Chen, 0 claims / 1 "
        "pre-approval) even though the member dropdown says 'Alice Wu' only. Expected: the "
        "chart's own Employee categories are a subset of the selected member filter."
    ),
)
def test_p1_precomp_chart_respects_the_member_filter():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    tests = bundle["skill"].get("tests", [])
    meta = workspace_tne._skill_flag_meta(bundle["run"].get("skill_id"), tests)

    result = workspace_tne._p1_update(bundle, None, None, ["Alice Wu"], meta)
    f5 = result[5]
    categories = {*f5.data[0].x, *f5.data[1].x}
    assert categories <= {"Alice Wu"}, categories


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: app/src/workspace_tne.py _filter_by_dates (lines 334-342) compares the raw "
        "string date column directly against a pandas Timestamp -- "
        "`out[date_col] >= pd.to_datetime(start_date)` -- without first parsing date_col to "
        "datetime. On this environment's pandas (3.0.6), a plain string column has dtype "
        "'str' (Arrow-backed), and pandas refuses that comparison outright: "
        "TypeError: Invalid comparison between dtype=str and Timestamp. Every date-range "
        "picker on /workspace/tne's Audit Detail pages (p1 'tne-p1-date', p2 'tne-p2-date', "
        "p3 'tne-p3-date') calls this same helper, so picking ANY start/end date on ANY of "
        "the three pages crashes that page's callback outright -- not a wrong number, a hard "
        "failure. Confirmed here for all three pages against the same run/frames the other "
        "tests in this file use."
    ),
)
def test_date_range_filter_crashes_on_every_audit_detail_page():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    tests = bundle["skill"].get("tests", [])
    meta = workspace_tne._skill_flag_meta(bundle["run"].get("skill_id"), tests)

    # Each call is expected to complete and return real chart data for a
    # real date range -- on the current codebase it raises TypeError instead.
    workspace_tne._p1_update(bundle, "2025-02-01", "2025-03-31", None, meta)
    workspace_tne._p2_update(bundle, "2025-02-01", "2025-03-31", None, None, meta)
    workspace_tne._p3_update(bundle, "2025-02-01", "2025-03-31", None)


# ── Audit Detail / page 1 -- zero-rows-after-filter (honest empty state) ──
#
# A member filter matching nobody (rather than an out-of-range DATE filter,
# which cannot be exercised here -- see test_date_range_filter_crashes_on_
# every_audit_detail_page below) still drives every chart's population to
# zero rows, exactly like an out-of-range date would.


def test_p1_no_matching_member_filter_shows_honest_empty_charts_not_stale_data(bundle, meta):
    result = workspace_tne._p1_update(bundle, None, None, ["Nobody By This Name"], meta)
    kpis, f1, f2, f3, f4, f5 = result[0], result[1], result[2], result[3], result[4], result[5]

    for fig in (f1, f2, f3, f4, f5):
        assert fig.data == (), fig.layout.title.text
        assert "no data" in (fig.layout.title.text or "").lower()

    # The KPI tiles still show a real, honestly-computed zero (sum over an
    # empty selection genuinely is 0), never a stale figure from the
    # unfiltered population.
    assert "$0" in str(kpis[0])
    assert "0" in str(kpis[1])


# ── Audit Detail / page 2 (Detailed risk) -- default view ────────────────


def test_p2_unfiltered_spend_by_employee_matches_independent_role_breakdown(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    expected = expense_df.groupby(["Employee", "role"])[_AMT].sum()
    expected_triples = {(emp, role, round(float(v), 6)) for (emp, role), v in expected.items()}

    result = workspace_tne._p2_update(bundle, None, None, None, None, meta)
    kpis, f1 = result[0], result[1]
    assert _multi_trace_h_triples(f1) == expected_triples

    # Consistency across views: each employee's TOTAL across role segments
    # must equal the Analytics 'Top 10 spenders' bar for that same employee.
    per_employee_total = expense_df.groupby("Employee")[_AMT].sum()
    role_totals: dict = {}
    for cat, _name, val in _multi_trace_h_triples(f1):
        role_totals[cat] = role_totals.get(cat, 0.0) + val
    for emp, total in per_employee_total.items():
        assert abs(role_totals[emp] - total) < 1e-6

    assert f"{len(expense_df):,}" in str(kpis[0])
    assert f"{expense_df['Employee'].nunique():,}" in str(kpis[1])
    expected_avg = expense_df[_AMT].mean()
    assert f"${expected_avg:,.0f}" in str(kpis[2])
    expected_hv = int((expense_df[_AMT] > 5000).sum())
    assert f"{expected_hv:,}" in str(kpis[3])


def test_p2_unfiltered_breach_by_expense_type_matches_independent_groupby(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    flag_cols = [c for c in expense_df.columns if c.startswith("RF_")]
    breach_mask = (expense_df[flag_cols].apply(pd.to_numeric, errors="coerce").fillna(0).astype(int).sum(axis=1) > 0)
    expected = expense_df[breach_mask].groupby("Expense Type").size().to_dict()

    result = workspace_tne._p2_update(bundle, None, None, None, None, meta)
    f3 = result[5]
    assert dict(zip(f3.data[0].y, f3.data[0].x)) == expected


# ── Audit Detail / page 2 -- filtered view ───────────────────────────────


def test_p2_filtered_by_expense_type_matches_independently_filtered_population(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    filtered = expense_df[expense_df["Expense Type"] == "Meals - Domestic Travel"]
    assert len(filtered) == 3  # sanity: fixture rows this test depends on

    result = workspace_tne._p2_update(bundle, None, None, None, ["Meals - Domestic Travel"], meta)
    kpis, f1 = result[0], result[1]
    assert f"{len(filtered):,}" in str(kpis[0])
    assert f"{filtered['Employee'].nunique():,}" in str(kpis[1])

    expected = filtered.groupby(["Employee", "role"])[_AMT].sum()
    expected_triples = {(emp, role, round(float(v), 6)) for (emp, role), v in expected.items()}
    assert _multi_trace_h_triples(f1) == expected_triples

    flag_cols = [c for c in filtered.columns if c.startswith("RF_")]
    breach_mask = (filtered[flag_cols].apply(pd.to_numeric, errors="coerce").fillna(0).astype(int).sum(axis=1) > 0)
    expected_breach = filtered[breach_mask].groupby("Expense Type").size().to_dict()
    f3 = result[5]
    assert dict(zip(f3.data[0].y, f3.data[0].x)) == expected_breach


# ── Audit Detail / page 3 (Receipt & approver review) -- default view ───


def _expected_receipt_status(approval_df: pd.DataFrame) -> pd.Series:
    rep = approval_df["Report Receipt Viewed"].astype(str).str.upper().str.strip()
    entry = approval_df["All Entry Receipts Viewed"].astype(str).str.upper().str.strip()
    return pd.Series([
        "Viewed Report Receipts" if r == "Y" else ("Viewed Entry Receipts" if e == "Y" else "No Receipt Viewed")
        for r, e in zip(rep, entry)
    ], index=approval_df.index)


def test_p3_unfiltered_donut_and_kpis_match_independent_receipt_status(bundle):
    approval_df = bundle["frames"]["approval_aging"]
    status = _expected_receipt_status(approval_df)
    expected_donut = status.value_counts().to_dict()
    viewed = (status.isin(["Viewed Report Receipts", "Viewed Entry Receipts"])).astype(int)
    expected_pct = viewed.mean() * 100
    expected_insufficient = int(approval_df["RF_APR_InsufficientReview"].sum())
    expected_approvers = approval_df["Approver Name"].nunique()

    result = workspace_tne._p3_update(bundle, None, None, None)
    kpis, f1 = result[0], result[1]
    assert _pie_dict(f1) == expected_donut
    assert f"{expected_approvers:,}" in str(kpis[0])
    assert f"{expected_pct:.1f}%" in str(kpis[1])
    assert f"{expected_insufficient:,}" in str(kpis[2])


def test_p3_unfiltered_by_approver_matches_independent_groupby(bundle):
    approval_df = bundle["frames"]["approval_aging"]
    status = _expected_receipt_status(approval_df)
    expected = pd.DataFrame({"Approver": approval_df["Approver Name"], "Status": status}).groupby(
        ["Approver", "Status"]
    ).size()
    expected_triples = {(app, st, int(n)) for (app, st), n in expected.items()}

    result = workspace_tne._p3_update(bundle, None, None, None)
    f2 = result[2]
    assert _multi_trace_v_triples(f2) == expected_triples


def test_p3_unfiltered_combo_and_risk_profile_match_independent_per_approver_rates(bundle):
    approval_df = bundle["frames"]["approval_aging"]
    status = _expected_receipt_status(approval_df)
    viewed = (status.isin(["Viewed Report Receipts", "Viewed Entry Receipts"])).astype(int)
    insufficient = approval_df["RF_APR_InsufficientReview"].astype(int)
    d = pd.DataFrame({
        "Approver": approval_df["Approver Name"], "Viewed": viewed.values, "Insufficient": insufficient.values,
    })
    per_approver = d.groupby("Approver").agg(
        Receipt_Viewed_Pct=("Viewed", lambda s: round(float(s.mean() * 100), 1)),
        Insufficient_Pct=("Insufficient", lambda s: round(float(s.mean() * 100), 1)),
        NoView_Pct=("Viewed", lambda s: round(float((1 - s.mean()) * 100), 1)),
    )

    result = workspace_tne._p3_update(bundle, None, None, None)
    f3, f4 = result[3], result[4]

    combo_viewed = dict(zip(f3.data[0].x, f3.data[0].y))
    combo_insufficient = dict(zip(f3.data[1].x, f3.data[1].y))
    for approver, row in per_approver.iterrows():
        assert combo_viewed[approver] == row["Receipt_Viewed_Pct"]
        assert combo_insufficient[approver] == row["Insufficient_Pct"]

    risk_noview = dict(zip(f4.data[1].y, f4.data[1].x))
    for approver, row in per_approver.iterrows():
        assert risk_noview[approver] == row["NoView_Pct"]


# ── Audit Detail / page 3 -- filtered view ───────────────────────────────


def test_p3_filtered_by_approver_matches_independently_filtered_population(bundle):
    approval_df = bundle["frames"]["approval_aging"]
    filtered = approval_df[approval_df["Approver Name"] == "Dana Price"]
    assert len(filtered) == 2  # sanity: fixture rows this test depends on

    result = workspace_tne._p3_update(bundle, None, None, ["Dana Price"])
    kpis, f1 = result[0], result[1]
    status = _expected_receipt_status(filtered)
    assert _pie_dict(f1) == status.value_counts().to_dict()
    assert "1" in str(kpis[0])  # a single distinct approver in scope
    assert "100.0%" in str(kpis[1])
    assert "0" in str(kpis[2])
