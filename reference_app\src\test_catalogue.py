"""Test Catalogue — definitions for all audit control tests.

Each test has an ID, control objective, population, rule description,
threshold, data source, and status logic.  The catalogue is used to:
  1. Render the Test Catalogue tab.
  2. Drive finding generation (build_all_findings references the same IDs).
  3. Populate the PPTX methodology appendix.
"""

TEST_CATALOGUE = [
    {
        "test_id": "T3.1a",
        "category": "Pre-travel",
        "control_objective": "All travel should be pre-approved before expenses are incurred",
        "test_name": "Pre-approvals Not Linked to Expense Reports",
        "population": "Authorised travel requests without matching expense report entry",
        "rule": "Identify travel requests in the pre-approval register that have no linked expense report",
        "threshold": "Zero unlinked requests expected",
        "source_file_key": "travel_requests",
        "metric_keys": ["preapproval_unlinked_count", "preapproval_unlinked_amount", "preapproval_employees"],
        "primary_metric": "preapproval_unlinked_count",
        "flag_col": None,
    },
    {
        "test_id": "T3.1b",
        "category": "Pre-travel",
        "control_objective": "All travel should be pre-approved before expenses are incurred",
        "test_name": "Employees Claiming Without Pre-Approval",
        "population": "Expense claimants with no corresponding travel request on file",
        "rule": "Match expense-report employees to travel-request employees; flag those with no match",
        "threshold": "Zero employees without pre-approval expected",
        "source_file_key": "travel_requests",
        "metric_keys": ["no_preapproval_employees"],
        "primary_metric": "no_preapproval_employees",
        "flag_col": None,
    },
    {
        "test_id": "T3.2a",
        "category": "Pre-travel",
        "control_objective": "Preferred suppliers should be used to maximise negotiated rates",
        "test_name": "Non-Preferred Supplier Usage",
        "population": "All ExCo bookings (airline, accommodation, car rental)",
        "rule": "Compare booking supplier against preferred-supplier list per category",
        "threshold": "Zero non-preferred bookings expected",
        "source_file_key": "expense_report",
        "metric_keys": ["pref_dom_airline", "pref_int_airline", "pref_dom_accom",
                        "pref_int_accom", "pref_dom_car", "pref_int_car"],
        "primary_metric": None,  # sum of all pref_ metrics
        "flag_col": ["RF_SP_DomAirline_NotPreferred", "RF_SP_IntAirline_NotPreferred",
                     "RF_SP_DomAccom_NotPreferred", "RF_SP_IntAccom_NotPreferred",
                     "RF_SP_DomCarRental_NotPreferred", "RF_SP_IntCarRental_NotPreferred"],
    },
    {
        "test_id": "T3.3a",
        "category": "Pre-travel",
        "control_objective": "Travel should be booked in advance to obtain best fares",
        "test_name": "Late / Urgent Travel Bookings",
        "population": "All ExCo bookings with Advance Purchase Days data",
        "rule": "Flag bookings < 14 days before departure; sub-flag < 3 days as very late",
        "threshold": "< 14 days = late; < 3 days = very late",
        "source_file_key": "booking_detail",
        "metric_keys": ["late_booking_count", "very_late_booking_count", "late_booking_employees"],
        "primary_metric": "late_booking_count",
        "flag_col": ["RF_CS_LateBooking", "RF_CS_VeryLateBooking"],
    },
    {
        "test_id": "T3.3b",
        "category": "Pre-travel",
        "control_objective": "Entertainment spend should comply with per-head limits",
        "test_name": "Entertainment Per-Head Threshold Exceedance",
        "population": "Entertainment/meal claims with attendee count > 0",
        "rule": "Calculate per-head cost; flag if > $40 (internal) or > $80 (external)",
        "threshold": "$40 internal / $80 external per head",
        "source_file_key": "expense_report",
        "metric_keys": ["ent_over_internal_count", "ent_over_external_count", "ent_over_amount", "ent_assessed"],
        "primary_metric": "ent_over_internal_count",
        "flag_col": None,
    },
    {
        "test_id": "T4.1",
        "category": "Spend compliance",
        "control_objective": "All claims must have supporting receipt documentation",
        "test_name": "Missing Receipt Documentation",
        "population": "All ExCo expense claims",
        "rule": "Flag claims where RF_CS_MissingReceipt = 1",
        "threshold": "Zero missing receipts expected",
        "source_file_key": "missing_receipt",
        "metric_keys": ["missing_receipt_count", "missing_receipt_pct", "missing_receipt_amount"],
        "primary_metric": "missing_receipt_count",
        "flag_col": "RF_CS_MissingReceipt",
    },
    {
        "test_id": "T4.2",
        "category": "Spend compliance",
        "control_objective": "Entertainment claims must list attendees and business purpose",
        "test_name": "Missing Attendee Details",
        "population": "Entertainment, function, and gift expense claims",
        "rule": "Flag entertainment claims where RF_ATT_Missing = 1",
        "threshold": "Zero missing attendee records expected",
        "source_file_key": "expense_report",
        "metric_keys": ["att_missing_count", "att_total", "att_missing_pct"],
        "primary_metric": "att_missing_count",
        "flag_col": "RF_ATT_Missing",
    },
    {
        "test_id": "T4.3",
        "category": "Spend compliance",
        "control_objective": "Business claims should not include personal expenses",
        "test_name": "Potential Personal Expenses in Business Claims",
        "population": "All ExCo expense claims",
        "rule": "Flag claims where description, vendor, or category suggests personal use",
        "threshold": "Zero personal expenses expected",
        "source_file_key": "expense_report",
        "metric_keys": ["personal_expense_count", "personal_expense_amount", "personal_expense_employees"],
        "primary_metric": "personal_expense_count",
        "flag_col": "RF_CS_PersonalExpense",
    },
    {
        "test_id": "T4.4",
        "category": "Spend compliance",
        "control_objective": "High-value claims require enhanced review",
        "test_name": "High-Value Reimbursements (> $5,000)",
        "population": "All ExCo expense claims",
        "rule": "Flag claims where Expense Amount > $5,000",
        "threshold": "$5,000 per claim",
        "source_file_key": "expense_report",
        "metric_keys": ["hv_count", "hv_amount", "hv_employees"],
        "primary_metric": "hv_count",
        "flag_col": "RF_CS_Reimbursement_GT_5K",
    },
    {
        "test_id": "T5.1",
        "category": "Fraud risk",
        "control_objective": "Claims should not be split to circumvent approval thresholds",
        "test_name": "Potential Split Claims",
        "population": "ExCo claims grouped by Employee + Vendor + Date + Expense Type",
        "rule": "Flag groups where SUM > $5,000 AND COUNT > 1 (same-day and ±2-day window)",
        "threshold": "$5,000 aggregate; excludes FCM and The Moving Company",
        "source_file_key": "expense_report",
        "metric_keys": ["split_same_day", "split_window"],
        "primary_metric": None,  # sum of both
        "flag_col": ["RF_CS_SplitClaims_SameDay", "RF_CS_SplitClaims_Window"],
    },
    {
        "test_id": "T5.2",
        "category": "Fraud risk",
        "control_objective": "Duplicate claims should be detected and prevented",
        "test_name": "Duplicate Expense Claims",
        "population": "ExCo claims matched on Employee + Date + Vendor + Amount",
        "rule": "Flag exact duplicates; sub-test for same claim on both OOP and corporate card",
        "threshold": "Zero duplicates expected",
        "source_file_key": "expense_report",
        "metric_keys": ["duplicate_count", "duplicate_oop_amex"],
        "primary_metric": None,  # sum of both
        "flag_col": ["RF_CS_Duplicate", "RF_CS_Duplicate_OOP_AMEX"],
    },
    {
        "test_id": "T6.1a",
        "category": "Approval effectiveness",
        "control_objective": "Approvers should review receipts and spend adequate time on approval",
        "test_name": "Approver Review Sufficiency",
        "population": "All expense-report approvals by ExCo members",
        "rule": "Flag approvals where receipt was not viewed and/or approval was instant (< 1 min)",
        "threshold": "100% receipt-viewed; zero instant approvals expected",
        "source_file_key": "approval_aging",
        "metric_keys": ["approver_total_reports", "approver_count", "approver_no_receipt_pct",
                        "approver_instant_pct", "approver_worst_case_pct", "approver_worst_case_n"],
        "primary_metric": "approver_worst_case_n",
        "flag_col": None,
    },
    {
        "test_id": "T6.1c",
        "category": "Approval effectiveness",
        "control_objective": "Entertainment attendees should not be direct reports of the claimant",
        "test_name": "Attendee Hierarchy Validity",
        "population": "Entertainment claims with attendee records",
        "rule": "Cross-reference attendees against the claimant's direct-report hierarchy",
        "threshold": "Zero hierarchy exceptions expected",
        "source_file_key": "attendee_validity",
        "metric_keys": ["att_hierarchy_invalid", "att_hierarchy_total"],
        "primary_metric": "att_hierarchy_invalid",
        "flag_col": None,
    },
    {
        "test_id": "T6.1d",
        "category": "Approval effectiveness",
        "control_objective": "Daily spend per employee should not exceed policy limits",
        "test_name": "Daily Spend Limit Exceedances",
        "population": "All ExCo claims aggregated by Employee + Transaction Date",
        "rule": "Sum daily spend per employee; flag days exceeding $1,000",
        "threshold": "$1,000 per employee per day",
        "source_file_key": "expense_report",
        "metric_keys": ["daily_over_count", "daily_over_amount", "daily_over_employees", "daily_over_max"],
        "primary_metric": "daily_over_count",
        "flag_col": None,
    },
]


