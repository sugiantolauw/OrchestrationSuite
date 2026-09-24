"""Value-level fidelity tests for every KPI tile and table on the three
Audit Detail pages (p1/p2/p3 of /workspace/tne) and on /actions -- gap #4 of
this task's brief. test_chart_data_fidelity.py already covers most p1/p2/p3
KPI tiles and every chart; this file covers what it does not: p1's
Entertainment/attendee table, p2's vendor/exception/claim-vs-pre-approval/
missing-receipt tables and its now-threshold-driven "High-value claims" KPI,
p3's detail table, and /actions' KPI row and action table -- each for the
default (unfiltered) view plus one filter combination, with every expected
value computed independently in this file with plain pandas/python directly
from the fixture data, never by calling src/charts.py, the _pX_update
helpers, or management_actions_page()'s own arithmetic.

Also carries regression coverage for two of this task's other fixes: a
missing RF_ flag column renders "n/a"/"—" on these same tiles/tables, never
a fabricated 0 (gap #2), and the /actions "Open / Under review" KPI counts
the real, title-cased "Under Review" status service.py actually produces
(gap #3).
"""

from __future__ import annotations

import pandas as pd
import pytest
from dash import html

from src import workspace_tne
from src.platform import adapters
from src.platform.pages import management_actions_page

_AMT = workspace_tne._AMOUNT_COL


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


def _bundle_without_column(bundle: dict, frame_name: str, column: str) -> dict:
    """A shallow copy of `bundle` whose named frame has `column` dropped --
    used to exercise the "contract/data gap, not a real zero" code paths
    (CLAUDE.md NN14) without touching fake_service.py's shared fixture."""
    frames = dict(bundle["frames"])
    frames[frame_name] = frames[frame_name].drop(columns=[column])
    return {**bundle, "frames": frames}


def _bundle_without_threshold(bundle: dict, threshold_id: str) -> dict:
    skill = dict(bundle["skill"])
    thresholds = dict(skill.get("thresholds") or {})
    thresholds.pop(threshold_id, None)
    skill["thresholds"] = thresholds
    return {**bundle, "skill": skill}


# ── Audit Detail / page 1 -- Entertainment/attendee table ───────────────────


def _expected_entertainment_table(df: pd.DataFrame) -> pd.DataFrame:
    ent = df[df["Expense Type"].isin(workspace_tne._ENTERTAINMENT_EXPENSE_TYPES)]
    g = ent.groupby("Employee").agg(
        **{"Entertainment Claims": ("Employee", "count"), "_missing": ("RF_ATT_Missing", "sum")}
    )
    g["Missing Attendee Count"] = pd.to_numeric(g["_missing"], errors="coerce").fillna(0)
    g["Missing %"] = (g["Missing Attendee Count"] / g["Entertainment Claims"] * 100).round(1)
    return g.reset_index()


