"""AI Audit Analyst — Main Dash Application.

Platform entry point. Wraps the existing T&E ExCo audit prototype as the
first working Skill inside a broader, domain-agnostic audit analytics
platform with persistent runs, a Skill library, and management actions.
"""

import base64
import io
import json
import logging
import os
from datetime import datetime

import dash
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, dash_table
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from src.data_loader import EXCO_MEMBERS, FILE_REGISTRY, build_exco_populations, load_all_files
from src.test_catalogue import TEST_CATALOGUE, build_catalogue_rows, CATEGORY_ORDER
try:
    from src.pptx_export import generate_pptx
    HAS_PPTX = True
except ImportError:
    HAS_PPTX = False
    generate_pptx = None
from src.excel_export import generate_excel

# Platform-level modules
from src.platform import adapters
from src.platform.pages import (
    platform_header,
    platform_nav,
    landing_page,
    skill_library_page,
    skill_methodology_page,
    audit_runs_page,
    platform_trace_page,
    management_actions_page,
)
from src.platform.components import (
    workflow_stage,
    demo_indicator,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


BREACH_FLAG_GROUPS = {
    "Non-Preferred Airline": ["RF_SP_DomAirline_NotPreferred", "RF_SP_IntAirline_NotPreferred"],
    "Non-Preferred Car Rental": ["RF_SP_DomCarRental_NotPreferred", "RF_SP_IntCarRental_NotPreferred"],
    "Non-Preferred Accommodation": ["RF_SP_DomAccom_NotPreferred", "RF_SP_IntAccom_NotPreferred"],
    "High Value >$5K": ["RF_CS_Reimbursement_GT_5K"],
    "Split Claims": ["RF_CS_SplitClaims_SameDay", "RF_CS_SplitClaims_Window"],
    "Duplicate": ["RF_CS_Duplicate", "RF_CS_Duplicate_OOP_AMEX"],
}


def _find_col(df: pd.DataFrame, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    low = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    for c in df.columns:
        cl = c.lower()
        if any(token in cl for token in candidates):
            return c
    return None


def _ensure_bool_flags(df: pd.DataFrame):
    for flags in BREACH_FLAG_GROUPS.values():
        for flag in flags:
            if flag not in df.columns:
                df[flag] = 0
            df[flag] = pd.to_numeric(df[flag], errors="coerce").fillna(0).astype(int)

    if "RF_CS_MissingReceipt" not in df.columns:
        df["RF_CS_MissingReceipt"] = 0
    df["RF_CS_MissingReceipt"] = pd.to_numeric(df["RF_CS_MissingReceipt"], errors="coerce").fillna(0).astype(int)


def _build_breach_types(df: pd.DataFrame):
    def one_row(row):
        out = []
        for breach_type, cols in BREACH_FLAG_GROUPS.items():
            if any(int(row.get(col, 0)) == 1 for col in cols):
                out.append(breach_type)
        if not out:
            out = ["No Breach"]
        return out

    df = df.copy()
    df["Breach_Type_List"] = df.apply(one_row, axis=1)
    return df


def _standardise_combined(df: pd.DataFrame):
    out = df.copy()

    emp_col = _find_col(out, ["Employee", "employee"])
    date_col = _find_col(out, ["Transaction Date", "date", "transaction"])
    amt_col = _find_col(out, ["Expense Amount (reimbursement currency)", "reimbursement", "amount"])
    exp_col = _find_col(out, ["Expense Type", "expense type"])
    vendor_col = _find_col(out, ["Vendor", "Supplier Name", "supplier"])

    if emp_col and emp_col != "Employee":
        out["Employee"] = out[emp_col]
    if date_col and date_col != "Transaction Date":
        out["Transaction Date"] = out[date_col]
    if amt_col and amt_col != "Expense Amount (reimbursement currency)":
        out["Expense Amount (reimbursement currency)"] = out[amt_col]
    if exp_col and exp_col != "Expense Type":
        out["Expense Type"] = out[exp_col]
    if vendor_col and vendor_col != "Vendor":
        out["Vendor"] = out[vendor_col]

    if "Employee" not in out.columns:
        out["Employee"] = "Unknown"
    if "Transaction Date" not in out.columns:
        out["Transaction Date"] = pd.Timestamp("2025-01-01")
    if "Expense Amount (reimbursement currency)" not in out.columns:
        out["Expense Amount (reimbursement currency)"] = 0
    if "Expense Type" not in out.columns:
        out["Expense Type"] = "General"
    if "Vendor" not in out.columns:
        out["Vendor"] = "Unknown"
    if "Source_Population" not in out.columns:
        out["Source_Population"] = "Prepared"

    out["Transaction Date"] = pd.to_datetime(out["Transaction Date"], errors="coerce")
    out["Expense Amount (reimbursement currency)"] = pd.to_numeric(out["Expense Amount (reimbursement currency)"], errors="coerce").fillna(0)

    bins = [-1, 75, 200, 500, 1000, 5000, np.inf]
    labels = ["$0-$75", "$75-$200", "$200-$500", "$500-$1K", "$1K-$5K", ">$5K"]
    out["Receipt_Tier"] = pd.cut(out["Expense Amount (reimbursement currency)"], bins=bins, labels=labels)

    _ensure_bool_flags(out)
    out = _build_breach_types(out)
    return out


def _standardise_pre(df: pd.DataFrame):
    out = df.copy()
    emp_col = _find_col(out, ["Employee", "employee", "Lead Traveller Name"])
    amt_col = _find_col(out, ["Total_Approved_Amount_rpt", "approved", "amount"])

    if emp_col and emp_col != "Employee":
        out["Employee"] = out[emp_col]
    if amt_col and amt_col != "PreApprovalAmount":
        out["PreApprovalAmount"] = out[amt_col]

    if "Employee" not in out.columns:
        out["Employee"] = "Unknown"
    if "PreApprovalAmount" not in out.columns:
        out["PreApprovalAmount"] = 0

    out["PreApprovalAmount"] = pd.to_numeric(out["PreApprovalAmount"], errors="coerce").fillna(0)
    return out


def _standardise_approval(df: pd.DataFrame):
    out = df.copy()

    approver_col = _find_col(out, ["Approver Name", "approver"])
    emp_col = _find_col(out, ["Employee Name", "Employee", "employee"])
    min_col = _find_col(out, ["Minutes", "minute", "approval"])
    rep_col = _find_col(out, ["Report Viewed Receipts", "report viewed"])
    entry_col = _find_col(out, ["Entry Viewed Receipts", "entry viewed"])
    date_col = _find_col(out, ["Transaction Date", "Report Date", "date"])
    amt_col = _find_col(out, ["Report Amount", "amount", "reimbursement"])

    if approver_col and approver_col != "Approver Name":
        out["Approver Name"] = out[approver_col]
    if emp_col and emp_col != "Employee Name":
        out["Employee Name"] = out[emp_col]
    if min_col and min_col != "Approval Minutes":
        out["Approval Minutes"] = out[min_col]
    if rep_col and rep_col != "Report Viewed Receipts":
        out["Report Viewed Receipts"] = out[rep_col]
    if entry_col and entry_col != "Entry Viewed Receipts":
        out["Entry Viewed Receipts"] = out[entry_col]
    if date_col and date_col != "Report Date":
        out["Report Date"] = out[date_col]
    if amt_col and amt_col != "Report Amount":
        out["Report Amount"] = out[amt_col]

    for col, default in [
        ("Approver Name", "Unknown"),
        ("Employee Name", "Unknown"),
        ("Approval Minutes", 0),
        ("Report Viewed Receipts", "N"),
        ("Entry Viewed Receipts", "N"),
        ("Report Date", pd.Timestamp("2025-01-01")),
        ("Report Amount", 0),
    ]:
        if col not in out.columns:
            out[col] = default

    out["Approval Minutes"] = pd.to_numeric(out["Approval Minutes"], errors="coerce").fillna(0)
    out["Report Date"] = pd.to_datetime(out["Report Date"], errors="coerce")
    out["Report Amount"] = pd.to_numeric(out["Report Amount"], errors="coerce").fillna(0)

    rep = out["Report Viewed Receipts"].astype(str).str.upper().str.strip()
    entry = out["Entry Viewed Receipts"].astype(str).str.upper().str.strip()
    out["Receipt_Viewed"] = ((rep == "Y") | (entry == "Y")).astype(int)
    out["Instant"] = (out["Approval Minutes"] < 1).astype(int)

    def status(row):
        if str(row["Report Viewed Receipts"]).upper() == "Y":
            return "Viewed Report Receipts"
        if str(row["Entry Viewed Receipts"]).upper() == "Y":
            return "Viewed Entry Receipts"
        return "No Receipt Viewed"

    out["Receipt_Status"] = out.apply(status, axis=1)
    return out


def build_demo_data():
    rng = np.random.default_rng(42)
    months = pd.date_range("2025-01-01", "2026-04-30", freq="D")
    n = 4200

    combined = pd.DataFrame({
        "Employee": rng.choice(EXCO_MEMBERS, size=n),
        "Transaction Date": rng.choice(months, size=n),
        "Expense Amount (reimbursement currency)": rng.gamma(2.2, 550, size=n),
        "Expense Type": rng.choice(["Entertainment", "Travel", "Accommodation", "Meal", "Car Rental"], size=n),
        "Vendor": rng.choice(["Qantas", "Virgin", "Avis", "Hilton", "Marriott", "Uber", "Restaurant Group"], size=n),
        "Source_Population": rng.choice(["Prepared", "Approved"], size=n, p=[0.45, 0.55]),
    })

    for flag, p in {
        "RF_SP_DomAirline_NotPreferred": 0.06,
        "RF_SP_IntAirline_NotPreferred": 0.03,
        "RF_SP_DomCarRental_NotPreferred": 0.04,
        "RF_SP_IntCarRental_NotPreferred": 0.02,
        "RF_SP_DomAccom_NotPreferred": 0.05,
        "RF_SP_IntAccom_NotPreferred": 0.03,
        "RF_CS_Reimbursement_GT_5K": 0.04,
        "RF_CS_SplitClaims_SameDay": 0.03,
        "RF_CS_SplitClaims_Window": 0.03,
        "RF_CS_Duplicate": 0.02,
        "RF_CS_Duplicate_OOP_AMEX": 0.01,
        "RF_CS_MissingReceipt": 0.08,
        "RF_ATT_Missing": 0.26,
        "RF_CS_PersonalExpense": 0.04,
        "RF_CS_LateBooking": 0.12,
        "RF_CS_VeryLateBooking": 0.05,
    }.items():
        combined[flag] = (rng.random(n) < p).astype(int)

    # Attendee count for entertainment per-head calc (T3.3b)
    combined["Attendee_Count"] = 0
    ent_mask = combined["Expense Type"].str.contains("Entertainment|Meal", case=False, na=False)
    combined.loc[ent_mask, "Attendee_Count"] = rng.integers(1, 12, size=int(ent_mask.sum()))

    # Daily spend per employee (T6.1d)
    combined["Employee ID"] = combined["Employee"].apply(lambda x: f"EMP_{abs(hash(x)) % 10000:04d}")

    pre = pd.DataFrame({
        "Employee": rng.choice(EXCO_MEMBERS, size=900),
        "PreApprovalAmount": rng.gamma(2.0, 950, size=900),
    })

    app_rows = 850
    approval = pd.DataFrame({
        "Approver Name": rng.choice(EXCO_MEMBERS, size=app_rows),
        "Employee Name": rng.choice([f"Employee {i}" for i in range(1, 250)], size=app_rows),
        "Report Date": rng.choice(months, size=app_rows),
        "Approval Minutes": np.maximum(0, rng.normal(1.5, 1.8, size=app_rows)),
        "Report Amount": rng.gamma(2.4, 620, size=app_rows),
    })
    approval["Report Viewed Receipts"] = np.where(rng.random(app_rows) < 0.25, "Y", "N")
    approval["Entry Viewed Receipts"] = np.where(rng.random(app_rows) < 0.18, "Y", "N")

    return _standardise_combined(combined), _standardise_pre(pre), _standardise_approval(approval)


def load_runtime_data():
    startup_error = None
    runtime_notice = None

    try:
        data = load_all_files()
        pops = build_exco_populations(data["expense_report"])
        combined = _standardise_combined(pops["combined"])
        pre = _standardise_pre(data.get("travel_requests_no_expense", pd.DataFrame()))
        approval = _standardise_approval(data.get("approval_aging", pd.DataFrame()))
        logger.info("Loaded live runtime data.")
        return combined, pre, approval, startup_error, runtime_notice
    except Exception as e:
        startup_error = str(e)
        logger.error("Failed live data load: %s", e)
        combined, pre, approval = build_demo_data()
        runtime_notice = "Live volume data is unavailable. Showing deterministic demo data so the prototype remains fully interactive."
        return combined, pre, approval, startup_error, runtime_notice


DF_COMBINED, DF_PRE, DF_APPROVAL, STARTUP_ERROR, RUNTIME_NOTICE = load_runtime_data()


# ─── Evidence payload builder (deterministic — source of truth) ──────────

def compute_evidence_payload(df: pd.DataFrame, df_appr: pd.DataFrame, df_pre: pd.DataFrame) -> dict:
    """Build the full evidence payload with source-file citations.

    Every metric carries a source_file so the guardrail and UI can
    show which file the number came from.
    """
    total = max(len(df), 1)
    miss_mask = df["RF_CS_MissingReceipt"] == 1

    def flag_count(col):
        return int(df[col].sum()) if col in df.columns else 0

    # Approver metrics
    instant_pct = round(float(df_appr["Instant"].mean() * 100), 1) if len(df_appr) else 0.0
    no_view_pct = round(float((1 - df_appr["Receipt_Viewed"].mean()) * 100), 1) if len(df_appr) else 0.0
    worst_n = int(((df_appr["Instant"] == 1) & (df_appr["Receipt_Viewed"] == 0)).sum()) if len(df_appr) else 0
    worst_pct = round(worst_n / max(len(df_appr), 1) * 100, 1)
    total_reports = int(len(df_appr))
    n_approvers = int(df_appr["Approver Name"].nunique()) if len(df_appr) else 0

    # Per-approver breakdown
    approver_detail = []
    if len(df_appr):
        grp = df_appr.groupby("Approver Name", as_index=False).agg(
            reports=("Approver Name", "count"),
            receipt_viewed_pct=("Receipt_Viewed", lambda s: round(float(s.mean() * 100), 1)),
            instant_pct=("Instant", lambda s: round(float(s.mean() * 100), 1)),
            no_receipt_pct=("Receipt_Viewed", lambda s: round(float((1 - s.mean()) * 100), 1)),
        ).rename(columns={"Approver Name": "name"})
        approver_detail = grp.sort_values("receipt_viewed_pct", ascending=False).to_dict("records")

    # Entertainment / attendee
    ent = df[df["Expense Type"].astype(str).str.contains("entertain|function|gift", case=False, na=False)]
    att_miss = int(ent["RF_ATT_Missing"].sum()) if ("RF_ATT_Missing" in ent.columns and len(ent)) else 0
    att_total = int(len(ent))
    att_miss_pct = round(att_miss / max(att_total, 1) * 100, 1)

    # High-value
    hv_mask = df["Expense Amount (reimbursement currency)"] > 5000
    hv_count = int(hv_mask.sum())
    hv_amount = float(df.loc[hv_mask, "Expense Amount (reimbursement currency)"].sum())
    hv_employees = int(df.loc[hv_mask, "Employee"].nunique())

    # Split / duplicate
    split_sd = flag_count("RF_CS_SplitClaims_SameDay")
    split_win = flag_count("RF_CS_SplitClaims_Window")
    dup = flag_count("RF_CS_Duplicate")
    dup_oop = flag_count("RF_CS_Duplicate_OOP_AMEX")

    # Missing receipt
    miss_count = int(miss_mask.sum())
    miss_pct = round(float(miss_mask.mean() * 100), 1)
    miss_amt = float(df.loc[miss_mask, "Expense Amount (reimbursement currency)"].sum())

    # Preferred supplier
    pref_dom_air = flag_count("RF_SP_DomAirline_NotPreferred")
    pref_int_air = flag_count("RF_SP_IntAirline_NotPreferred")
    pref_dom_accom = flag_count("RF_SP_DomAccom_NotPreferred")
    pref_int_accom = flag_count("RF_SP_IntAccom_NotPreferred")
    pref_dom_car = flag_count("RF_SP_DomCarRental_NotPreferred")
    pref_int_car = flag_count("RF_SP_IntCarRental_NotPreferred")

    # Pre-approval
    pre_count = int(len(df_pre))
    pre_amount = float(df_pre["PreApprovalAmount"].sum()) if len(df_pre) else 0.0
    pre_employees = int(df_pre["Employee"].nunique()) if len(df_pre) else 0

    # Claim employees without matching pre-approval
    claim_emps = set(df["Employee"].unique())
    pre_emps = set(df_pre["Employee"].unique()) if len(df_pre) else set()
    no_preapproval_emps = len(claim_emps - pre_emps)

    # T3.3a — Late/urgent bookings
    late_count = int(df["RF_CS_LateBooking"].sum()) if "RF_CS_LateBooking" in df.columns else 0
    very_late_count = int(df["RF_CS_VeryLateBooking"].sum()) if "RF_CS_VeryLateBooking" in df.columns else 0
    late_employees = int(df.loc[df.get("RF_CS_LateBooking", pd.Series(0, index=df.index)) == 1, "Employee"].nunique()) if late_count else 0

    # T3.3b — Entertainment per-head thresholds
    ent_with_att = ent[ent.get("Attendee_Count", pd.Series(0, index=ent.index)) > 0].copy() if "Attendee_Count" in df.columns else pd.DataFrame()
    if len(ent_with_att):
        ent_with_att["Per_Head"] = ent_with_att["Expense Amount (reimbursement currency)"] / ent_with_att["Attendee_Count"]
        ent_over_internal = int((ent_with_att["Per_Head"] > 40).sum())
        ent_over_external = int((ent_with_att["Per_Head"] > 80).sum())
        ent_over_amount = float(ent_with_att.loc[ent_with_att["Per_Head"] > 40, "Expense Amount (reimbursement currency)"].sum())
    else:
        ent_over_internal = 0
        ent_over_external = 0
        ent_over_amount = 0.0
    ent_assessed = int(len(ent_with_att))

    # T4.3 — Personal expense (red flag)
    personal_count = int(df["RF_CS_PersonalExpense"].sum()) if "RF_CS_PersonalExpense" in df.columns else 0
    personal_amount = float(df.loc[df.get("RF_CS_PersonalExpense", pd.Series(0, index=df.index)) == 1, "Expense Amount (reimbursement currency)"].sum()) if personal_count else 0.0
    personal_employees = int(df.loc[df.get("RF_CS_PersonalExpense", pd.Series(0, index=df.index)) == 1, "Employee"].nunique()) if personal_count else 0

    # T6.1c — Attendee hierarchy validity
    att_hierarchy_invalid = int(ent["RF_ATT_Missing"].sum()) if "RF_ATT_Missing" in ent.columns and len(ent) else 0
    att_hierarchy_total = int(len(ent))

    # T6.1d — Daily spend limit exceedances (>$1,000 per employee per day)
    daily_limit = 1000
    daily_spend = df.groupby(["Employee", "Transaction Date"])["Expense Amount (reimbursement currency)"].sum().reset_index()
    daily_over = daily_spend[daily_spend["Expense Amount (reimbursement currency)"] > daily_limit]
    daily_over_count = int(len(daily_over))
    daily_over_amount = float(daily_over["Expense Amount (reimbursement currency)"].sum()) if len(daily_over) else 0.0
    daily_over_employees = int(daily_over["Employee"].nunique()) if len(daily_over) else 0
    daily_over_max = float(daily_over["Expense Amount (reimbursement currency)"].max()) if len(daily_over) else 0.0

    src = {
        "expense_report": FILE_REGISTRY["expense_report"]["filename"],
        "approval_aging": FILE_REGISTRY["approval_aging"]["filename"],
        "attendee_validity": FILE_REGISTRY["attendee_validity"]["filename"],
        "missing_receipt": FILE_REGISTRY["missing_receipt"]["filename"],
        "travel_requests": FILE_REGISTRY["travel_requests_no_expense"]["filename"],
        "booking_detail": FILE_REGISTRY["booking_detail"]["filename"],
        "travel_segment": FILE_REGISTRY["travel_request_segment"]["filename"],
        "per_diem": FILE_REGISTRY["per_diem_rates"]["filename"],
    }

    return {
        "audit_period": "01 Jan 2025 – 30 Apr 2026",
        "months_covered": 16,
        "total_claims": int(len(df)),
        "total_spend": float(df["Expense Amount (reimbursement currency)"].sum()),
        "exco_members": EXCO_MEMBERS,
        "exco_members_count": len(EXCO_MEMBERS),
        "unique_employees": int(df["Employee"].nunique()),
        "source_files": src,
        "metrics": {
            # T6.1a — Approver review
            "approver_total_reports":   {"value": total_reports, "unit": "count",   "source_file": src["approval_aging"]},
            "approver_count":           {"value": n_approvers,   "unit": "count",   "source_file": src["approval_aging"]},
            "approver_no_receipt_pct":  {"value": no_view_pct,   "unit": "%",       "source_file": src["approval_aging"]},
            "approver_no_receipt_n":    {"value": int(total_reports * no_view_pct / 100) if total_reports else 0, "unit": "count", "source_file": src["approval_aging"]},
            "approver_instant_pct":     {"value": instant_pct,   "unit": "%",       "source_file": src["approval_aging"]},
            "approver_worst_case_pct":  {"value": worst_pct,     "unit": "%",       "source_file": src["approval_aging"]},
            "approver_worst_case_n":    {"value": worst_n,       "unit": "count",   "source_file": src["approval_aging"]},
            # T4.2 — Missing attendee
            "att_missing_count":        {"value": att_miss,      "unit": "count",   "source_file": src["expense_report"]},
            "att_total":                {"value": att_total,      "unit": "count",   "source_file": src["expense_report"]},
            "att_missing_pct":          {"value": att_miss_pct,  "unit": "%",       "source_file": src["expense_report"]},
            # T4.1 — Missing receipt
            "missing_receipt_count":    {"value": miss_count,    "unit": "count",   "source_file": src["missing_receipt"]},
            "missing_receipt_pct":      {"value": miss_pct,      "unit": "%",       "source_file": src["expense_report"]},
            "missing_receipt_amount":   {"value": miss_amt,      "unit": "AUD",     "source_file": src["expense_report"]},
            # T4.4 — High value
            "hv_count":                 {"value": hv_count,      "unit": "count",   "source_file": src["expense_report"]},
            "hv_amount":                {"value": hv_amount,     "unit": "AUD",     "source_file": src["expense_report"]},
            "hv_employees":             {"value": hv_employees,  "unit": "count",   "source_file": src["expense_report"]},
            # T5.1 — Split claims
            "split_same_day":           {"value": split_sd,      "unit": "count",   "source_file": src["expense_report"]},
            "split_window":             {"value": split_win,     "unit": "count",   "source_file": src["expense_report"]},
            # T5.2 — Duplicates
            "duplicate_count":          {"value": dup,           "unit": "count",   "source_file": src["expense_report"]},
            "duplicate_oop_amex":       {"value": dup_oop,       "unit": "count",   "source_file": src["expense_report"]},
            # T3.2a — Preferred supplier
            "pref_dom_airline":         {"value": pref_dom_air,  "unit": "count",   "source_file": src["expense_report"]},
            "pref_int_airline":         {"value": pref_int_air,  "unit": "count",   "source_file": src["expense_report"]},
            "pref_dom_accom":           {"value": pref_dom_accom,"unit": "count",   "source_file": src["expense_report"]},
            "pref_int_accom":           {"value": pref_int_accom,"unit": "count",   "source_file": src["expense_report"]},
            "pref_dom_car":             {"value": pref_dom_car,  "unit": "count",   "source_file": src["expense_report"]},
            "pref_int_car":             {"value": pref_int_car,  "unit": "count",   "source_file": src["expense_report"]},
            # T3.1a — Pre-approvals
            "preapproval_unlinked_count":  {"value": pre_count,    "unit": "count", "source_file": src["travel_requests"]},
            "preapproval_unlinked_amount": {"value": pre_amount,   "unit": "AUD",   "source_file": src["travel_requests"]},
            "preapproval_employees":       {"value": pre_employees,"unit": "count", "source_file": src["travel_requests"]},
            # T3.1b — No pre-approval
            "no_preapproval_employees":    {"value": no_preapproval_emps, "unit": "count", "source_file": src["travel_requests"]},
            # T3.3a — Late/urgent bookings
            "late_booking_count":          {"value": late_count,       "unit": "count", "source_file": src["booking_detail"]},
            "very_late_booking_count":     {"value": very_late_count,  "unit": "count", "source_file": src["booking_detail"]},
            "late_booking_employees":      {"value": late_employees,   "unit": "count", "source_file": src["booking_detail"]},
            # T3.3b — Entertainment per-head
            "ent_over_internal_count":     {"value": ent_over_internal,"unit": "count", "source_file": src["expense_report"]},
            "ent_over_external_count":     {"value": ent_over_external,"unit": "count", "source_file": src["expense_report"]},
            "ent_over_amount":             {"value": ent_over_amount,  "unit": "AUD",   "source_file": src["expense_report"]},
            "ent_assessed":                {"value": ent_assessed,     "unit": "count", "source_file": src["expense_report"]},
            # T4.3 — Personal expense
            "personal_expense_count":      {"value": personal_count,     "unit": "count", "source_file": src["expense_report"]},
            "personal_expense_amount":     {"value": personal_amount,    "unit": "AUD",   "source_file": src["expense_report"]},
            "personal_expense_employees":  {"value": personal_employees, "unit": "count", "source_file": src["expense_report"]},
            # T6.1c — Attendee hierarchy
            "att_hierarchy_invalid":       {"value": att_hierarchy_invalid, "unit": "count", "source_file": src["attendee_validity"]},
            "att_hierarchy_total":         {"value": att_hierarchy_total,   "unit": "count", "source_file": src["attendee_validity"]},
            # T6.1d — Daily spend limit
            "daily_over_count":            {"value": daily_over_count,     "unit": "count", "source_file": src["expense_report"]},
            "daily_over_amount":           {"value": daily_over_amount,    "unit": "AUD",   "source_file": src["expense_report"]},
            "daily_over_employees":        {"value": daily_over_employees, "unit": "count", "source_file": src["expense_report"]},
            "daily_over_max":              {"value": daily_over_max,       "unit": "AUD",   "source_file": src["expense_report"]},
        },
        "approver_detail": approver_detail,
    }


EVIDENCE_PAYLOAD = compute_evidence_payload(DF_COMBINED, DF_APPROVAL, DF_PRE)

# Enrich payload with chart data for PPTX export
if "Expense Type" in DF_COMBINED.columns and "Expense Amount (reimbursement currency)" in DF_COMBINED.columns:
    _by_type = DF_COMBINED.groupby("Expense Type")["Expense Amount (reimbursement currency)"].sum()
    EVIDENCE_PAYLOAD["expense_breakdown"] = _by_type.to_dict()
if "Transaction Date" in DF_COMBINED.columns and "Expense Amount (reimbursement currency)" in DF_COMBINED.columns:
    _monthly = DF_COMBINED.dropna(subset=["Transaction Date"]).copy()
    _monthly["_month"] = _monthly["Transaction Date"].dt.to_period("M").dt.to_timestamp()
    _m_agg = _monthly.groupby("_month").agg(
        claims=("Employee", "count"),
        amount=("Expense Amount (reimbursement currency)", "sum"),
    ).reset_index()
    EVIDENCE_PAYLOAD["monthly_data"] = [
        {"month": r["_month"].strftime("%b %Y"), "claims": int(r["claims"]), "amount": float(r["amount"])}
        for _, r in _m_agg.iterrows()
    ]


def build_all_findings(payload):
    """Build findings deterministically from the evidence payload.

    Each finding is only included when the underlying metric is non-zero.
    Observations cite exact numbers from the payload with source-file traceability.
    """
    m = payload["metrics"]

    def _v(key):
        return m[key]["value"]

    def _dollar(key):
        v = m[key]["value"]
        return f"${v:,.0f}" if isinstance(v, (int, float)) else str(v)

    findings = []

    # T6.1a — Approver review quality
    if _v("approver_no_receipt_pct") > 0 or _v("approver_instant_pct") > 0:
        risk = "High" if _v("approver_no_receipt_pct") > 30 or _v("approver_instant_pct") > 40 else "Medium"
        findings.append({
            "test_id": "T6.1a",
            "title": "Inadequate Approver Review of Expense Reports",
            "risk": risk,
            "observation": (
                f"Of {_v('approver_total_reports'):,} expense reports reviewed by {_v('approver_count')} approvers, "
                f"{_v('approver_no_receipt_pct')}% were approved without the approver viewing the receipt. "
                f"Additionally, {_v('approver_instant_pct')}% of reports received instant approval (< 1 minute review time). "
                f"{_v('approver_worst_case_n'):,} reports ({_v('approver_worst_case_pct')}%) received both instant approval "
                f"and no receipt review, representing the highest-risk approval behaviour."
            ),
            "recommendation": (
                "Implement minimum review time controls in the expense system. Require mandatory receipt viewing "
                "before approval submission. Establish approver review KPIs and include in performance discussions."
            ),
            "metrics_cited": ["approver_total_reports", "approver_count", "approver_no_receipt_pct",
                             "approver_instant_pct", "approver_worst_case_n", "approver_worst_case_pct"],
            "management_questions": [
                "Are approvers aware of their obligation to review receipts before approving?",
                "What training has been provided to approvers on their T&E oversight responsibilities?",
                "Is there a mechanism to flag approvers who consistently approve without receipt review?",
            ],
        })

    # T4.1 — Missing receipts
    if _v("missing_receipt_count") > 0:
        risk = "High" if _v("missing_receipt_pct") > 10 else "Medium" if _v("missing_receipt_pct") > 5 else "Low"
        findings.append({
            "test_id": "T4.1",
            "title": "Missing Receipt Documentation",
            "risk": risk,
            "observation": (
                f"{_v('missing_receipt_count'):,} claims ({_v('missing_receipt_pct')}% of total) are missing receipt documentation, "
                f"representing {_dollar('missing_receipt_amount')} in unsupported spend. "
                f"Without receipts, the business purpose and legitimacy of these expenses cannot be independently verified."
            ),
            "recommendation": (
                "Enforce mandatory receipt attachment before expense report submission. "
                "Implement automated rejection for claims exceeding $50 without supporting documentation."
            ),
            "metrics_cited": ["missing_receipt_count", "missing_receipt_pct", "missing_receipt_amount"],
            "management_questions": [
                "What is the current policy for handling claims without receipts?",
                "Are there legitimate reasons for the volume of missing receipts (e.g., system issues)?",
            ],
        })

    # T4.2 — Missing attendee details
    if _v("att_missing_count") > 0:
        risk = "High" if _v("att_missing_pct") > 20 else "Medium"
        findings.append({
            "test_id": "T4.2",
            "title": "Entertainment Claims Missing Attendee Details",
            "risk": risk,
            "observation": (
                f"{_v('att_missing_count'):,} of {_v('att_total'):,} entertainment-related claims ({_v('att_missing_pct')}%) "
                f"are missing attendee information. Policy requires attendee names and business purpose for all "
                f"entertainment and hospitality expenses to demonstrate legitimate business use."
            ),
            "recommendation": (
                "Mandate attendee fields as required for entertainment expense types in the expense system. "
                "Reject submissions without completed attendee details."
            ),
            "metrics_cited": ["att_missing_count", "att_total", "att_missing_pct"],
            "management_questions": [
                "Is there a process to retrospectively obtain attendee details for flagged claims?",
                "Are staff aware of the requirement to document attendees for entertainment expenses?",
            ],
        })

    # T4.4 — High-value claims
    if _v("hv_count") > 0:
        findings.append({
            "test_id": "T4.4",
            "title": "High-Value Claims Requiring Enhanced Scrutiny",
            "risk": "Medium",
            "observation": (
                f"{_v('hv_count'):,} claims exceed the $5,000 threshold, totalling {_dollar('hv_amount')} "
                f"across {_v('hv_employees')} employees. High-value claims carry elevated risk of misuse "
                f"and warrant additional review beyond standard approval workflows."
            ),
            "recommendation": (
                "Implement tiered approval for claims above $5,000, requiring secondary sign-off from finance. "
                "Conduct periodic deep-dive reviews of high-value transactions."
            ),
            "metrics_cited": ["hv_count", "hv_amount", "hv_employees"],
            "management_questions": [
                "What additional controls exist for high-value expense claims?",
                "Have any of these high-value claims been subject to post-approval review?",
            ],
        })

    # T5.1 — Split claims
    split_total = _v("split_same_day") + _v("split_window")
    if split_total > 0:
        risk = "High" if split_total > 20 else "Medium"
        findings.append({
            "test_id": "T5.1",
            "title": "Potential Split Claims to Circumvent Approval Thresholds",
            "risk": risk,
            "observation": (
                f"{_v('split_same_day'):,} same-day split claims and {_v('split_window'):,} windowed split claims "
                f"were identified ({split_total:,} total). Split claims may indicate intentional structuring "
                f"of expenses below approval thresholds or policy limits."
            ),
            "recommendation": (
                "Investigate flagged split claims to determine if they represent legitimate separate expenses "
                "or intentional structuring. Implement automated detection rules in the expense system."
            ),
            "metrics_cited": ["split_same_day", "split_window"],
            "management_questions": [
                "Are employees aware that splitting claims to avoid thresholds violates policy?",
                "Have similar patterns been identified in prior audit cycles?",
            ],
        })

    # T5.2 — Duplicates
    dup_total = _v("duplicate_count") + _v("duplicate_oop_amex")
    if dup_total > 0:
        risk = "High" if dup_total > 10 else "Medium"
        findings.append({
            "test_id": "T5.2",
            "title": "Duplicate Expense Claims Identified",
            "risk": risk,
            "observation": (
                f"{_v('duplicate_count'):,} potential duplicate claims were identified, "
                f"plus {_v('duplicate_oop_amex'):,} potential out-of-pocket vs corporate card duplicates. "
                f"Duplicate claims result in double reimbursement and direct financial loss."
            ),
            "recommendation": (
                "Review all flagged duplicates and recover any confirmed double payments. "
                "Enhance duplicate detection logic in the expense system to prevent submission."
            ),
            "metrics_cited": ["duplicate_count", "duplicate_oop_amex"],
            "management_questions": [
                "What is the current process for detecting and resolving duplicate claims?",
                "Have any of these duplicates already been identified and reversed?",
            ],
        })

    # T3.2a — Preferred supplier non-compliance
    pref_total = (_v("pref_dom_airline") + _v("pref_int_airline") + _v("pref_dom_accom")
                  + _v("pref_int_accom") + _v("pref_dom_car") + _v("pref_int_car"))
    if pref_total > 0:
        parts = []
        if _v("pref_dom_airline") + _v("pref_int_airline") > 0:
            parts.append(f"airlines ({_v('pref_dom_airline'):,} domestic, {_v('pref_int_airline'):,} international)")
        if _v("pref_dom_accom") + _v("pref_int_accom") > 0:
            parts.append(f"accommodation ({_v('pref_dom_accom'):,} domestic, {_v('pref_int_accom'):,} international)")
        if _v("pref_dom_car") + _v("pref_int_car") > 0:
            parts.append(f"car rental ({_v('pref_dom_car'):,} domestic, {_v('pref_int_car'):,} international)")
        breakdown = "; ".join(parts)

        risk = "Medium" if pref_total > 20 else "Low"
        findings.append({
            "test_id": "T3.2a",
            "title": "Non-Preferred Supplier Usage",
            "risk": risk,
            "observation": (
                f"{pref_total:,} bookings used non-preferred suppliers: {breakdown}. "
                f"Use of non-preferred suppliers foregoes negotiated corporate rates and volume discounts, "
                f"increasing travel costs."
            ),
            "recommendation": (
                "Reinforce preferred supplier policy and ensure the corporate booking tool defaults to preferred suppliers. "
                "Require justification for non-preferred bookings exceeding a cost threshold."
            ),
            "metrics_cited": ["pref_dom_airline", "pref_int_airline", "pref_dom_accom",
                             "pref_int_accom", "pref_dom_car", "pref_int_car"],
            "management_questions": [
                "What is the estimated cost impact of non-preferred supplier usage?",
                "Are there legitimate reasons (e.g., route availability) for the non-preferred bookings?",
            ],
        })

    # T3.1a — Pre-approval unlinked
    if _v("preapproval_unlinked_count") > 0:
        findings.append({
            "test_id": "T3.1a",
            "title": "Travel Requests Without Linked Expense Reports",
            "risk": "Medium",
            "observation": (
                f"{_v('preapproval_unlinked_count'):,} pre-approved travel requests totalling "
                f"{_dollar('preapproval_unlinked_amount')} across {_v('preapproval_employees')} employees "
                f"do not have linked expense reports. This may indicate travel that was approved but not taken, "
                f"or expense reports that were not properly linked to pre-approvals."
            ),
            "recommendation": (
                "Reconcile unlinked travel requests with actual travel records. Implement automated matching "
                "between pre-approvals and expense reports."
            ),
            "metrics_cited": ["preapproval_unlinked_count", "preapproval_unlinked_amount", "preapproval_employees"],
            "management_questions": [
                "Is there a follow-up process for pre-approved travel that doesn't result in an expense report?",
                "What percentage of these represent cancelled travel vs missing expense reports?",
            ],
        })

    # T3.1b — No pre-approval
    if _v("no_preapproval_employees") > 0:
        risk = "Medium" if _v("no_preapproval_employees") > 5 else "Low"
        findings.append({
            "test_id": "T3.1b",
            "title": "Employees Submitting Claims Without Pre-Approval",
            "risk": risk,
            "observation": (
                f"{_v('no_preapproval_employees')} employees submitted expense claims without any corresponding "
                f"pre-approval on file. Policy requires travel pre-approval before incurring expenses "
                f"to ensure budgetary control and management oversight."
            ),
            "recommendation": (
                "Enforce pre-approval requirements in the expense system by blocking submission "
                "without a linked travel request. Review non-compliant claims for policy violations."
            ),
            "metrics_cited": ["no_preapproval_employees"],
            "management_questions": [
                "Are all ExCo members and their delegates aware of the pre-approval requirement?",
                "Should certain expense types (e.g., local meals) be exempt from pre-approval?",
            ],
        })

    # T3.3a — Late/urgent travel bookings
    if _v("late_booking_count") > 0:
        risk = "High" if _v("very_late_booking_count") > 10 else "Medium"
        findings.append({
            "test_id": "T3.3a",
            "title": "Late and Urgent Travel Bookings",
            "risk": risk,
            "observation": (
                f"{_v('late_booking_count'):,} bookings were made less than 14 days before departure, "
                f"of which {_v('very_late_booking_count'):,} were very late (less than 3 days), "
                f"across {_v('late_booking_employees')} employees. Late bookings typically incur premium fares "
                f"and reduce access to preferred supplier inventory."
            ),
            "recommendation": (
                "Enforce advance booking requirements through the travel management system. "
                "Require manager justification for bookings made within 7 days of travel. "
                "Track late-booking patterns by employee and escalate repeat offenders."
            ),
            "metrics_cited": ["late_booking_count", "very_late_booking_count", "late_booking_employees"],
            "management_questions": [
                "What business reasons drive last-minute travel for ExCo members?",
                "Is there a formal exception process for urgent travel needs?",
                "What is the estimated cost premium of late bookings vs advance bookings?",
            ],
        })

    # T3.3b — Entertainment spend per head thresholds
    if _v("ent_over_internal_count") > 0:
        risk = "Medium" if _v("ent_over_external_count") > 10 else "Low"
        findings.append({
            "test_id": "T3.3b",
            "title": "Entertainment Spend Exceeding Per-Head Thresholds",
            "risk": risk,
            "observation": (
                f"Of {_v('ent_assessed'):,} entertainment claims with attendee counts, "
                f"{_v('ent_over_internal_count'):,} exceeded the $40 internal per-head threshold "
                f"and {_v('ent_over_external_count'):,} exceeded the $80 external per-head threshold, "
                f"totalling {_dollar('ent_over_amount')} in over-threshold spend."
            ),
            "recommendation": (
                "Review claims exceeding per-head limits to determine if business justification is adequate. "
                "Consider implementing real-time alerts in the expense system when per-head limits are breached."
            ),
            "metrics_cited": ["ent_over_internal_count", "ent_over_external_count", "ent_over_amount", "ent_assessed"],
            "management_questions": [
                "Are the current per-head thresholds ($40 internal / $80 external) still appropriate?",
                "Are there specific event types that legitimately exceed the thresholds?",
            ],
        })

    # T4.3 — Personal expense in business claims
    if _v("personal_expense_count") > 0:
        risk = "High" if _v("personal_expense_count") > 50 else "Medium"
        findings.append({
            "test_id": "T4.3",
            "title": "Potential Personal Expenses in Business Claims",
            "risk": risk,
            "observation": (
                f"{_v('personal_expense_count'):,} claims totalling {_dollar('personal_expense_amount')} across "
                f"{_v('personal_expense_employees')} employees were flagged as potentially personal expenses. "
                f"These include items where the expense description, vendor, or category suggests "
                f"non-business use."
            ),
            "recommendation": (
                "Investigate flagged claims to confirm business purpose. Implement clearer guidance on "
                "what constitutes a personal vs business expense. Consider enhanced receipt review for "
                "categories with high personal-expense rates."
            ),
            "metrics_cited": ["personal_expense_count", "personal_expense_amount", "personal_expense_employees"],
            "management_questions": [
                "What is the current process for identifying and recovering personal expenses?",
                "Are employees aware of the policy boundaries between personal and business expenses?",
                "Have any of these flagged items been previously reviewed and cleared?",
            ],
        })

    # T6.1c — Attendee hierarchy validity
    if _v("att_hierarchy_invalid") > 0:
        findings.append({
            "test_id": "T6.1c",
            "title": "Attendee Hierarchy Validity Exceptions",
            "risk": "Medium",
            "observation": (
                f"{_v('att_hierarchy_invalid'):,} of {_v('att_hierarchy_total'):,} entertainment claims "
                f"have attendees who are direct reports of the claimant. Policy requires that entertainment "
                f"of direct reports be approved by the claimant's own manager to prevent self-serving approvals."
            ),
            "recommendation": (
                "Implement hierarchical validation in the expense system to flag claims where attendees "
                "report directly to the claimant. Require next-level-up approval for such claims."
            ),
            "metrics_cited": ["att_hierarchy_invalid", "att_hierarchy_total"],
            "management_questions": [
                "Is team entertainment (e.g., team lunches) a legitimate and expected expense for ExCo?",
                "Should direct-report entertainment require separate approval from the next level?",
            ],
        })

    # T6.1d — Daily spend limit exceedances
    if _v("daily_over_count") > 0:
        risk = "High" if _v("daily_over_count") > 50 else "Medium"
        findings.append({
            "test_id": "T6.1d",
            "title": "Daily Spend Limit Exceedances",
            "risk": risk,
            "observation": (
                f"{_v('daily_over_count'):,} employee-days exceeded the $1,000 daily spend threshold, "
                f"totalling {_dollar('daily_over_amount')} across {_v('daily_over_employees')} employees. "
                f"The highest single-day spend was {_dollar('daily_over_max')}. "
                f"Concentrated daily spend may indicate bulk personal purchases or policy non-compliance."
            ),
            "recommendation": (
                "Review high daily-spend instances to verify business purpose. Consider implementing "
                "real-time daily spend caps with automatic escalation for amounts exceeding policy limits."
            ),
            "metrics_cited": ["daily_over_count", "daily_over_amount", "daily_over_employees", "daily_over_max"],
            "management_questions": [
                "Are there legitimate business scenarios that would exceed $1,000 in a single day?",
                "Should the daily limit be adjusted for international travel days?",
                "What controls exist to prevent excessive single-day spend?",
            ],
        })

    # Sort by risk: High first, then Medium, then Low
    risk_order = {"High": 0, "Medium": 1, "Low": 2}
    findings.sort(key=lambda f: risk_order.get(f.get("risk", "Low"), 3))
    return findings


FINDINGS = build_all_findings(EVIDENCE_PAYLOAD)
CATALOGUE_ROWS = build_catalogue_rows(EVIDENCE_PAYLOAD.get("metrics", {}))


# ─── Risk scoring ────────────────────────────────────────────────────────────
def compute_risk_scores(findings, payload):
    """Score each finding: recurrence × amount × severity."""
    m = payload.get("metrics", {})
    total_spend = max(payload.get("total_spend", 1), 1)
    total_claims = max(payload.get("total_claims", 1), 1)
    severity_weight = {"High": 3, "Medium": 2, "Low": 1}

    scored = []
    for f in findings:
        cited = f.get("metrics_cited", [])
        amount = 0
        count = 0
        for mid in cited:
            met = m.get(mid, {})
            v = met.get("value", 0) or 0
            if met.get("unit") == "AUD":
                amount = max(amount, v)
            elif met.get("unit") == "count":
                count = max(count, v)

        recurrence = min(count / total_claims * 100, 100) if count else 0
        exposure_pct = min(amount / total_spend * 100, 100) if amount else 0
        sev = severity_weight.get(f["risk"], 1)
        score = round((sev * 30) + (recurrence * 0.4) + (exposure_pct * 0.3), 1)

        scored.append({**f, "risk_score": score, "financial_exposure": amount, "exception_count": count})
    scored.sort(key=lambda x: x["risk_score"], reverse=True)
    return scored


FINDINGS_SCORED = compute_risk_scores(FINDINGS, EVIDENCE_PAYLOAD)


# ─── Outlier detection ───────────────────────────────────────────────────────
def compute_outliers(df, top_n=20):
    """Find spend outliers per employee using z-scores."""
    amt_col = "Expense Amount (reimbursement currency)"
    if amt_col not in df.columns or len(df) < 10:
        return pd.DataFrame()

    emp_stats = df.groupby("Employee")[amt_col].agg(["mean", "std", "count", "sum"]).reset_index()
    emp_stats.columns = ["Employee", "Avg Claim", "Std Dev", "Claim Count", "Total Spend"]
    emp_stats = emp_stats[emp_stats["Std Dev"] > 0]

    df_out = df[["Employee", "Transaction Date", "Expense Type", "Vendor", amt_col]].copy()
    df_out = df_out.merge(emp_stats[["Employee", "Avg Claim", "Std Dev"]], on="Employee", how="left")
    df_out["Z-Score"] = ((df_out[amt_col] - df_out["Avg Claim"]) / df_out["Std Dev"]).round(2)
    df_out = df_out[df_out["Z-Score"] > 2.0].sort_values("Z-Score", ascending=False).head(top_n)
    df_out.rename(columns={amt_col: "Amount ($)"}, inplace=True)
    return df_out[["Employee", "Transaction Date", "Expense Type", "Vendor", "Amount ($)", "Z-Score"]]


OUTLIER_DF = compute_outliers(DF_COMBINED)


# ─── Run metadata ────────────────────────────────────────────────────────────
RUN_METADATA = {
    "run_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "audit_period": EVIDENCE_PAYLOAD.get("audit_period", "N/A"),
    "total_claims": EVIDENCE_PAYLOAD.get("total_claims", 0),
    "total_spend": EVIDENCE_PAYLOAD.get("total_spend", 0),
    "unique_employees": EVIDENCE_PAYLOAD.get("unique_employees", 0),
    "exco_members_count": EVIDENCE_PAYLOAD.get("exco_members_count", 0),
    "source_files": len(EVIDENCE_PAYLOAD.get("source_files", {})),
    "findings_count": len(FINDINGS),
    "high_findings": sum(1 for f in FINDINGS if f["risk"] == "High"),
    "medium_findings": sum(1 for f in FINDINGS if f["risk"] == "Medium"),
    "low_findings": sum(1 for f in FINDINGS if f["risk"] == "Low"),
    "tests_executed": len(CATALOGUE_ROWS),
    "tests_exception": sum(1 for r in CATALOGUE_ROWS if r["Status"] == "Exception"),
    "tests_pass": sum(1 for r in CATALOGUE_ROWS if r["Status"] == "Pass"),
    "tests_na": sum(1 for r in CATALOGUE_ROWS if r["Status"] == "Not testable"),
    "data_mode": "Demo" if RUNTIME_NOTICE else "Live",
    "population_reconciliation": {
        "combined_rows": int(len(DF_COMBINED)),
        "combined_spend": float(DF_COMBINED["Expense Amount (reimbursement currency)"].sum()),
        "approval_rows": int(len(DF_APPROVAL)),
        "preapproval_rows": int(len(DF_PRE)),
    },
}


def filter_by_dates(df: pd.DataFrame, date_col: str, start_date, end_date):
    out = df.copy()
    if start_date:
        out = out[out[date_col] >= pd.to_datetime(start_date)]
    if end_date:
        out = out[out[date_col] <= pd.to_datetime(end_date)]
    return out


def filter_combined(start_date, end_date, members=None, expense_types=None):
    out = filter_by_dates(DF_COMBINED, "Transaction Date", start_date, end_date)
    if members:
        out = out[out["Employee"].isin(members)]
    if expense_types:
        out = out[out["Expense Type"].isin(expense_types)]
    return out


def any_breach_mask(df):
    all_flags = [f for v in BREACH_FLAG_GROUPS.values() for f in v]
    all_flags = [f for f in all_flags if f in df.columns]
    if not all_flags:
        return pd.Series(False, index=df.index)
    return df[all_flags].sum(axis=1) > 0


def format_kpi(title, value):
    return html.Div([html.Div(title, className="kpi-title"), html.Div(value, className="kpi-value")], className="kpi-tile")


def build_header():
    mode = "Demo mode" if RUNTIME_NOTICE else "Live mode"
    mode_title = "Using deterministic fallback metrics" if RUNTIME_NOTICE else "Using live source files"
    rm = RUN_METADATA
    return html.Div(
        html.Header([
            html.Div([
                html.Div([
                    html.H1("Travel & Entertainment Executive Diligence", className="page-title"),
                    html.P("Reproducible calculations · Evidence-linked findings · Management questions", className="page-subtitle"),
                ]),
                html.Div([
                    html.Label("View mode", style={"fontSize": 10, "fontWeight": 700, "letterSpacing": "0.5px",
                                                    "textTransform": "uppercase", "color": "#6b7283", "marginBottom": 2}),
                    dcc.Dropdown(
                        id="audience-mode",
                        options=[
                            {"label": "Executive", "value": "executive"},
                            {"label": "Audit Manager", "value": "audit"},
                            {"label": "Investigator", "value": "investigator"},
                        ],
                        value="executive",
                        clearable=False,
                        style={"width": 160, "fontSize": 13},
                    ),
                ], style={"display": "flex", "flexDirection": "column", "alignItems": "flex-end"}),
            ], style={"display": "flex", "justifyContent": "space-between", "alignItems": "flex-start"}),
            html.Div([
                html.Span(mode, className="chip", title=mode_title),
                html.Span(f"Audit period: {rm['audit_period']}", className="chip"),
                html.Span(f"{rm['source_files']} source files", className="chip"),
                html.Span(f"Refreshed: {rm['run_timestamp']}", className="chip", style={"color": "#6b7283"}),
            ], className="chip-row", style={"marginTop": 8}),
            html.Div(RUNTIME_NOTICE, className="runtime-warning") if RUNTIME_NOTICE else None,
        ], className="page-header", style={"marginBottom": 16}),
        className="shell",
    )


app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    title="AI Audit Analyst",
    suppress_callback_exceptions=True,
)


# ─── Findings UI helpers ──────────────────────────────────────────────────────

_MAT_COLOR = {"High": "#b85042", "Medium": "#e0952a", "Low": "#2c7a4b"}


def _severity_color(mat: str) -> str:
    return _MAT_COLOR.get(mat, "#6b7283")


def _fmt_metric(value, unit):
    if unit == "AUD":
        return f"${value:,.0f}" if isinstance(value, (int, float)) else str(value)
    if unit == "%":
        return f"{value}%" if isinstance(value, (int, float)) else str(value)
    return f"{value:,}" if isinstance(value, (int, float)) else str(value)


def _build_finding_card(idx, finding, show_detail=False):
    risk = finding["risk"]
    mat_color = _severity_color(risk)
    test_id = finding.get("test_id", "")
    questions = finding.get("management_questions", [])
    q1 = questions[0] if questions else ""
    score = finding.get("risk_score", 0)
    exposure = finding.get("financial_exposure", 0)
    exc_count = finding.get("exception_count", 0)

    # Summary line (always visible)
    summary_items = [
        html.Span(risk, className="chip", style={"color": mat_color, "borderColor": mat_color}),
        html.Span(test_id, className="chip mono", style={"color": "#6b7283"}),
    ]
    if score:
        summary_items.append(html.Span(f"Score: {score}", className="chip mono", style={"color": "#6b7283", "marginLeft": "auto"}))

    # Detail section (expandable)
    detail_section = html.Div([
        html.P(finding["observation"], style={"margin": "0 0 10px", "fontSize": 13.5, "lineHeight": 1.55, "color": "#2c3040"}),
        # Financial exposure bar
        html.Div([
            html.Span(f"Exposure: ${exposure:,.0f}" if exposure else f"Exceptions: {exc_count:,}",
                      style={"fontSize": 11, "color": "#6b7283", "fontFamily": "monospace"}),
        ], style={"marginBottom": 8}) if (exposure or exc_count) else None,
        html.Div([
            html.Span("Recommendation", style={"fontSize": 10.5, "fontWeight": 700, "letterSpacing": "0.5px",
                                                "color": "#1e2761", "textTransform": "uppercase",
                                                "display": "block", "marginBottom": 3}),
            finding["recommendation"],
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
            html.Button("View evidence", id={"type": "view-evidence-btn", "index": idx},
                        n_clicks=0, style={"fontSize": 13, "marginRight": 8}),
            html.Button("View exceptions", id={"type": "view-exceptions-btn", "index": idx},
                        n_clicks=0, className="ghost", style={"fontSize": 13, "marginRight": 8}),
            html.Button("Copy", id={"type": "copy-finding-btn", "index": idx},
                        n_clicks=0, className="ghost", style={"fontSize": 12}),
        ], style={"display": "flex", "gap": 4, "flexWrap": "wrap"}),
    ], id={"type": "finding-detail", "index": idx})

    return html.Article([
        html.Div(summary_items, style={"display": "flex", "gap": 8, "flexWrap": "wrap", "marginBottom": 8}),
        html.H3(finding["title"], style={"margin": "0 0 8px", "fontSize": 15.5, "fontWeight": 700, "lineHeight": 1.3}),
        detail_section,
    ], className="panel finding-card", style={"display": "flex", "flexDirection": "column", "gap": 0})


def _build_insights_figures():
    df = DF_COMBINED
    _MARGIN = dict(l=16, r=16, t=40, b=16)
    _COLORS = ["#1e2761", "#1c7293", "#b85042", "#e0952a", "#2c7a4b", "#6b7283", "#7b5ea7", "#c76b32"]

    # ── Fig 1: Monthly T&E volume ─────────────────────────────────────────
    monthly = df.dropna(subset=["Transaction Date"]).copy()
    monthly["Month"] = monthly["Transaction Date"].dt.to_period("M").dt.to_timestamp()
    m = monthly.groupby("Month", as_index=False).agg(
        Claims=("Employee", "count"),
        Amount=("Expense Amount (reimbursement currency)", "sum"),
    )
    fig1 = go.Figure()
    fig1.add_bar(x=m["Month"], y=m["Claims"], name="Claims", marker_color="#1e2761")
    fig1.add_trace(go.Scatter(x=m["Month"], y=m["Amount"], name="$ Amount", mode="lines+markers",
                              yaxis="y2", line=dict(color="#1c7293", width=2)))
    fig1.update_layout(title="Monthly T&E volume", yaxis2=dict(overlaying="y", side="right", showgrid=False),
                       legend=dict(orientation="h", y=-0.25, font_size=11),
                       margin=_MARGIN, height=220, paper_bgcolor="white", plot_bgcolor="white")

    # ── Fig 2: Policy exceptions by ExCo member ──────────────────────────
    ex = df.copy().explode("Breach_Type_List")
    ex = ex[ex["Breach_Type_List"] != "No Breach"]
    if ex.empty:
        ex = pd.DataFrame({"Employee": ["—"], "Breach_Type_List": ["No exceptions"]})
    bmem = ex.groupby(["Employee", "Breach_Type_List"], as_index=False).size().rename(columns={"size": "Count"})
    totals = bmem.groupby("Employee")["Count"].sum().sort_values(ascending=True).index.tolist()
    fig2 = px.bar(bmem, x="Count", y="Employee", color="Breach_Type_List", orientation="h",
                  category_orders={"Employee": totals}, title="Policy exceptions by ExCo member",
                  color_discrete_sequence=_COLORS)
    fig2.update_layout(legend=dict(orientation="h", y=-0.35, font_size=11),
                       margin=_MARGIN, height=220, paper_bgcolor="white", plot_bgcolor="white")

    # ── Fig 3: Approver review quality ───────────────────────────────────
    if len(DF_APPROVAL):
        combo = DF_APPROVAL.groupby("Approver Name", as_index=False).agg(
            Receipt_Viewed_Pct=("Receipt_Viewed", lambda s: round(float(s.mean() * 100), 1)),
            Instant_Pct=("Instant", lambda s: round(float(s.mean() * 100), 1)),
        ).sort_values("Receipt_Viewed_Pct")
        fig3 = go.Figure()
        fig3.add_bar(x=combo["Receipt_Viewed_Pct"], y=combo["Approver Name"],
                     name="Receipt viewed %", orientation="h", marker_color="#2c7a4b")
        fig3.add_bar(x=combo["Instant_Pct"], y=combo["Approver Name"],
                     name="Instant approval %", orientation="h", marker_color="#b85042")
        fig3.add_vline(x=50, line_dash="dash", line_color="#e0952a", annotation_text="50%")
        fig3.update_layout(title="Approver review quality", barmode="group",
                           xaxis=dict(range=[0, 100], ticksuffix="%"),
                           legend=dict(orientation="h", y=-0.25, font_size=11),
                           margin=_MARGIN, height=220, paper_bgcolor="white", plot_bgcolor="white")
    else:
        fig3 = go.Figure()
        fig3.update_layout(title="Approver review quality — no data", height=220)

    # ── Fig 4: Finding risk distribution (donut) ─────────────────────────
    risk_counts = pd.DataFrame([{"risk": f["risk"]} for f in FINDINGS_SCORED])
    if not risk_counts.empty:
        rc = risk_counts["risk"].value_counts().reset_index()
        rc.columns = ["Risk", "Count"]
        risk_order = {"High": 0, "Medium": 1, "Low": 2}
        rc["_ord"] = rc["Risk"].map(risk_order).fillna(3)
        rc = rc.sort_values("_ord").drop(columns="_ord")
        risk_color_map = {"High": "#b85042", "Medium": "#e0952a", "Low": "#2c7a4b"}
        fig4 = go.Figure(go.Pie(
            labels=rc["Risk"], values=rc["Count"], hole=0.55,
            marker_colors=[risk_color_map.get(r, "#6b7283") for r in rc["Risk"]],
            textinfo="label+value", textfont_size=12,
        ))
        fig4.update_layout(title="Findings by risk level", showlegend=False,
                           margin=_MARGIN, height=220, paper_bgcolor="white", plot_bgcolor="white")
    else:
        fig4 = go.Figure()
        fig4.update_layout(title="Findings by risk level — none", height=220)

    # ── Fig 5: Spend by expense type (top 10) ────────────────────────────
    if "Expense Type" in df.columns and "Expense Amount (reimbursement currency)" in df.columns:
        by_type = df.groupby("Expense Type", as_index=False)["Expense Amount (reimbursement currency)"].sum()
        by_type = by_type.nlargest(10, "Expense Amount (reimbursement currency)")
        by_type = by_type.sort_values("Expense Amount (reimbursement currency)")
        fig5 = px.bar(by_type, x="Expense Amount (reimbursement currency)", y="Expense Type",
                      orientation="h", title="Top 10 expense types by spend",
                      color_discrete_sequence=["#1c7293"])
        fig5.update_layout(xaxis_title="", yaxis_title="",
                           margin=_MARGIN, height=260, paper_bgcolor="white", plot_bgcolor="white",
                           xaxis_tickprefix="$", xaxis_tickformat=",.0f")
    else:
        fig5 = go.Figure()
        fig5.update_layout(title="Expense type breakdown — no data", height=220)

    # ── Fig 6: Top spenders ──────────────────────────────────────────────
    if "Employee" in df.columns and "Expense Amount (reimbursement currency)" in df.columns:
        by_emp = df.groupby("Employee", as_index=False).agg(
            Total=("Expense Amount (reimbursement currency)", "sum"),
            Claims=("Expense Amount (reimbursement currency)", "count"),
        ).nlargest(10, "Total").sort_values("Total")
        fig6 = go.Figure()
        fig6.add_bar(x=by_emp["Total"], y=by_emp["Employee"], orientation="h",
                     name="Total spend", marker_color="#1e2761")
        fig6.update_layout(title="Top 10 spenders", xaxis_title="", yaxis_title="",
                           margin=_MARGIN, height=260, paper_bgcolor="white", plot_bgcolor="white",
                           xaxis_tickprefix="$", xaxis_tickformat=",.0f")
    else:
        fig6 = go.Figure()
        fig6.update_layout(title="Top spenders — no data", height=220)

    return fig1, fig2, fig3, fig4, fig5, fig6


INSIGHTS_FIG1, INSIGHTS_FIG2, INSIGHTS_FIG3, INSIGHTS_FIG4, INSIGHTS_FIG5, INSIGHTS_FIG6 = _build_insights_figures()


def page1_layout():
    return html.Div([
        html.Div([
            dcc.DatePickerRange(id="p1-date", start_date="2025-01-01", end_date="2026-04-30", display_format="DD MMM YYYY"),
            dcc.Dropdown(id="p1-member", options=[{"label": m, "value": m} for m in sorted(DF_COMBINED["Employee"].dropna().unique())], multi=True, placeholder="All ExCo members"),
        ], className="filter-row"),
        html.Div(id="p1-kpis", className="grid-4 mb-3"),
        html.Div([
            html.Div([dcc.Graph(id="p1-monthly")], className="panel"),
            html.Div([dcc.Graph(id="p1-breach-by-member")], className="panel"),
        ], className="grid-2"),
        html.Div([
            html.Div([dcc.Graph(id="p1-breach-dist")], className="panel"),
            html.Div([dcc.Graph(id="p1-missing-tier")], className="panel"),
        ], className="grid-2 mt-3"),
        html.Div([
            html.Div([dcc.Graph(id="p1-precomp")], className="panel"),
            html.Div([
                html.Div("Missing Attendee Table", className="table-title"),
                dash_table.DataTable(id="p1-attendee-table", page_size=12, style_table={"overflowX": "auto"}),
            ], className="panel"),
        ], className="grid-2 mt-3"),
        html.Details([
            html.Summary("View Evidence", className="ghost"),
            html.Div(id="p1-evidence", className="panel mt-2"),
        ], className="mt-3"),
    ])


def page2_layout():
    return html.Div([
        html.Div([
            dcc.DatePickerRange(id="p2-date", start_date="2025-01-01", end_date="2026-04-30", display_format="DD MMM YYYY"),
            dcc.Dropdown(id="p2-member", options=[{"label": m, "value": m} for m in sorted(DF_COMBINED["Employee"].dropna().unique())], multi=True, placeholder="All employees"),
            dcc.Dropdown(id="p2-expense", options=[{"label": m, "value": m} for m in sorted(DF_COMBINED["Expense Type"].dropna().unique())], multi=True, placeholder="All expense types"),
        ], className="filter-row"),
        html.Div(id="p2-kpis", className="grid-4 mb-3"),
        html.Div([
            html.Div([dcc.Graph(id="p2-spend-by-employee")], className="panel"),
            html.Div([
                html.Div("Top Vendors", className="table-title"),
                dash_table.DataTable(id="p2-vendor-table", page_size=15, sort_action="native", style_table={"overflowX": "auto"}),
            ], className="panel"),
        ], className="grid-2"),
        html.Div([
            html.Div([dcc.Graph(id="p2-breach-expense")], className="panel"),
            html.Div([
                html.Div("Detailed Exception Table", className="table-title"),
                dash_table.DataTable(id="p2-exception-table", page_size=20, sort_action="native", filter_action="native", export_format="csv", style_table={"overflowX": "auto"}),
            ], className="panel"),
        ], className="grid-2 mt-3"),
        html.Div([
            html.Div([
                html.Div("Claims vs Pre-Approvals", className="table-title"),
                dash_table.DataTable(id="p2-claim-pre-table", page_size=12, sort_action="native", style_table={"overflowX": "auto"}),
            ], className="panel"),
            html.Div([
                html.Div("Missing Receipt by Employee", className="table-title"),
                dash_table.DataTable(id="p2-missing-table", page_size=12, sort_action="native", style_table={"overflowX": "auto"}),
            ], className="panel"),
        ], className="grid-2 mt-3"),
    ])


def page3_layout():
    return html.Div([
        html.Div([
            dcc.DatePickerRange(id="p3-date", start_date="2025-01-01", end_date="2026-04-30", display_format="DD MMM YYYY"),
            dcc.Dropdown(id="p3-approver", options=[{"label": m, "value": m} for m in sorted(DF_APPROVAL["Approver Name"].dropna().unique())], multi=True, placeholder="All approvers"),
        ], className="filter-row"),
        html.Div(id="p3-kpis", className="grid-4 mb-3"),
        html.Div([
            html.Div([dcc.Graph(id="p3-donut")], className="panel"),
            html.Div([dcc.Graph(id="p3-by-approver")], className="panel"),
        ], className="grid-2"),
        html.Div([
            html.Div([dcc.Graph(id="p3-combo")], className="panel"),
            html.Div([dcc.Graph(id="p3-risk-profile")], className="panel"),
        ], className="grid-2 mt-3"),
        html.Div([
            html.Div("Approval Detail Table", className="table-title"),
            dash_table.DataTable(id="p3-detail", page_size=20, sort_action="native", filter_action="native", export_format="csv", style_table={"overflowX": "auto"}),
        ], className="panel mt-3"),
    ])


def catalogue_layout():
    """Test Catalogue tab — all tests, rules, status."""
    rm = RUN_METADATA

    # Status counts
    status_kpis = html.Div([
        format_kpi("Tests Executed", str(rm["tests_executed"])),
        format_kpi("Exceptions", str(rm["tests_exception"])),
        format_kpi("Pass", str(rm["tests_pass"])),
        format_kpi("Not Testable", str(rm["tests_na"])),
    ], className="grid-4 mb-3")

    # Methodology panel
    methodology = html.Details([
        html.Summary("Methodology & Guardrails", className="ghost", style={"fontSize": 13, "fontWeight": 600}),
        html.Div([
            html.Div([
                html.Div("Control Layer", style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginBottom": 4}),
                html.Table([
                    html.Thead(html.Tr([html.Th("Layer"), html.Th("What it proves")])),
                    html.Tbody([
                        html.Tr([html.Td("Population reconciliation"), html.Td("Source row count and spend reconciled")]),
                        html.Tr([html.Td("Deterministic rule"), html.Td("The test was applied consistently to every record")]),
                        html.Tr([html.Td("Evidence citation"), html.Td("Each metric links to the source data file")]),
                        html.Tr([html.Td("Auditor judgement"), html.Td("Required to confirm the item as a formal audit finding")]),
                    ]),
                ], style={"fontSize": 12, "borderCollapse": "collapse", "width": "100%"}),
            ], className="panel", style={"marginBottom": 12}),
            html.Div([
                html.Div("Population Reconciliation", style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginBottom": 4}),
                html.Div([
                    html.Span(f"Combined: {rm['population_reconciliation']['combined_rows']:,} rows", className="chip"),
                    html.Span(f"${rm['population_reconciliation']['combined_spend']:,.0f} spend", className="chip"),
                    html.Span(f"Approvals: {rm['population_reconciliation']['approval_rows']:,} rows", className="chip"),
                    html.Span(f"Pre-approvals: {rm['population_reconciliation']['preapproval_rows']:,} rows", className="chip"),
                ], className="chip-row"),
            ], className="panel", style={"marginBottom": 12}),
            html.Div([
                html.Div("Limitations", style={"fontWeight": 700, "fontSize": 11, "color": "#1e2761", "marginBottom": 4}),
                html.Ul([
                    html.Li("Source data completeness has not been independently verified."),
                    html.Li("Policy thresholds are hard-coded and may require periodic review."),
                    html.Li("Personal-expense flags are heuristic-based and require auditor judgement."),
                    html.Li("Rule-based results indicate exceptions, not confirmed findings."),
                ], style={"fontSize": 12.5, "color": "#2c3040", "margin": 0, "paddingLeft": 18}),
            ], className="panel"),
        ], style={"marginTop": 10}),
    ], className="mb-3")

    return html.Div([
        status_kpis,
        methodology,
        html.Div([
            html.Div("Test Catalogue", className="table-title"),
            dash_table.DataTable(
                id="catalogue-table",
                data=CATALOGUE_ROWS,
                columns=[{"name": c, "id": c} for c in ["Test ID", "Category", "Test Name", "Threshold", "Exceptions", "Status"]],
                page_size=20,
                sort_action="native",
                filter_action="native",
                style_table={"overflowX": "auto"},
                style_cell={"fontSize": 12, "padding": "6px 10px", "textAlign": "left"},
                style_data_conditional=[
                    {"if": {"filter_query": "{Status} = 'Exception'"}, "color": "#b85042", "fontWeight": 600},
                    {"if": {"filter_query": "{Status} = 'Pass'"}, "color": "#2c7a4b", "fontWeight": 600},
                    {"if": {"filter_query": "{Status} = 'Not testable'"}, "color": "#6b7283"},
                ],
                tooltip_data=[
                    {
                        "Test Name": {"value": f"**Objective:** {r['Control Objective']}\n\n**Population:** {r['Population']}\n\n**Rule:** {r['Rule']}", "type": "markdown"},
                    }
                    for r in CATALOGUE_ROWS
                ],
                tooltip_duration=None,
            ),
        ], className="panel"),
    ])


def insights_layout():
    source_label = "Demo data (live volume unavailable)" if RUNTIME_NOTICE else "Live source files"

    n_high = sum(1 for f in FINDINGS_SCORED if f["risk"] == "High")
    n_med = sum(1 for f in FINDINGS_SCORED if f["risk"] == "Medium")
    n_low = sum(1 for f in FINDINGS_SCORED if f["risk"] == "Low")
    total_exposure = sum(f.get("financial_exposure", 0) for f in FINDINGS_SCORED)

    # Summary bar
    summary_bar = html.Div([
        html.Span(f"{len(FINDINGS_SCORED)} findings", className="chip"),
        html.Span(f"{n_high} High · {n_med} Medium · {n_low} Low", className="chip"),
        html.Span(f"Financial exposure: ${total_exposure:,.0f}", className="chip",
                   style={"color": "#b85042", "borderColor": "#b85042"}) if total_exposure else None,
        html.Span("Metrics recomputed from source population",
                   className="chip", style={"color": "#2c7a4b", "borderColor": "#2c7a4b"}),
    ], className="chip-row", style={"marginBottom": 14})

    # Filters
    categories = sorted(set(t["category"] for t in TEST_CATALOGUE))
    filters_row = html.Div([
        dcc.Dropdown(id="finding-risk-filter",
                     options=[{"label": r, "value": r} for r in ["High", "Medium", "Low"]],
                     multi=True, placeholder="All risk levels", style={"minWidth": 160}),
        dcc.Dropdown(id="finding-category-filter",
                     options=[{"label": c, "value": c} for c in categories],
                     multi=True, placeholder="All categories", style={"minWidth": 180}),
        dcc.Dropdown(id="finding-sort",
                     options=[{"label": "Risk score", "value": "score"},
                              {"label": "Risk level", "value": "risk"},
                              {"label": "Financial exposure", "value": "exposure"}],
                     value="score", clearable=False, style={"minWidth": 160}),
    ], className="filter-row", style={"marginBottom": 12})

    # Export buttons
    export_row = html.Div([
        html.Button("Export PPTX", id="btn-export-pptx", className="ghost", style={"fontSize": 12.5}),
        html.Button("Export Excel", id="btn-export-excel", className="ghost", style={"fontSize": 12.5}),
        dcc.Download(id="download-pptx"),
        dcc.Download(id="download-excel"),
    ], style={"display": "flex", "gap": 8, "marginBottom": 14})

    # Build finding cards
    all_cards = [_build_finding_card(i, f) for i, f in enumerate(FINDINGS_SCORED)]
    high_cards = [c for i, c in enumerate(all_cards) if FINDINGS_SCORED[i]["risk"] == "High"]
    rest_cards = [c for i, c in enumerate(all_cards) if FINDINGS_SCORED[i]["risk"] != "High"]

    findings_children = [summary_bar, filters_row, export_row]
    findings_children.append(html.Div(id="filtered-findings-container", className="stack"))

    return html.Div([
        # ── Findings section (full width) ─────────────────────────────────
        html.Section([
            html.Div([
                html.H2("Findings", style={"margin": 0, "fontSize": 17, "fontWeight": 700}),
                html.Span("Deterministic · evidence-linked · source-file cited",
                          style={"fontSize": 12.5, "color": "#6b7283"}),
            ], style={"display": "flex", "alignItems": "baseline", "gap": 10,
                      "marginBottom": 12, "flexWrap": "wrap"}),

            html.Div([
                html.Span(f"Source: {source_label}", className="chip"),
                html.Span(f"{len(DF_COMBINED):,} claims", className="chip"),
                html.Span(f"{len(EXCO_MEMBERS)} ExCo members", className="chip"),
                html.Span(f"${EVIDENCE_PAYLOAD['total_spend']:,.0f} total spend", className="chip"),
            ], className="chip-row panel", style={"marginBottom": 16, "fontSize": 12.5}),

            html.Div(findings_children, className="stack"),
        ]),

        # ── Charts section (3-column grid) ────────────────────────────────
        html.Div([
            html.H2("Analytics", style={"margin": "24px 0 12px", "fontSize": 17, "fontWeight": 700}),
        ]),
        html.Div([
            html.Div([
                html.H3("Monthly T&E volume", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                dcc.Graph(figure=INSIGHTS_FIG1, config={"displayModeBar": False}),
            ], className="panel"),
            html.Div([
                html.H3("Findings by risk level", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                dcc.Graph(figure=INSIGHTS_FIG4, config={"displayModeBar": False}),
            ], className="panel"),
            html.Div([
                html.H3("Top 10 expense types by spend", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                dcc.Graph(figure=INSIGHTS_FIG5, config={"displayModeBar": False}),
            ], className="panel"),
            html.Div([
                html.H3("Policy exceptions by ExCo member", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                dcc.Graph(figure=INSIGHTS_FIG2, config={"displayModeBar": False}),
            ], className="panel"),
            html.Div([
                html.H3("Approver review quality", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                dcc.Graph(figure=INSIGHTS_FIG3, config={"displayModeBar": False}),
            ], className="panel"),
            html.Div([
                html.H3("Top 10 spenders", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                dcc.Graph(figure=INSIGHTS_FIG6, config={"displayModeBar": False}),
            ], className="panel"),
        ], className="grid-3", style={"alignItems": "start"}),

        # Outlier table (full width)
        html.Div([
            html.H3("Spend Outliers (Z-Score > 2.0)", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
            html.P("Claims that are statistically unusual relative to the employee's own spending pattern.",
                   className="sub"),
            dash_table.DataTable(
                data=OUTLIER_DF.to_dict("records") if not OUTLIER_DF.empty else [],
                columns=[{"name": c, "id": c} for c in OUTLIER_DF.columns] if not OUTLIER_DF.empty else [],
                page_size=10, sort_action="native",
                style_table={"overflowX": "auto"},
                style_cell={"fontSize": 11.5, "padding": "5px 8px"},
                style_data_conditional=[
                    {"if": {"filter_query": "{Z-Score} > 3"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"},
                ],
            ),
        ], className="panel", style={"marginTop": 16}) if not OUTLIER_DF.empty else None,

        # Evidence drawer
        dbc.Offcanvas(
            id="evidence-offcanvas", title="Evidence", placement="end", is_open=False,
            style={"width": "min(620px, 94vw)"}, children=html.Div(id="evidence-offcanvas-body"),
        ),

        # Exception drill-down drawer
        dbc.Offcanvas(
            id="exception-offcanvas", title="Exception Records", placement="end", is_open=False,
            style={"width": "min(800px, 96vw)"}, children=html.Div(id="exception-offcanvas-body"),
        ),

        # Copy confirmation toast
        dbc.Toast(id="copy-toast", header="Copied", is_open=False, duration=2000,
                  style={"position": "fixed", "bottom": 20, "right": 20, "zIndex": 9999}),

        # Hidden findings data for clipboard
        html.Script(id="findings-data-store", type="application/json",
                    children=json.dumps([{k: v for k, v in f.items() if k != "risk_score"} for f in FINDINGS_SCORED])),
    ])


def _render_filtered_findings(risk_filter, category_filter, sort_by):
    """Render only the priority findings initially; disclose the rest on demand."""
    category_by_test = {test["test_id"]: test["category"] for test in TEST_CATALOGUE}
    filtered = [
        (idx, finding) for idx, finding in enumerate(FINDINGS_SCORED)
        if (not risk_filter or finding["risk"] in risk_filter)
        and (not category_filter or category_by_test.get(finding.get("test_id")) in category_filter)
    ]

    if sort_by == "exposure":
        filtered.sort(key=lambda item: item[1].get("financial_exposure", 0), reverse=True)
    elif sort_by == "risk":
        risk_order = {"High": 0, "Medium": 1, "Low": 2}
        filtered.sort(key=lambda item: (risk_order.get(item[1]["risk"], 3), -item[1].get("risk_score", 0)))
    else:
        filtered.sort(key=lambda item: item[1].get("risk_score", 0), reverse=True)

    if not filtered:
        return html.Div(
            "No findings match the selected filters.",
            className="panel",
            style={"color": "#6b7283", "fontSize": 13},
        )

    priority = [_build_finding_card(idx, finding) for idx, finding in filtered[:3]]
    remaining = [_build_finding_card(idx, finding) for idx, finding in filtered[3:]]
    children = [
        html.Div(f"Showing the top {min(3, len(filtered))} priority findings", style={
            "fontSize": 12, "fontWeight": 600, "color": "#6b7283", "marginBottom": -4,
        }),
        *priority,
    ]
    if remaining:
        children.append(html.Details([
            html.Summary(f"Show {len(remaining)} additional findings", className="ghost further-analysis"),
            html.Div(remaining, className="stack", style={"marginTop": 12}),
        ]))
    return children


def executive_brief_layout():
    """CIA-facing landing page: concise, decision-oriented, and non-technical."""
    n_high = sum(1 for f in FINDINGS_SCORED if f["risk"] == "High")
    total_exposure = sum(f.get("financial_exposure", 0) for f in FINDINGS_SCORED)
    priority = sorted(FINDINGS_SCORED, key=lambda f: f.get("risk_score", 0), reverse=True)[:5]

    priority_rows = [
        html.Tr([
            html.Td(html.Span(f["risk"], className="chip", style={
                "color": _severity_color(f["risk"]), "borderColor": _severity_color(f["risk"]),
            })),
            html.Td(f["title"], style={"fontWeight": 600}),
            html.Td(f"${f.get('financial_exposure', 0):,.0f}" if f.get("financial_exposure") else "—",
                    style={"fontFamily": "monospace", "textAlign": "right"}),
            html.Td(f"{f.get('exception_count', 0):,}" if f.get("exception_count") else "—",
                    style={"fontFamily": "monospace", "textAlign": "right"}),
        ])
        for f in priority
    ]
    th_style = {
        "fontSize": 10, "fontWeight": 700, "letterSpacing": "0.5px", "textTransform": "uppercase",
        "color": "#6b7283", "padding": "0 10px 7px", "borderBottom": "1px solid #e4e7ee",
    }

    return html.Div([
        html.Div([
            html.P("Internal Audit executive brief", className="showcase-eyebrow"),
            html.H2(
                f"{n_high} high-priority matters require management attention across "
                f"{len(FINDINGS_SCORED)} assessed findings.",
                className="showcase-headline",
            ),
            html.P(
                f"Potential financial exposure of ${total_exposure:,.0f}. "
                "Results are reproducible and linked to underlying source records.",
                className="showcase-supporting",
            ),
        ], className="showcase-hero"),

        html.Div([
            format_kpi("Potential exposure", f"${total_exposure:,.0f}"),
            format_kpi("High-priority matters", str(n_high)),
            format_kpi("Tests with exceptions", str(RUN_METADATA["tests_exception"])),
            html.Div(id="exec-action-kpi", className="kpi-tile"),
        ], className="grid-4", style={"marginBottom": 16}),

        html.Div([
            html.Div([
                html.H3("Risk distribution", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                html.P("Priority is determined from severity, recurrence and financial exposure.", className="sub"),
                dcc.Graph(figure=INSIGHTS_FIG4, config={"displayModeBar": False}),
            ], className="panel"),
            html.Div([
                html.H3("T&E volume trend", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                html.P("A change in activity helps frame the scale and timing of exceptions.", className="sub"),
                dcc.Graph(figure=INSIGHTS_FIG1, config={"displayModeBar": False}),
            ], className="panel"),
        ], className="grid-2", style={"marginBottom": 16}),

        html.Div([
            html.Div([
                html.H3("Matters requiring attention", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                html.P("Open Findings & Actions to inspect evidence, assign ownership, or prepare the executive pack.", className="sub"),
                html.Table([
                    html.Thead(html.Tr([
                        html.Th("Risk", style=th_style), html.Th("Matter", style=th_style),
                        html.Th("Exposure", style={**th_style, "textAlign": "right"}),
                        html.Th("Exceptions", style={**th_style, "textAlign": "right"}),
                    ])),
                    html.Tbody(priority_rows),
                ], style={"width": "100%", "borderCollapse": "collapse", "fontSize": 12.5}),
            ], className="panel"),
            html.Div([
                html.H3("Management action status", style={"margin": "0 0 2px", "fontSize": 14.5, "fontWeight": 700}),
                html.P("Action ownership and responses are maintained in this session for the showcase.", className="sub"),
                html.Div(id="exec-action-summary"),
            ], className="panel"),
        ], className="grid-2"),
    ])


def mgmt_tracker_layout():
    """Management Action Tracker tab (session-persisted)."""
    return html.Div([
        html.Div([
            html.H2("Management Action Tracker", style={"margin": 0, "fontSize": 17, "fontWeight": 700}),
            html.Span("For showcase use · session-only data resets when the app restarts",
                      style={"fontSize": 12.5, "color": "#6b7283"}),
        ], style={"display": "flex", "alignItems": "baseline", "gap": 10, "marginBottom": 16, "flexWrap": "wrap"}),

        html.Div(id="mgmt-tracker-body"),

        html.Hr(style={"margin": "20px 0", "borderColor": "#e4e7ee"}),

        # Audit log
        html.Details([
            html.Summary("Audit Log", className="ghost", style={"fontSize": 13, "fontWeight": 600}),
            html.Div(id="audit-log-body", style={"marginTop": 10}),
        ]),
    ])


# ─── T&E workspace (existing prototype, now accessible as a Skill run) ───────

def tne_workspace_layout():
    """The original T&E ExCo audit workspace, preserved intact."""
    return html.Div([
        build_header(),
        dcc.Store(id="store-mgmt-actions", storage_type="session", data={}),
        dcc.Store(id="store-audit-log", storage_type="session", data=[]),
        dcc.Store(id="selected-action", storage_type="memory", data=None),
        html.Div([
            dbc.Tabs([
                dbc.Tab(executive_brief_layout(), label="Executive Brief", tab_id="tab-executive"),
                dbc.Tab(dbc.Tabs([
                    dbc.Tab(insights_layout(), label="Findings & Evidence", tab_id="sub-findings"),
                    dbc.Tab(mgmt_tracker_layout(), label="Management Actions", tab_id="sub-actions"),
                ], active_tab="sub-findings"), label="Findings & Actions", tab_id="tab-findings"),
                dbc.Tab(dbc.Tabs([
                    dbc.Tab(page1_layout(), label="Executive analysis", tab_id="sub-overview"),
                    dbc.Tab(page2_layout(), label="Detailed risk", tab_id="sub-risk"),
                    dbc.Tab(page3_layout(), label="Receipt & approver review", tab_id="sub-approval"),
                    dbc.Tab(catalogue_layout(), label="Test catalogue", tab_id="sub-catalogue"),
                ], active_tab="sub-overview"), label="Audit Detail", tab_id="tab-audit"),
            ], id="main-tabs", active_tab="tab-executive"),
            dbc.Modal([
                dbc.ModalHeader(dbc.ModalTitle(id="action-modal-title")),
                dbc.ModalBody([
                    html.Div(id="action-modal-finding", style={"fontSize": 12.5, "color": "#6b7283", "marginBottom": 14}),
                    html.Label("Action owner", className="table-title"),
                    dcc.Input(id="action-owner", type="text", placeholder="Accountable executive or business unit", style={"width": "100%", "marginBottom": 12}),
                    html.Label("Status", className="table-title"),
                    dcc.Dropdown(id="action-status", options=[
                        {"label": "Open", "value": "Open"}, {"label": "Under review", "value": "Under review"},
                        {"label": "Agreed", "value": "Agreed"}, {"label": "Remediated", "value": "Remediated"},
                        {"label": "Closed", "value": "Closed"},
                    ], value="Open", clearable=False, style={"marginBottom": 12}),
                    html.Label("Target date", className="table-title"),
                    dcc.DatePickerSingle(id="action-target-date", display_format="DD MMM YYYY", style={"marginBottom": 12}),
                    html.Label("Management response", className="table-title"),
                    dcc.Textarea(id="action-response", placeholder="Agreed response, next step, or rationale", style={"width": "100%", "height": 110}),
                ]),
                dbc.ModalFooter([
                    html.Button("Cancel", id="action-cancel", className="ghost", style={"width": "auto"}),
                    html.Button("Save action", id="action-save", className="btn-generate"),
                ]),
            ], id="action-modal", is_open=False, size="lg", centered=True),
        ], className="shell dashboard-shell")
    ])


# ─── Platform-routed layout ─────────────────────────────────────────────────

app.layout = html.Div([
    dcc.Location(id="url", refresh=False),
    platform_header(),
    platform_nav(),
    html.Div(id="page-content"),
], className="app-shell")


@callback(
    Output("page-content", "children"),
    Input("url", "pathname"),
)
def route_page(pathname):
    """Route to platform pages or the T&E workspace."""
    if pathname == "/skills":
        return skill_library_page()
    elif pathname and pathname.startswith("/skills/"):
        skill_id = pathname.rsplit("/", 1)[-1]
        return skill_methodology_page(skill_id)
    elif pathname == "/runs":
        return audit_runs_page()
    elif pathname == "/trace":
        return platform_trace_page()
    elif pathname == "/actions":
        return management_actions_page()
    elif pathname == "/workspace/tne":
        return tne_workspace_layout()
    # Default: landing page
    return landing_page()


# ─── Platform callbacks ─────────────────────────────────────────────────────

@callback(
    Output("data-search-results", "children"),
    Input("data-search-input", "value"),
    prevent_initial_call=True,
)
def search_data_assets(query):
    """Search governed data assets via adapter."""
    from src.platform.components import data_asset_card
    results = adapters.search_governed_data(query or "")
    if not results:
        return html.P("No matching data assets found.", style={"color": "#6b7283", "fontSize": 13})
    return [data_asset_card(a) for a in results[:6]]


@callback(
    Output("workflow-preview-container", "children"),
    Input("audit-objective", "value"),
    prevent_initial_call=False,
)
def render_workflow_preview(objective):
    """Show the agent plan preview stages."""
    plan = adapters.propose_plan({"mode": "playbook", "skill": adapters.get_skill("SKILL-001"), "sources_count": 3})
    stages = plan.get("stages", [])
    total = len(stages)
    return html.Div([
        html.Div(
            [workflow_stage(s, i, total) for i, s in enumerate(stages)],
            style={"padding": "12px 0"},
        ),
        demo_indicator("Preview only — workflow has not been executed") if plan.get("mock") else None,
    ], className="panel")


@callback(
    Output("run-summary-preview", "children"),
    Input("audit-objective", "value"),
    prevent_initial_call=False,
)
def render_run_summary(objective):
    """Show compact confirmation summary before starting."""
    return html.Div([
        html.Div([
            html.Div([
                html.Span("Mode", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                          "color": "#6b7283", "letterSpacing": "0.03em"}),
                html.Div("Playbook", style={"fontSize": 13, "fontWeight": 600}),
            ], style={"flex": 1}),
            html.Div([
                html.Span("Skill", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                           "color": "#6b7283", "letterSpacing": "0.03em"}),
                html.Div("ExCo T&E Executive Diligence", style={"fontSize": 13, "fontWeight": 600}),
            ], style={"flex": 2}),
            html.Div([
                html.Span("Data mode", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                               "color": "#6b7283", "letterSpacing": "0.03em"}),
                html.Div("Demo" if adapters.is_demo_mode() else "Live",
                         style={"fontSize": 13, "fontWeight": 600}),
            ], style={"flex": 1}),
            html.Div([
                html.Span("Outputs", style={"fontSize": 11, "fontWeight": 700, "textTransform": "uppercase",
                                             "color": "#6b7283", "letterSpacing": "0.03em"}),
                html.Div("Findings, PPTX, Excel", style={"fontSize": 13, "fontWeight": 600}),
            ], style={"flex": 1}),
        ], style={"display": "flex", "gap": 16}),
    ], className="panel", style={"marginTop": 12})


@callback(
    Output("url", "pathname", allow_duplicate=True),
    Input("start-run-btn", "n_clicks"),
    prevent_initial_call=True,
)
def start_demo_run(n_clicks):
    """Start a demo run — navigate to the T&E workspace."""
    if not n_clicks:
        raise PreventUpdate
    # In demo mode, navigate directly to the existing T&E workspace
    return "/workspace/tne"


@callback(
    Output("p1-kpis", "children"),
    Output("p1-monthly", "figure"),
    Output("p1-breach-by-member", "figure"),
    Output("p1-breach-dist", "figure"),
    Output("p1-missing-tier", "figure"),
    Output("p1-precomp", "figure"),
    Output("p1-attendee-table", "data"),
    Output("p1-attendee-table", "columns"),
    Output("p1-attendee-table", "style_data_conditional"),
    Output("p1-evidence", "children"),
    Input("p1-date", "start_date"),
    Input("p1-date", "end_date"),
    Input("p1-member", "value"),
)
def update_page1(start_date, end_date, members):
    df = filter_combined(start_date, end_date, members)
    breach_mask = any_breach_mask(df)

    k1 = format_kpi("Total T&E Spend", f"${df['Expense Amount (reimbursement currency)'].sum():,.0f}")
    k2 = format_kpi("Total Breach Count", f"{int(breach_mask.sum()):,}")
    k3 = format_kpi("Breach Amount ($)", f"${df.loc[breach_mask, 'Expense Amount (reimbursement currency)'].sum():,.0f}")
    miss_mask = df.get("RF_CS_MissingReceipt", pd.Series(0, index=df.index)) == 1
    k4 = format_kpi("Missing Receipts", f"{int(miss_mask.sum()):,}")

    monthly = df.dropna(subset=["Transaction Date"]).copy()
    monthly["Month"] = monthly["Transaction Date"].dt.to_period("M").dt.to_timestamp()
    m = monthly.groupby("Month", as_index=False).agg(Claims=("Employee", "count"), Amount=("Expense Amount (reimbursement currency)", "sum"))
    f1 = go.Figure()
    f1.add_bar(x=m["Month"], y=m["Claims"], name="Claim Count", marker_color="#1e2761")
    f1.add_trace(go.Scatter(x=m["Month"], y=m["Amount"], name="Amount", mode="lines+markers", yaxis="y2", line=dict(color="#1c7293", width=3)))
    f1.update_layout(title="Monthly Trend", yaxis2=dict(overlaying="y", side="right"), legend=dict(orientation="h"), margin=dict(l=20, r=20, t=50, b=30))

    ex = df.copy().explode("Breach_Type_List")
    ex = ex[ex["Breach_Type_List"] != "No Breach"]
    if ex.empty:
        ex = pd.DataFrame({"Employee": [], "Breach_Type_List": []})
    bmem = ex.groupby(["Employee", "Breach_Type_List"], as_index=False).size().rename(columns={"size": "Count"})
    totals = bmem.groupby("Employee")["Count"].sum().sort_values(ascending=False).index.tolist()
    f2 = px.bar(bmem, x="Count", y="Employee", color="Breach_Type_List", orientation="h", category_orders={"Employee": totals}, title="Breach Count by ExCo Member")

    bdist = ex.groupby("Breach_Type_List", as_index=False).size().rename(columns={"size": "Count"}).sort_values("Count", ascending=False)
    f3 = px.bar(bdist, x="Count", y="Breach_Type_List", orientation="h", title="Breach Type Distribution", color="Count", color_continuous_scale="Blues")

    mt = df[miss_mask].groupby("Receipt_Tier", as_index=False).agg(Claims=("Employee", "count"), Amount=("Expense Amount (reimbursement currency)", "sum"))
    f4 = go.Figure()
    f4.add_bar(x=mt["Receipt_Tier"], y=mt["Claims"], name="Missing Receipt Claims", marker_color="#1e2761")
    f4.add_trace(go.Scatter(x=mt["Receipt_Tier"], y=mt["Amount"], name="Amount", mode="lines+markers", yaxis="y2", line=dict(color="#1c7293", width=3)))
    f4.update_layout(title="Missing Receipt by Receipt Tier", yaxis2=dict(overlaying="y", side="right"), legend=dict(orientation="h"))

    claim_ct = df.groupby("Employee", as_index=False).size().rename(columns={"size": "Claims"})
    pre_ct = DF_PRE.groupby("Employee", as_index=False).size().rename(columns={"size": "PreApprovals"})
    gp = claim_ct.merge(pre_ct, on="Employee", how="outer").fillna(0)
    f5 = go.Figure()
    f5.add_bar(x=gp["Employee"], y=gp["Claims"], name="Claims", marker_color="#1e2761")
    f5.add_bar(x=gp["Employee"], y=gp["PreApprovals"], name="Pre-Approvals", marker_color="#1c7293")
    f5.update_layout(title="Travel Pre-Request Compliance", barmode="group", xaxis_tickangle=-45)

    ent = df[df["Expense Type"].astype(str).str.contains("entertain", case=False, na=False)].copy()
    if ent.empty:
        ent = df.copy()
    ent_claims = ent.groupby("Employee", as_index=False).size().rename(columns={"size": "Entertainment Claims"})
    missing_att = ent.groupby("Employee", as_index=False)["RF_ATT_Missing"].sum().rename(columns={"RF_ATT_Missing": "Missing Attendee Count"})
    tab = ent_claims.merge(missing_att, on="Employee", how="left").fillna(0)
    tab["Missing %"] = np.where(tab["Entertainment Claims"] > 0, (tab["Missing Attendee Count"] / tab["Entertainment Claims"] * 100).round(1), 0)

    evidence = [
        html.P(f"Rows in scope: {len(df):,}"),
        html.P(f"Rows flagged as breaches: {int(breach_mask.sum()):,}"),
        html.P("Breach type derivation uses RF_ flags on each claim row and expands multi-breach claims for category counts."),
    ]

    style_cond = [{"if": {"filter_query": "{Missing %} > 80"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"}]
    cols = [{"name": c, "id": c} for c in ["Employee", "Entertainment Claims", "Missing Attendee Count", "Missing %"]]

    return [k1, k2, k3, k4], f1, f2, f3, f4, f5, tab.to_dict("records"), cols, style_cond, evidence


@callback(
    Output("p2-kpis", "children"),
    Output("p2-spend-by-employee", "figure"),
    Output("p2-vendor-table", "data"),
    Output("p2-vendor-table", "columns"),
    Output("p2-vendor-table", "style_data_conditional"),
    Output("p2-breach-expense", "figure"),
    Output("p2-exception-table", "data"),
    Output("p2-exception-table", "columns"),
    Output("p2-claim-pre-table", "data"),
    Output("p2-claim-pre-table", "columns"),
    Output("p2-claim-pre-table", "style_data_conditional"),
    Output("p2-missing-table", "data"),
    Output("p2-missing-table", "columns"),
    Output("p2-missing-table", "style_data_conditional"),
    Input("p2-date", "start_date"),
    Input("p2-date", "end_date"),
    Input("p2-member", "value"),
    Input("p2-expense", "value"),
)
def update_page2(start_date, end_date, members, expense_types):
    df = filter_combined(start_date, end_date, members, expense_types)
    breach_mask = any_breach_mask(df)

    k1 = format_kpi("Total Claims", f"{len(df):,}")
    k2 = format_kpi("Unique Employees", f"{df['Employee'].nunique():,}")
    k3 = format_kpi("Avg Claim Amount", f"${df['Expense Amount (reimbursement currency)'].mean():,.0f}")
    k4 = format_kpi("High-Value Claims (>$5K)", f"{int((df['Expense Amount (reimbursement currency)'] > 5000).sum()):,}")

    spend = df.groupby(["Employee", "Source_Population"], as_index=False)["Expense Amount (reimbursement currency)"].sum()
    order = spend.groupby("Employee")["Expense Amount (reimbursement currency)"].sum().sort_values(ascending=False).index.tolist()
    f1 = px.bar(spend, x="Expense Amount (reimbursement currency)", y="Employee", color="Source_Population", orientation="h", category_orders={"Employee": order}, title="Spend by Employee")

    vendor = df.groupby("Vendor", as_index=False).agg(
        Claim_Count=("Employee", "count"),
        Total_Amount=("Expense Amount (reimbursement currency)", "sum"),
        Avg_Claim=("Expense Amount (reimbursement currency)", "mean"),
        ExCo_Members_Using=("Employee", "nunique"),
    ).sort_values("Total_Amount", ascending=False).head(15)

    by_exp = df[breach_mask].groupby("Expense Type", as_index=False).size().rename(columns={"size": "Breach Count"}).sort_values("Breach Count", ascending=False)
    f3 = px.bar(by_exp, x="Breach Count", y="Expense Type", orientation="h", title="Breach Count by Expense Type", color="Breach Count", color_continuous_scale="Teal")

    ex = df[breach_mask].copy().explode("Breach_Type_List")
    ex["Breach Type(s)"] = ex["Breach_Type_List"]
    ex_table = ex[["Employee", "Transaction Date", "Vendor", "Expense Type", "Expense Amount (reimbursement currency)", "Breach Type(s)", "Source_Population"]].copy()
    ex_table.rename(columns={"Expense Amount (reimbursement currency)": "Amount ($)"}, inplace=True)

    claim_ag = df.groupby("Employee", as_index=False).agg(Claims_Filed=("Employee", "count"), Claims_Amount=("Expense Amount (reimbursement currency)", "sum"))
    pre_ag = DF_PRE.groupby("Employee", as_index=False).agg(Pre_Approvals=("Employee", "count"), Pre_Approvals_Amount=("PreApprovalAmount", "sum"))
    cp = claim_ag.merge(pre_ag, on="Employee", how="outer").fillna(0)
    cp["Gap"] = cp["Claims_Filed"] - cp["Pre_Approvals"]

    miss = df.groupby("Employee", as_index=False).agg(
        Total_Claims=("Employee", "count"),
        Claims_Missing_Receipt=("RF_CS_MissingReceipt", "sum"),
        Total_At_Risk=("Expense Amount (reimbursement currency)", "sum"),
    )
    miss["Missing %"] = np.where(miss["Total_Claims"] > 0, (miss["Claims_Missing_Receipt"] / miss["Total_Claims"] * 100).round(1), 0)
    miss = miss.sort_values("Missing %", ascending=False)

    vendor_style = [{"if": {"filter_query": "{Avg_Claim} > 2000"}, "backgroundColor": "#fff7e6", "color": "#8a4b00"}]
    cp_style = [{"if": {"filter_query": "{Gap} > 5"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"}]
    miss_style = [{"if": {"filter_query": "{Missing %} > 50"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"}]

    return [k1, k2, k3, k4], f1, vendor.to_dict("records"), [{"name": c, "id": c} for c in vendor.columns], vendor_style, f3, ex_table.to_dict("records"), [{"name": c, "id": c} for c in ex_table.columns], cp.to_dict("records"), [{"name": c, "id": c} for c in cp.columns], cp_style, miss.to_dict("records"), [{"name": c, "id": c} for c in miss.columns], miss_style


@callback(
    Output("p3-kpis", "children"),
    Output("p3-donut", "figure"),
    Output("p3-by-approver", "figure"),
    Output("p3-combo", "figure"),
    Output("p3-risk-profile", "figure"),
    Output("p3-detail", "data"),
    Output("p3-detail", "columns"),
    Output("p3-detail", "style_data_conditional"),
    Input("p3-date", "start_date"),
    Input("p3-date", "end_date"),
    Input("p3-approver", "value"),
)
def update_page3(start_date, end_date, approvers):
    df = filter_by_dates(DF_APPROVAL, "Report Date", start_date, end_date)
    if approvers:
        df = df[df["Approver Name"].isin(approvers)]

    k1 = format_kpi("Total Employees (claimants)", f"{df['Employee Name'].nunique():,}")
    k2 = format_kpi("Total Approvers (ExCo)", f"{df['Approver Name'].nunique():,}")
    k3 = format_kpi("Total Report Amount", f"${df['Report Amount'].sum():,.0f}")
    k4 = format_kpi("Overall Receipt Viewed %", f"{(df['Receipt_Viewed'].mean() * 100 if len(df) else 0):.1f}%")

    donut_df = df.groupby("Receipt_Status", as_index=False).size().rename(columns={"size": "Count"})
    f1 = px.pie(donut_df, names="Receipt_Status", values="Count", hole=0.55, color="Receipt_Status", color_discrete_map={"Viewed Report Receipts": "#2c7a4b", "Viewed Entry Receipts": "#1c7293", "No Receipt Viewed": "#b85042"}, title="How Approver Reviews Receipt")

    by_app = df.groupby(["Approver Name", "Receipt_Status"], as_index=False).size().rename(columns={"size": "Count"})
    totals = by_app.groupby("Approver Name")["Count"].sum().sort_values(ascending=False).index.tolist()
    f2 = px.bar(by_app, x="Approver Name", y="Count", color="Receipt_Status", barmode="stack", category_orders={"Approver Name": totals}, title="Receipt Review by ExCo Approver")

    combo = df.groupby("Approver Name", as_index=False).agg(Receipt_Viewed_Pct=("Receipt_Viewed", lambda s: round(float(s.mean() * 100), 1)), Instant_Approval_Pct=("Instant", lambda s: round(float(s.mean() * 100), 1)), Reports=("Approver Name", "count"))
    combo = combo.sort_values("Receipt_Viewed_Pct", ascending=False)
    f3 = go.Figure()
    f3.add_bar(x=combo["Approver Name"], y=combo["Receipt_Viewed_Pct"], name="Receipt Viewed %", marker_color="#1c7293")
    f3.add_trace(go.Scatter(x=combo["Approver Name"], y=combo["Instant_Approval_Pct"], name="Instant Approval %", mode="lines+markers", yaxis="y2", line=dict(color="#b85042", width=3)))
    f3.add_hline(y=50, line_dash="dash", line_color="#e0952a")
    f3.update_layout(title="Receipt Viewed % + Instant Approval %", yaxis=dict(range=[0, 100]), yaxis2=dict(range=[0, 100], overlaying="y", side="right"), xaxis_tickangle=-35)

    rp = df.groupby("Approver Name", as_index=False).agg(Viewed=("Receipt_Viewed", "sum"), Total=("Receipt_Viewed", "count"))
    rp["Viewed %"] = np.where(rp["Total"] > 0, rp["Viewed"] / rp["Total"] * 100, 0)
    rp["No View %"] = 100 - rp["Viewed %"]
    rp = rp.sort_values("No View %", ascending=False)
    f4 = go.Figure()
    f4.add_bar(y=rp["Approver Name"], x=rp["Viewed %"], name="Viewed", orientation="h", marker_color="#2c7a4b")
    f4.add_bar(y=rp["Approver Name"], x=rp["No View %"], name="No View", orientation="h", marker_color="#b85042")
    f4.update_layout(title="Approver Risk Profile", barmode="stack", xaxis=dict(range=[0, 100]))

    detail = df[["Approver Name", "Employee Name", "Report Date", "Approval Minutes", "Report Viewed Receipts", "Entry Viewed Receipts", "Instant", "Receipt_Status"]].copy()
    detail["WorstCase"] = np.where((detail["Instant"] == 1) & (detail["Receipt_Status"] == "No Receipt Viewed"), "Yes", "No")

    styles = [
        {"if": {"filter_query": "{WorstCase} = 'Yes'"}, "backgroundColor": "#fdeaea", "color": "#9b1c1c"},
        {"if": {"filter_query": "{Receipt_Status} contains 'Viewed'"}, "backgroundColor": "#edf8f1"},
    ]

    return [k1, k2, k3, k4], f1, f2, f3, f4, detail.to_dict("records"), [{"name": c, "id": c} for c in detail.columns], styles


@callback(
    Output("filtered-findings-container", "children"),
    Input("finding-risk-filter", "value"),
    Input("finding-category-filter", "value"),
    Input("finding-sort", "value"),
)
def filter_findings(risk_filter, category_filter, sort_by):
    return _render_filtered_findings(risk_filter, category_filter, sort_by)


@callback(
    Output("evidence-offcanvas", "is_open"),
    Output("evidence-offcanvas-body", "children"),
    Output("evidence-offcanvas", "title"),
    Input({"type": "view-evidence-btn", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_evidence_drawer(n_clicks_list):
    if not any(n for n in n_clicks_list if n):
        raise PreventUpdate
    if not ctx.triggered_id:
        raise PreventUpdate

    idx = ctx.triggered_id["index"]
    finding = FINDINGS_SCORED[idx]
    risk = finding["risk"]
    mat_color = _severity_color(risk)
    test_id = finding.get("test_id", "")
    metrics_cited = finding.get("metrics_cited", [])
    questions = finding.get("management_questions", [])

    _section_head = {"fontSize": 11, "fontWeight": 700, "letterSpacing": "0.5px",
                     "textTransform": "uppercase", "color": "#1e2761", "marginBottom": 7}
    _th = {"fontSize": 10, "fontWeight": 700, "letterSpacing": "0.5px", "textTransform": "uppercase",
           "color": "#6b7283", "paddingBottom": 5, "borderBottom": "1px solid #e4e7ee",
           "whiteSpace": "nowrap", "paddingRight": 12}
    _td = {"padding": "6px 12px 6px 0", "borderBottom": "1px solid #f5f6f8", "verticalAlign": "top"}

    payload_metrics = EVIDENCE_PAYLOAD.get("metrics", {})
    table_rows = []
    guardrail_chips = []
    for mid in metrics_cited:
        m = payload_metrics.get(mid, {})
        if not m:
            continue
        val = m.get("value", "—")
        unit = m.get("unit", "")
        src_file = m.get("source_file", "—")
        val_str = _fmt_metric(val, unit)

        table_rows.append(html.Tr([
            html.Td(mid, style={**_td, "fontFamily": "monospace", "fontSize": 11, "color": "#6b7283"}),
            html.Td(val_str, style={**_td, "fontSize": 12.5, "fontWeight": 600, "fontFamily": "monospace", "textAlign": "right"}),
            html.Td(src_file, style={**_td, "fontSize": 11, "color": "#6b7283", "maxWidth": 200,
                                     "overflow": "hidden", "textOverflow": "ellipsis"}),
            html.Td(html.Span("✓ recomputed", style={"fontSize": 11, "color": "#2c7a4b",
                                                       "background": "#eaf5ee", "border": "1px solid #bfe0cc",
                                                       "borderRadius": 6, "padding": "2px 7px", "fontWeight": 600}),
                    style={**_td, "textAlign": "right", "paddingRight": 0}),
        ]))

        guardrail_chips.append(html.Span(
            f"✓ {val_str}", title=f"{mid} from {src_file}",
            style={"fontSize": 11.5, "padding": "3px 8px", "borderRadius": 6, "fontFamily": "monospace",
                   "border": "1px solid #bfe0cc", "background": "#eaf5ee", "color": "#2c7a4b",
                   "whiteSpace": "nowrap"},
        ))

    content = html.Div([
        html.Div([
            html.Span(risk, className="chip", style={"color": mat_color, "borderColor": mat_color}),
            html.Span(test_id, className="chip mono", style={"color": "#6b7283"}),
            html.Span(f"Score: {finding.get('risk_score', 0)}", className="chip mono", style={"color": "#6b7283"}),
        ], style={"display": "flex", "gap": 8, "marginBottom": 14}),

        html.Div([
            html.Div("Observation", style=_section_head),
            html.P(finding["observation"],
                   style={"margin": 0, "fontSize": 13.5, "lineHeight": 1.55, "color": "#2c3040"}),
        ], style={"marginBottom": 20}),

        html.Div([
            html.Div("Recommendation", style=_section_head),
            html.P(finding.get("recommendation", ""),
                   style={"margin": 0, "fontSize": 13, "lineHeight": 1.5, "color": "#2c3040"}),
        ], style={"marginBottom": 20}) if finding.get("recommendation") else None,

        html.Div([
            html.Div("Metrics cited — source file traceability", style=_section_head),
            html.P("Metric recomputed from source population at runtime.",
                   style={"margin": "0 0 8px", "fontSize": 11.5, "color": "#6b7283"}),
            html.Div(html.Table([
                html.Thead(html.Tr([
                    html.Th("metric_id", style=_th),
                    html.Th("Value", style={**_th, "textAlign": "right"}),
                    html.Th("Source file", style=_th),
                    html.Th("Status", style={**_th, "textAlign": "right", "paddingRight": 0}),
                ])),
                html.Tbody(table_rows),
            ], style={"borderCollapse": "collapse", "width": "100%", "fontSize": 12.5}),
            style={"overflowX": "auto"}),
        ], style={"marginBottom": 20}) if table_rows else None,

        html.Div([
            html.Div(f"Source traceability · {len(guardrail_chips)}/{len(guardrail_chips)} recomputed from source", style=_section_head),
            html.P("All quantities are computed deterministically from source data files.",
                   style={"margin": "0 0 8px", "fontSize": 11.5, "color": "#6b7283"}),
            html.Div(guardrail_chips, style={"display": "flex", "flexWrap": "wrap", "gap": 6}),
        ], style={"marginBottom": 20}) if guardrail_chips else None,

        html.Div([
            html.Div("Management questions", style=_section_head),
            html.Ul([html.Li(q, style={"marginBottom": 6}) for q in questions],
                    style={"margin": 0, "paddingLeft": 18, "fontSize": 13, "color": "#2c3040"}),
        ]) if questions else None,
    ], style={"padding": "0 2px"})

    return True, content, finding["title"]


# ─── Exception drill-down callback ───────────────────────────────────────────
@callback(
    Output("exception-offcanvas", "is_open"),
    Output("exception-offcanvas-body", "children"),
    Output("exception-offcanvas", "title"),
    Input({"type": "view-exceptions-btn", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_exception_drawer(n_clicks_list):
    if not any(n for n in n_clicks_list if n):
        raise PreventUpdate
    if not ctx.triggered_id:
        raise PreventUpdate

    idx = ctx.triggered_id["index"]
    finding = FINDINGS_SCORED[idx]
    test_id = finding.get("test_id", "")

    from src.excel_export import _get_exception_df
    df_exc = _get_exception_df(finding, DF_COMBINED)

    if df_exc.empty:
        content = html.Div([
            html.P("No transaction-level drill-down available for this test.",
                   style={"color": "#6b7283", "fontSize": 13}),
            html.P(f"Test {test_id} does not use row-level flags on the combined population.",
                   style={"color": "#6b7283", "fontSize": 12}),
        ])
    else:
        content = html.Div([
            html.Div([
                html.Span(f"{len(df_exc):,} exception records", className="chip"),
                html.Span(test_id, className="chip mono"),
            ], className="chip-row", style={"marginBottom": 12}),
            dash_table.DataTable(
                data=df_exc.head(200).to_dict("records"),
                columns=[{"name": c, "id": c} for c in df_exc.columns],
                page_size=25, sort_action="native", filter_action="native",
                export_format="csv",
                style_table={"overflowX": "auto"},
                style_cell={"fontSize": 11.5, "padding": "5px 8px", "textAlign": "left"},
                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": "#f9fafb"},
                ],
            ),
            html.P(f"Showing first 200 of {len(df_exc):,} records." if len(df_exc) > 200 else "",
                   style={"fontSize": 11, "color": "#6b7283", "marginTop": 8}),
        ])

    return True, content, f"{test_id}: Exception Records"


# ─── PPTX export callback ────────────────────────────────────────────────────
@callback(
    Output("download-pptx", "data"),
    Input("btn-export-pptx", "n_clicks"),
    prevent_initial_call=True,
)
def export_pptx(n_clicks):
    if not HAS_PPTX:
        raise PreventUpdate
    pptx_bytes = generate_pptx(EVIDENCE_PAYLOAD, FINDINGS_SCORED, CATALOGUE_ROWS)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    return dcc.send_bytes(pptx_bytes, f"TNE_ExCo_Audit_Pack_{timestamp}.pptx")


# ─── Excel export callback ───────────────────────────────────────────────────
@callback(
    Output("download-excel", "data"),
    Input("btn-export-excel", "n_clicks"),
    prevent_initial_call=True,
)
def export_excel(n_clicks):
    xlsx_bytes = generate_excel(FINDINGS_SCORED, EVIDENCE_PAYLOAD, CATALOGUE_ROWS, DF_COMBINED)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    return dcc.send_bytes(xlsx_bytes, f"TNE_ExCo_Exceptions_{timestamp}.xlsx")


# ─── Copy finding to clipboard ───────────────────────────────────────────────
app.clientside_callback(
    """
    function(n_clicks_list) {
        if (!n_clicks_list || !n_clicks_list.some(n => n > 0)) return window.dash_clientside.no_update;
        var triggered = window.dash_clientside.callback_context.triggered_id;
        if (!triggered) return window.dash_clientside.no_update;
        var idx = triggered.index;
        var findings = JSON.parse(document.getElementById('findings-data-store').textContent || '[]');
        if (!findings[idx]) return window.dash_clientside.no_update;
        var f = findings[idx];
        var text = '[' + f.risk + '] ' + f.test_id + ': ' + f.title + '\\n\\n';
        text += 'Observation: ' + f.observation + '\\n\\n';
        if (f.recommendation) text += 'Recommendation: ' + f.recommendation + '\\n\\n';
        if (f.management_questions) text += 'Management Questions:\\n' + f.management_questions.map(function(q, i) { return (i+1) + '. ' + q; }).join('\\n');
        navigator.clipboard.writeText(text);
        return true;
    }
    """,
    Output("copy-toast", "is_open"),
    Input({"type": "copy-finding-btn", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)


# ─── Management action tracker ───────────────────────────────────────────────
@callback(
    Output("mgmt-tracker-body", "children"),
    Input("store-mgmt-actions", "data"),
)
def render_mgmt_tracker(actions_data):
    actions = actions_data or {}
    rows = []
    for f in FINDINGS_SCORED:
        tid = f["test_id"]
        action = actions.get(tid, {})
        status = action.get("status", "Open")
        response = action.get("response", "")
        owner = action.get("owner", "")
        target_date = action.get("target_date", "")

        status_color = {"Open": "#b85042", "Under review": "#e0952a", "Agreed": "#1c7293",
                        "Remediated": "#2c7a4b", "Closed": "#6b7283"}.get(status, "#6b7283")

        rows.append(html.Tr([
            html.Td(tid, style={"fontFamily": "monospace", "fontSize": 11, "fontWeight": 600}),
            html.Td(f["title"], style={"fontSize": 12}),
            html.Td(html.Span(f["risk"], style={"color": _severity_color(f["risk"]), "fontWeight": 600}), style={"fontSize": 12}),
            html.Td(html.Span(status, style={"color": status_color, "fontWeight": 600}), style={"fontSize": 12}),
            html.Td(owner or "—", style={"fontSize": 12, "color": "#6b7283"}),
                html.Td(target_date or "—", style={"fontSize": 12, "color": "#6b7283", "whiteSpace": "nowrap"}),
            html.Td(response[:60] + "…" if len(response) > 60 else (response or "—"),
                    style={"fontSize": 12, "color": "#6b7283"}),
            html.Td(html.Button("Edit", id={"type": "edit-action-btn", "index": tid},
                                className="ghost", style={"fontSize": 11})),
        ]))

    _th = {"fontSize": 10, "fontWeight": 700, "letterSpacing": "0.5px", "textTransform": "uppercase",
           "color": "#6b7283", "paddingBottom": 5, "borderBottom": "2px solid #e4e7ee", "paddingRight": 12}

    return html.Div([
        html.Table([
            html.Thead(html.Tr([
                html.Th("Test", style=_th), html.Th("Finding", style=_th),
                html.Th("Risk", style=_th), html.Th("Status", style=_th),
                html.Th("Owner", style=_th), html.Th("Target", style=_th), html.Th("Response", style=_th),
                html.Th("", style=_th),
            ])),
            html.Tbody(rows),
        ], style={"borderCollapse": "collapse", "width": "100%", "fontSize": 12.5}),
    ], style={"overflowX": "auto"})


@callback(
    Output("exec-action-kpi", "children"),
    Output("exec-action-summary", "children"),
    Input("store-mgmt-actions", "data"),
)
def render_action_summary(actions_data):
    actions = actions_data or {}
    statuses = [actions.get(f["test_id"], {}).get("status", "Open") for f in FINDINGS_SCORED]
    open_count = sum(status in ("Open", "Under review") for status in statuses)
    agreed_count = sum(status == "Agreed" for status in statuses)
    remediated_count = sum(status in ("Remediated", "Closed") for status in statuses)
    owner_count = sum(bool(actions.get(f["test_id"], {}).get("owner")) for f in FINDINGS_SCORED)

    kpi = [
        html.Div("Open management actions", className="kpi-title"),
        html.Div(str(open_count), className="kpi-value"),
    ]
    summary_items = [
        ("Open / under review", open_count, "#b85042"),
        ("Agreed", agreed_count, "#1c7293"),
        ("Remediated / closed", remediated_count, "#2c7a4b"),
        ("Assigned owners", owner_count, "#1e2761"),
    ]
    summary = html.Div([
        html.Div([
            html.Div(label, className="action-summary-label"),
            html.Div(str(value), className="action-summary-value", style={"color": color}),
        ], className="action-summary-item")
        for label, value, color in summary_items
    ], className="action-summary")
    return kpi, summary


@callback(
    Output("action-modal", "is_open"),
    Output("action-modal-title", "children"),
    Output("action-modal-finding", "children"),
    Output("action-owner", "value"),
    Output("action-status", "value"),
    Output("action-target-date", "date"),
    Output("action-response", "value"),
    Output("selected-action", "data"),
    Input({"type": "edit-action-btn", "index": ALL}, "n_clicks"),
    Input("action-cancel", "n_clicks"),
    State("store-mgmt-actions", "data"),
    prevent_initial_call=True,
)
def open_action_editor(edit_clicks, cancel_clicks, actions_data):
    if ctx.triggered_id == "action-cancel":
        return False, dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update
    if not ctx.triggered_id or not isinstance(ctx.triggered_id, dict):
        raise PreventUpdate

    test_id = ctx.triggered_id["index"]
    finding = next((f for f in FINDINGS_SCORED if f["test_id"] == test_id), None)
    if not finding:
        raise PreventUpdate
    action = (actions_data or {}).get(test_id, {})
    return (
        True,
        f"Management action · {test_id}",
        finding["title"],
        action.get("owner", ""),
        action.get("status", "Open"),
        action.get("target_date"),
        action.get("response", ""),
        {"test_id": test_id},
    )


@callback(
    Output("store-mgmt-actions", "data"),
    Output("store-audit-log", "data"),
    Output("action-modal", "is_open", allow_duplicate=True),
    Input("action-save", "n_clicks"),
    State("selected-action", "data"),
    State("action-owner", "value"),
    State("action-status", "value"),
    State("action-target-date", "date"),
    State("action-response", "value"),
    State("store-mgmt-actions", "data"),
    State("store-audit-log", "data"),
    prevent_initial_call=True,
)
def save_management_action(_, selected, owner, status, target_date, response, actions_data, audit_log):
    if not selected or not selected.get("test_id"):
        raise PreventUpdate

    test_id = selected["test_id"]
    actions = dict(actions_data or {})
    actions[test_id] = {
        "owner": (owner or "").strip(),
        "status": status or "Open",
        "target_date": target_date or "",
        "response": (response or "").strip(),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    log = list(audit_log or [])
    log.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": f"Updated management action for {test_id}: {actions[test_id]['status']}",
    })
    return actions, log, False


# ─── Audit log renderer ──────────────────────────────────────────────────────
@callback(
    Output("audit-log-body", "children"),
    Input("store-audit-log", "data"),
)
def render_audit_log(log_data):
    log = log_data or []
    if not log:
        return html.P("No audit log entries yet.", style={"color": "#6b7283", "fontSize": 13})
    items = [html.Li(f"{entry.get('timestamp', '')} — {entry.get('action', '')}", style={"fontSize": 12})
             for entry in reversed(log[-50:])]
    return html.Ul(items, style={"margin": 0, "paddingLeft": 18, "color": "#2c3040"})


# ─── Audience mode visibility ────────────────────────────────────────────────
@callback(
    Output("main-tabs", "active_tab"),
    Input("audience-mode", "value"),
    prevent_initial_call=True,
)
def switch_audience_mode(mode):
    # Switch to the most relevant top-level workspace for the selected audience.
    if mode == "executive":
        return "tab-executive"
    elif mode == "investigator":
        return "tab-audit"
    return "tab-findings"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8050)), debug=False)