CATEGORY_ORDER = ["Pre-travel", "Spend compliance", "Fraud risk", "Approval effectiveness"]


def get_test_status(test, metrics):
    """Determine test status: Pass / Exception / Not testable."""
    m = metrics
    tid = test["test_id"]

    # Sum-based tests
    if tid == "T3.2a":
        total = sum(m.get(k, {}).get("value", 0) for k in test["metric_keys"])
        return ("Exception", total) if total > 0 else ("Pass", 0)
    if tid in ("T5.1", "T5.2"):
        total = sum(m.get(k, {}).get("value", 0) for k in test["metric_keys"])
        return ("Exception", total) if total > 0 else ("Pass", 0)

    pk = test.get("primary_metric")
    if pk is None:
        return ("Not testable", 0)

    val = m.get(pk, {}).get("value", 0)
    if val is None:
        return ("Not testable", 0)
    return ("Exception", val) if val > 0 else ("Pass", 0)


def build_catalogue_rows(metrics):
    """Build a list of dicts for the test catalogue table."""
    rows = []
    for t in TEST_CATALOGUE:
        status, count = get_test_status(t, metrics)
        rows.append({
            "Test ID": t["test_id"],
            "Category": t["category"],
            "Test Name": t["test_name"],
            "Control Objective": t["control_objective"],
            "Population": t["population"],
            "Rule": t["rule"],
            "Threshold": t["threshold"],
            "Exceptions": count,
            "Status": status,
        })
    return rows