def test_p1_entertainment_table_matches_independent_groupby(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    expected = _expected_entertainment_table(expense_df)
    assert len(expected) == 1  # sanity: fixture rows this test depends on (Carol Ng only)

    result = workspace_tne._p1_update(bundle, None, None, None, meta)
    data = result[6]
    assert len(data) == 1
    row = data[0]
    exp_row = expected.iloc[0]
    assert row["Employee"] == exp_row["Employee"]
    assert row["Entertainment Claims"] == exp_row["Entertainment Claims"]
    assert row["Missing Attendee Count"] == exp_row["Missing Attendee Count"]
    assert row["Missing %"] == exp_row["Missing %"]


def test_p1_entertainment_table_is_empty_when_member_filter_excludes_every_entertainment_row(bundle, meta):
    """Filtered view: Alice Wu has no entertainment-type expense row in the
    fixture, so the table is honestly empty for that selection -- never a
    stale copy of the unfiltered table."""
    result = workspace_tne._p1_update(bundle, None, None, ["Alice Wu"], meta)
    data = result[6]
    assert data == []


# ── Audit Detail / page 2 -- vendor table ────────────────────────────────


def _expected_vendor_table(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("Vendor").agg(
        Claim_Count=("Vendor", "count"), Total_Amount=(_AMT, "sum"),
        Avg_Claim=(_AMT, "mean"), ExCo_Members_Using=("Employee", "nunique"),
    ).reset_index()
    g["Total_Amount"] = g["Total_Amount"].round(2)
    g["Avg_Claim"] = g["Avg_Claim"].round(2)
    return g.sort_values("Total_Amount", ascending=False)


def test_p2_vendor_table_unfiltered_matches_independent_groupby(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    expected = _expected_vendor_table(expense_df)

    result = workspace_tne._p2_update(bundle, None, None, None, None, meta)
    data = result[2]
    assert [r["Vendor"] for r in data] == list(expected["Vendor"])
    for r, (_, exp) in zip(data, expected.iterrows()):
        assert r["Claim_Count"] == exp["Claim_Count"]
        assert r["Total_Amount"] == exp["Total_Amount"]
        assert r["Avg_Claim"] == exp["Avg_Claim"]
        assert r["ExCo_Members_Using"] == exp["ExCo_Members_Using"]


def test_p2_vendor_table_filtered_by_expense_type_matches_independent_groupby(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    filtered = expense_df[expense_df["Expense Type"] == "Meals - Domestic Travel"]
    assert len(filtered) == 3  # sanity: fixture rows this test depends on
    expected = _expected_vendor_table(filtered)

    result = workspace_tne._p2_update(bundle, None, None, None, ["Meals - Domestic Travel"], meta)
    data = result[2]
    assert [r["Vendor"] for r in data] == list(expected["Vendor"])
    for r, (_, exp) in zip(data, expected.iterrows()):
        assert r["Claim_Count"] == exp["Claim_Count"]
        assert r["Total_Amount"] == exp["Total_Amount"]
        assert r["ExCo_Members_Using"] == exp["ExCo_Members_Using"]


# ── Audit Detail / page 2 -- exploded exception table ────────────────────


def test_p2_exception_table_matches_independent_flag_filter(bundle, meta):
    """RF_CS_SplitClaims_SameDay (T5.1's flag) is not a column this fixture's
    expense frame carries, so RF_CS_MissingReceipt (T4.1) is the only
    labelled flag in scope -- the table is exactly its own == 1 rows,
    labelled 'T4.1: Missing receipts' (same label the Analytics-grid chart
    test elsewhere in this suite already established)."""
    expense_df = bundle["frames"]["expense_report"]
    expected = expense_df[expense_df["RF_CS_MissingReceipt"] == 1]
    assert len(expected) == 3  # sanity: fixture rows this test depends on

    result = workspace_tne._p2_update(bundle, None, None, None, None, meta)
    data = result[6]
    assert len(data) == 3
    actual = {(r["Employee"], r["Vendor"], round(r["Amount ($)"], 6), r["Breach Type(s)"]) for r in data}
    expected_set = {
        (row["Employee"], row["Vendor"], round(float(row[_AMT]), 6), "T4.1: Missing receipts")
        for _, row in expected.iterrows()
    }
    assert actual == expected_set


# ── Audit Detail / page 2 -- claims vs pre-approvals table ───────────────
#
# `_p2_update`'s own `cp` table merges this VIEW's (date/member/expense-type
# filtered) claim counts against travel_requests_no_expense UNFILTERED
# (unlike page 1's own precomp chart, which filters `pre_df` too -- see that
# chart's own comment in workspace_tne.py) -- both cases below independently
# reproduce that exact, real shape rather than a hypothetical "both filtered"
# one.


def _expected_cp_table(df: pd.DataFrame, pre_df: pd.DataFrame) -> pd.DataFrame:
    claim_ag = df.groupby("Employee", as_index=False).agg(
        Claims_Filed=("Employee", "count"), Claims_Amount=(_AMT, "sum")
    )
    pre_ag = pre_df.groupby("Employee", as_index=False).agg(
        Pre_Approvals=("Employee", "count"), Pre_Approvals_Amount=("Total Approved Amount (rpt)", "sum")
    )
    cp = claim_ag.merge(pre_ag, on="Employee", how="outer").fillna(0)
    cp["Gap"] = cp["Claims_Filed"] - cp["Pre_Approvals"]
    return cp


def test_p2_claims_vs_pre_approvals_unfiltered_matches_independent_merge(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    pre_df = bundle["frames"]["travel_requests_no_expense"]
    expected = _expected_cp_table(expense_df, pre_df)

    result = workspace_tne._p2_update(bundle, None, None, None, None, meta)
    data = result[8]
    by_employee = {r["Employee"]: r for r in data}
    assert set(by_employee) == set(expected["Employee"])
    for _, exp in expected.iterrows():
        row = by_employee[exp["Employee"]]
        assert row["Claims_Filed"] == exp["Claims_Filed"]
        assert row["Claims_Amount"] == exp["Claims_Amount"]
        assert row["Pre_Approvals"] == exp["Pre_Approvals"]
        assert row["Gap"] == exp["Gap"]


def test_p2_claims_vs_pre_approvals_filtered_matches_independent_merge(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    pre_df = bundle["frames"]["travel_requests_no_expense"]
    filtered = expense_df[expense_df["Expense Type"] == "Meals - Domestic Travel"]
    expected = _expected_cp_table(filtered, pre_df)  # pre_df stays unfiltered -- see docstring above

    result = workspace_tne._p2_update(bundle, None, None, None, ["Meals - Domestic Travel"], meta)
    data = result[8]
    by_employee = {r["Employee"]: r for r in data}
    assert set(by_employee) == set(expected["Employee"])
    for _, exp in expected.iterrows():
        row = by_employee[exp["Employee"]]
        assert row["Claims_Filed"] == exp["Claims_Filed"]
        assert row["Pre_Approvals"] == exp["Pre_Approvals"]
        assert row["Gap"] == exp["Gap"]


# ── Audit Detail / page 2 -- missing receipt by employee table ───────────


def _expected_missing_table(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("Employee").agg(
        Total_Claims=("Employee", "count"), Total_At_Risk=(_AMT, "sum"),
        **{"_miss": ("RF_CS_MissingReceipt", "sum")},
    )
    g["Claims_Missing_Receipt"] = pd.to_numeric(g["_miss"], errors="coerce").fillna(0)
    g["Missing %"] = (g["Claims_Missing_Receipt"] / g["Total_Claims"] * 100).round(1)
    return g.reset_index()


def test_p2_missing_receipt_table_unfiltered_matches_independent_groupby(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    expected = _expected_missing_table(expense_df)

    result = workspace_tne._p2_update(bundle, None, None, None, None, meta)
    data = result[11]
    by_employee = {r["Employee"]: r for r in data}
    assert set(by_employee) == set(expected["Employee"])
    for _, exp in expected.iterrows():
        row = by_employee[exp["Employee"]]
        assert row["Total_Claims"] == exp["Total_Claims"]
        assert row["Claims_Missing_Receipt"] == exp["Claims_Missing_Receipt"]
        assert row["Missing %"] == exp["Missing %"]


def test_p2_missing_receipt_table_filtered_matches_independent_groupby(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    filtered = expense_df[expense_df["Expense Type"] == "Meals - Domestic Travel"]
    expected = _expected_missing_table(filtered)

    result = workspace_tne._p2_update(bundle, None, None, None, ["Meals - Domestic Travel"], meta)
    data = result[11]
    by_employee = {r["Employee"]: r for r in data}
    assert set(by_employee) == set(expected["Employee"])
    for _, exp in expected.iterrows():
        row = by_employee[exp["Employee"]]
        assert row["Total_Claims"] == exp["Total_Claims"]
        assert row["Claims_Missing_Receipt"] == exp["Claims_Missing_Receipt"]
        assert row["Missing %"] == exp["Missing %"]


def test_p2_missing_receipt_table_shows_na_never_zero_when_flag_column_absent(bundle, meta):
    """Gap #2 regression: RF_CS_MissingReceipt absent from the run's own
    data entirely (a contract/data gap) must never render as "0 claims
    missing a receipt" for every employee -- CLAUDE.md NN14."""
    expense_df = bundle["frames"]["expense_report"]
    modified = _bundle_without_column(bundle, "expense_report", "RF_CS_MissingReceipt")

    result = workspace_tne._p2_update(modified, None, None, None, None, meta)
    data = result[11]
    assert len(data) == expense_df["Employee"].nunique()
    for row in data:
        assert row["Claims_Missing_Receipt"] == "n/a"
        assert row["Missing %"] == "n/a"
        assert row["Total_Claims"] == int((expense_df["Employee"] == row["Employee"]).sum())


# ── Audit Detail / page 1 -- "Missing receipts" KPI, flag column absent ──


def test_p1_missing_receipts_kpi_shows_na_never_zero_when_flag_column_absent(bundle, meta):
    """Gap #2 regression, p1's own KPI tile version of the table test above."""
    modified = _bundle_without_column(bundle, "expense_report", "RF_CS_MissingReceipt")

    result = workspace_tne._p1_update(modified, None, None, None, meta)
    kpis = result[0]
    assert "n/a" in str(kpis[3])
    assert "0" not in str(kpis[3])


# ── Audit Detail / page 2 -- "High-value claims" KPI (gap #1) ────────────


def test_p2_high_value_kpi_reads_the_real_skill_threshold_not_a_hardcoded_5000(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    threshold = bundle["skill"]["thresholds"]["high_value_limit"]["value"]
    assert threshold == 5000  # sanity: skills/tne_exco/thresholds.yaml's real value
    expected_hv = int((expense_df[_AMT] > threshold).sum())
    assert expected_hv == 1  # sanity: fixture rows this test depends on ($5,500 Carol Ng claim)

    result = workspace_tne._p2_update(bundle, None, None, None, None, meta)
    kpis = result[0]
    assert f"{expected_hv:,}" in str(kpis[3])
    assert "$5K" in str(kpis[3])


def test_p2_high_value_kpi_filtered_matches_independent_threshold_comparison(bundle, meta):
    expense_df = bundle["frames"]["expense_report"]
    threshold = bundle["skill"]["thresholds"]["high_value_limit"]["value"]
    filtered = expense_df[expense_df["Expense Type"] == "Meals - Domestic Travel"]
    expected_hv = int((filtered[_AMT] > threshold).sum())
    assert expected_hv == 0  # sanity: none of this filtered set's amounts exceed $5,000

    result = workspace_tne._p2_update(bundle, None, None, None, ["Meals - Domestic Travel"], meta)
    kpis = result[0]
    assert f"{expected_hv:,}" in str(kpis[3])


def test_p2_high_value_kpi_shows_na_never_zero_when_threshold_missing_from_skill(bundle, meta):
    """CLAUDE.md NN14: an un-computable threshold-driven KPI must degrade to
    "n/a", never a fabricated 0 that would misreport 'zero high-value
    claims'."""
    modified = _bundle_without_threshold(bundle, "high_value_limit")

    result = workspace_tne._p2_update(modified, None, None, None, None, meta)
    kpis = result[0]
    assert "High-value claims" in str(kpis[3])
    assert "n/a" in str(kpis[3])


# ── Audit Detail / page 3 -- detail table ─────────────────────────────────


def _expected_receipt_status(approval_df: pd.DataFrame) -> pd.Series:
    rep = approval_df["Report Receipt Viewed"].astype(str).str.upper().str.strip()
    entry = approval_df["All Entry Receipts Viewed"].astype(str).str.upper().str.strip()
    return pd.Series([
        "Viewed Report Receipts" if r == "Y" else ("Viewed Entry Receipts" if e == "Y" else "No Receipt Viewed")
        for r, e in zip(rep, entry)
    ], index=approval_df.index)


def test_p3_detail_table_unfiltered_matches_independent_per_row_computation(bundle):
    approval_df = bundle["frames"]["approval_aging"]
    expected_status = _expected_receipt_status(approval_df)

    result = workspace_tne._p3_update(bundle, None, None, None)
    data = result[5]
    assert len(data) == len(approval_df)
    by_report = {r["Report ID"]: r for r in data}
    for report_id, exp_status in zip(approval_df["Report ID"], expected_status):
        row = approval_df[approval_df["Report ID"] == report_id].iloc[0]
        assert by_report[report_id]["Receipt_Status"] == exp_status
        assert by_report[report_id]["Insufficient review (RF_APR_InsufficientReview)"] == int(row["RF_APR_InsufficientReview"])
        assert by_report[report_id]["Minutes of Approval from Receipt View"] == row["Minutes of Approval from Receipt View"]


def test_p3_detail_table_filtered_matches_independent_per_row_computation(bundle):
    approval_df = bundle["frames"]["approval_aging"]
    filtered = approval_df[approval_df["Approver Name"] == "Dana Price"]
    assert len(filtered) == 2  # sanity: fixture rows this test depends on
    expected_status = _expected_receipt_status(filtered)

    result = workspace_tne._p3_update(bundle, None, None, ["Dana Price"])
    data = result[5]
    assert len(data) == 2
    by_report = {r["Report ID"]: r for r in data}
    for report_id, exp_status in zip(filtered["Report ID"], expected_status):
        assert by_report[report_id]["Receipt_Status"] == exp_status
        assert by_report[report_id]["Approver Name"] == "Dana Price"


# ── /actions -- KPI tiles and table (gap #3, plus its own value fidelity) ─
#
# Deliberately NOT reached through _completed_run()/fake_service.py's
# auto-generated, always-"draft" management actions (the real gap #3 bug
# only shows up against a genuinely varied status set) -- a hand-built,
# realistic action list local to this file, following the same pattern
# test_layouts.py already uses for this same page.

_ACTIONS_FIXTURE = [
    {"action_id": "MA-1", "finding_id": "F1", "finding_title": "Missing receipts",
     "skill_id": "SKILL-001", "skill_name": "T&E ExCo", "run_id": "RUN-1",
     "risk": "High", "owner": "Alex Chen", "status": "Open", "target_date": "2026-03-01",
     "potential_exposure": 6825.0, "evidence_link": "T4.1"},
    {"action_id": "MA-2", "finding_id": "F2", "finding_title": "Split claims same day",
     "skill_id": "SKILL-001", "skill_name": "T&E ExCo", "run_id": "RUN-1",
     "risk": "High", "owner": "Alex Chen", "status": "Under Review", "target_date": "2026-03-15",
     "potential_exposure": 1725.0, "evidence_link": "T5.1"},
    {"action_id": "MA-3", "finding_id": "F3", "finding_title": "Late booking",
     "skill_id": "SKILL-001", "skill_name": "T&E ExCo", "run_id": "RUN-1",
     "risk": "Medium", "owner": "Jan Kowalski", "status": "Agreed", "target_date": "2026-04-01",
     "potential_exposure": 900.0, "evidence_link": "T3.3a"},
    {"action_id": "MA-4", "finding_id": "F4", "finding_title": "Insufficient approver review",
     "skill_id": "SKILL-001", "skill_name": "T&E ExCo", "run_id": "RUN-1",
     "risk": "Medium", "owner": "Jan Kowalski", "status": "Remediated", "target_date": "2026-02-01",
     "potential_exposure": None, "evidence_link": "T6.1a"},
    {"action_id": "MA-5", "finding_id": "F5", "finding_title": "Duplicate claims",
     "skill_id": "SKILL-001", "skill_name": "T&E ExCo", "run_id": "RUN-1",
     "risk": "Low", "owner": "Dana Price", "status": "Closed", "target_date": "2026-01-15",
     "potential_exposure": 300.0, "evidence_link": "T5.2"},
]


def _kpi_values(layout) -> dict[str, str]:
    """Walks the page tree for every 'kpi-tile' Div and returns {label:
    value} -- the same shape kpi_card() builds (components.py), read back
    independently of how management_actions_page() assembles the row."""
    out: dict[str, str] = {}

    def walk(node):
        if node is None or isinstance(node, (str, int, float)):
            return
        if isinstance(node, (list, tuple)):
            for n in node:
                walk(n)
            return
        cls = (getattr(node, "className", None) or "")
        if cls == "kpi-tile":
            label, value = node.children[0].children, node.children[1].children
            out[label] = value
            return
        walk(getattr(node, "children", None))

    walk(layout)
    return out


def _action_table_rows(layout) -> list[html.Tr]:
    def walk(node):
        if node is None or isinstance(node, (str, int, float)):
            return None
        if isinstance(node, (list, tuple)):
            for n in node:
                found = walk(n)
                if found is not None:
                    return found
            return None
        if getattr(node, "id", None) == "actions-table-body":
            return node.children
        return walk(getattr(node, "children", None))

    rows = walk(layout)
    return rows or []


def test_actions_kpi_tiles_match_independent_computation(monkeypatch):
    import fake_service

    monkeypatch.setattr(fake_service, "list_management_actions", lambda ctx, filters=None: _ACTIONS_FIXTURE)
    monkeypatch.setattr(fake_service, "list_runs", lambda ctx, filters=None: [])

    expected_total = len(_ACTIONS_FIXTURE)
    # Gap #3 regression: "Open" + the real, title-cased "Under Review" --
    # matching orchestrator.service.list_management_actions()'s own
    # `.replace("_", " ").title()` output, never the prototype's literal,
    # never-matching lowercase-r "Under review".
    expected_open = sum(1 for a in _ACTIONS_FIXTURE if a["status"] in ("Open", "Under Review"))
    assert expected_open == 2  # sanity: MA-1 (Open) + MA-2 (Under Review)
    expected_high = sum(1 for a in _ACTIONS_FIXTURE if a["risk"] == "High")

    layout = management_actions_page()
    kpis = _kpi_values(layout)
    assert kpis["Total actions"] == str(expected_total)
    assert kpis["Open / Under review"] == str(expected_open)
    assert kpis["High risk"] == str(expected_high)


def test_actions_table_rows_match_independent_per_action_values(monkeypatch):
    import fake_service

    monkeypatch.setattr(fake_service, "list_management_actions", lambda ctx, filters=None: _ACTIONS_FIXTURE)
    layout = management_actions_page()
    rows = _action_table_rows(layout)
    assert len(rows) == len(_ACTIONS_FIXTURE)

    for row, expected in zip(rows, _ACTIONS_FIXTURE):
        cells = row.children
        assert cells[0].children == expected["finding_title"]
        assert cells[1].children == expected["skill_name"]
        assert cells[2].children.children == expected["risk"]
        assert cells[3].children == expected["owner"]
        assert cells[4].children.children == expected["status"]
        assert cells[5].children == expected["target_date"]
        expected_money = f"${expected['potential_exposure']:,.0f}" if expected["potential_exposure"] is not None else "—"
        assert cells[6].children == expected_money
        assert cells[7].children == expected["evidence_link"]


def test_actions_kpi_tiles_one_filter_combination_matches_independent_computation(monkeypatch):
    """The 'one filter combination' required by this task's brief: the same
    page function fed a genuinely filtered action list (server-side
    filters={"risk": "High"} -- /actions' own dropdowns are not yet wired to
    a callback, a separate, pre-existing gap outside this task's scope), so
    the KPI arithmetic is exercised against a second, different population."""
    import fake_service

    filtered = [a for a in _ACTIONS_FIXTURE if a["risk"] == "High"]
    monkeypatch.setattr(fake_service, "list_management_actions", lambda ctx, filters=None: filtered)
    monkeypatch.setattr(fake_service, "list_runs", lambda ctx, filters=None: [])

    expected_open = sum(1 for a in filtered if a["status"] in ("Open", "Under Review"))
    assert expected_open == 2  # sanity: both High-risk actions are Open/Under Review

    layout = management_actions_page()
    kpis = _kpi_values(layout)
    assert kpis["Total actions"] == str(len(filtered))
    assert kpis["Open / Under review"] == str(expected_open)
    assert kpis["High risk"] == str(len(filtered))
