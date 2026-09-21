"""Demo fixture data for AI Audit Analyst platform.

All fixture data is clearly labelled as demo content.  Replace with
Delta-backed persistence or Unity Catalog registry queries when available.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# ── Skill definitions ────────────────────────────────────────────────────────

DEMO_SKILLS: list[dict] = [
    {
        "skill_id": "SKILL-001",
        "name": "ExCo T&E Executive Diligence",
        "domain": "Travel & Entertainment",
        "category": "Spend Compliance",
        "version": "1.2",
        "owner": "Internal Audit",
        "status": "Published",
        "last_updated": "2026-07-15",
        "last_run": "2026-08-14",
        "description": (
            "Assess executive travel and entertainment spend against policy. "
            "14 deterministic tests covering pre-approval, preferred suppliers, "
            "split claims, duplicates, missing receipts, attendee validation, "
            "approver review quality, and daily spend limits."
        ),
        "tests": 14,
        "previous_runs": 3,
        "has_workspace": True,  # existing T&E prototype is the workspace
    },
    {
        "skill_id": "SKILL-002",
        "name": "T4.8 Input GST AP Vendor Eligibility & GST Coding Review",
        "domain": "Tax Governance",
        "category": "Tax Compliance",
        "version": "2.0",
        "owner": "Internal Audit — Tax",
        "status": "Published",
        "last_updated": "2026-06-20",
        "last_run": "2026-08-10",
        "description": (
            "Review accounts payable transactions for GST input tax credit "
            "eligibility. Validate vendor ABN registration, GST coding accuracy, "
            "and tax invoice compliance against ATO requirements."
        ),
        "tests": 11,
        "previous_runs": 5,
        "has_workspace": False,
    },
    {
        "skill_id": "SKILL-003",
        "name": "Emergency Change Testing",
        "domain": "IT General Controls",
        "category": "Change Management",
        "version": "1.0",
        "owner": "Internal Audit — IT",
        "status": "Published",
        "last_updated": "2026-05-01",
        "last_run": "2026-07-22",
        "description": (
            "Assess emergency change requests for proper authorisation, "
            "retrospective approval, documentation completeness, and "
            "segregation of duties compliance."
        ),
        "tests": 8,
        "previous_runs": 2,
        "has_workspace": False,
    },
    {
        "skill_id": "SKILL-004",
        "name": "User Access Review",
        "domain": "IT General Controls",
        "category": "Access Management",
        "version": "1.1",
        "owner": "Internal Audit — IT",
        "status": "Needs review",
        "last_updated": "2026-04-10",
        "last_run": None,
        "description": (
            "Validate user access privileges across critical systems. "
            "Identify orphaned accounts, excessive privileges, segregation "
            "of duties conflicts, and dormant accounts."
        ),
        "tests": 9,
        "previous_runs": 0,
        "has_workspace": False,
    },
    {
        "skill_id": "SKILL-005",
        "name": "Software Licence Compliance",
        "domain": "Procurement",
        "category": "Asset Management",
        "version": "0.9",
        "owner": "Internal Audit — Procurement",
        "status": "Draft",
        "last_updated": "2026-03-18",
        "last_run": None,
        "description": (
            "Compare deployed software instances against purchased licence "
            "entitlements. Identify over-deployment, under-utilisation, "
            "and non-compliant installations."
        ),
        "tests": 6,
        "previous_runs": 0,
        "has_workspace": False,
    },
]

# ── Data assets (mock Unity Catalog results) ────────────────────────────────

DEMO_DATA_ASSETS: list[dict] = [
    {
        "name": "sdpt_gia.tne_exco.expense_reports",
        "type": "Table",
        "catalog": "sdpt_gia",
        "schema": "tne_exco",
        "owner": "data-engineering@optus.com.au",
        "last_refreshed": "2026-08-20",
        "classification": "Internal — Confidential",
        "description": "Concur expense report line items for ExCo members",
        "rows": 42_000,
        "access": "Available",
    },
    {
        "name": "sdpt_gia.tne_exco.approval_aging",
        "type": "Table",
        "catalog": "sdpt_gia",
        "schema": "tne_exco",
        "owner": "data-engineering@optus.com.au",
        "last_refreshed": "2026-08-20",
        "classification": "Internal — Confidential",
        "description": "Expense report approval timing and receipt viewing status",
        "rows": 8_500,
        "access": "Available",
    },
    {
        "name": "sdpt_gia.tne_exco.travel_requests",
        "type": "Table",
        "catalog": "sdpt_gia",
        "schema": "tne_exco",
        "owner": "data-engineering@optus.com.au",
        "last_refreshed": "2026-08-18",
        "classification": "Internal — Confidential",
        "description": "Pre-approved travel requests without linked expense reports",
        "rows": 900,
        "access": "Available",
    },
    {
        "name": "sdpt_gia.tax_gov.ap_transactions",
        "type": "Table",
        "catalog": "sdpt_gia",
        "schema": "tax_gov",
        "owner": "tax-data@optus.com.au",
        "last_refreshed": "2026-08-19",
        "classification": "Internal — Restricted",
        "description": "Accounts payable transactions with GST coding detail",
        "rows": 185_000,
        "access": "Request access",
    },
    {
        "name": "sdpt_gia.tax_gov.vendor_master",
        "type": "Table",
        "catalog": "sdpt_gia",
        "schema": "tax_gov",
        "owner": "tax-data@optus.com.au",
        "last_refreshed": "2026-08-15",
        "classification": "Internal — Restricted",
        "description": "Vendor master data with ABN, GST registration, and payment terms",
        "rows": 12_400,
        "access": "Request access",
    },
    {
        "name": "/Volumes/sdpt_gia/ep_temp/taxgovernance/gst_sample.csv",
        "type": "Volume file",
        "catalog": "sdpt_gia",
        "schema": "ep_temp",
        "owner": "internal-audit@optus.com.au",
        "last_refreshed": "2026-08-12",
        "classification": "Internal",
        "description": "Sample GST transaction extract for testing",
        "rows": None,
        "access": "Available",
    },
]

# ── Audit runs ───────────────────────────────────────────────────────────────

_BASE = datetime(2026, 8, 14, 9, 30, 0)

DEMO_AUDIT_RUNS: list[dict] = [
    {
        "run_id": "RUN-A1B2C3D4",
        "skill_id": "SKILL-001",
        "skill_name": "ExCo T&E Executive Diligence",
        "audit_period": "Jan 2025 – Apr 2026",
        "run_timestamp": _BASE.isoformat(),
        "run_owner": "Sugianto Lauw",
        "data_mode": "Demo",
        "status": "Completed",
        "findings_count": 14,
        "high_risk_count": 4,
        "potential_exposure": 287_450,
        "open_actions": 6,
        "last_updated": _BASE.isoformat(),
        "has_workspace": True,
    },
    {
        "run_id": "RUN-E5F6G7H8",
        "skill_id": "SKILL-002",
        "skill_name": "T4.8 Input GST AP Vendor Eligibility & GST Coding Review",
        "audit_period": "Jul 2025 – Jun 2026",
        "run_timestamp": (_BASE - timedelta(days=3)).isoformat(),
        "run_owner": "Sugianto Lauw",
        "data_mode": "Demo",
        "status": "Completed",
        "findings_count": 8,
        "high_risk_count": 2,
        "potential_exposure": 1_245_000,
        "open_actions": 3,
        "last_updated": (_BASE - timedelta(days=2)).isoformat(),
        "has_workspace": False,
    },
    {
        "run_id": "RUN-I9J0K1L2",
        "skill_id": None,
        "skill_name": "Explorer — Procurement Card Review",
        "audit_period": "Jan 2026 – Jun 2026",
        "run_timestamp": (_BASE - timedelta(days=7)).isoformat(),
        "run_owner": "Audit Analyst",
        "data_mode": "Demo",
        "status": "Needs review",
        "findings_count": 5,
        "high_risk_count": 1,
        "potential_exposure": 89_200,
        "open_actions": 2,
        "last_updated": (_BASE - timedelta(days=6)).isoformat(),
        "has_workspace": False,
    },
]

# ── Management actions (cross-Skill) ────────────────────────────────────────

DEMO_MANAGEMENT_ACTIONS: list[dict] = [
    {
        "action_id": "MA-001",
        "finding_title": "Inadequate Approver Review of Expense Reports",
        "skill_id": "SKILL-001",
        "skill_name": "ExCo T&E Executive Diligence",
        "run_id": "RUN-A1B2C3D4",
        "risk": "High",
        "owner": "CFO Office",
        "status": "Open",
        "target_date": "2026-10-31",
        "potential_exposure": 45_200,
        "last_updated": "2026-08-14",
        "evidence_link": "T6.1a",
    },
    {
        "action_id": "MA-002",
        "finding_title": "Missing Receipt Documentation",
        "skill_id": "SKILL-001",
        "skill_name": "ExCo T&E Executive Diligence",
        "run_id": "RUN-A1B2C3D4",
        "risk": "High",
        "owner": "Finance Operations",
        "status": "Under review",
        "target_date": "2026-09-30",
        "potential_exposure": 62_800,
        "last_updated": "2026-08-16",
        "evidence_link": "T4.1",
    },
    {
        "action_id": "MA-003",
        "finding_title": "Potential Split Claims to Circumvent Approval Thresholds",
        "skill_id": "SKILL-001",
        "skill_name": "ExCo T&E Executive Diligence",
        "run_id": "RUN-A1B2C3D4",
        "risk": "High",
        "owner": "Expense Policy Team",
        "status": "Agreed",
        "target_date": "2026-11-15",
        "potential_exposure": 38_900,
        "last_updated": "2026-08-18",
        "evidence_link": "T5.1",
    },
    {
        "action_id": "MA-004",
        "finding_title": "GST Input Tax Credits on Non-Eligible Vendors",
        "skill_id": "SKILL-002",
        "skill_name": "T4.8 Input GST",
        "run_id": "RUN-E5F6G7H8",
        "risk": "High",
        "owner": "Tax Governance",
        "status": "Open",
        "target_date": "2026-09-15",
        "potential_exposure": 892_000,
        "last_updated": "2026-08-12",
        "evidence_link": "T4.8.1",
    },
    {
        "action_id": "MA-005",
        "finding_title": "Vendor ABN Registration Gaps",
        "skill_id": "SKILL-002",
        "skill_name": "T4.8 Input GST",
        "run_id": "RUN-E5F6G7H8",
        "risk": "Medium",
        "owner": "Vendor Management",
        "status": "Under review",
        "target_date": "2026-10-01",
        "potential_exposure": 124_500,
        "last_updated": "2026-08-13",
        "evidence_link": "T4.8.3",
    },
    {
        "action_id": "MA-006",
        "finding_title": "Unusual Procurement Card Transactions",
        "skill_id": None,
        "skill_name": "Explorer — Procurement Card Review",
        "run_id": "RUN-I9J0K1L2",
        "risk": "Medium",
        "owner": "Procurement",
        "status": "Open",
        "target_date": "2026-10-15",
        "potential_exposure": 45_600,
        "last_updated": "2026-08-09",
        "evidence_link": "EXP-1",
    },
]

# ── Platform trace events ────────────────────────────────────────────────────

DEMO_TRACE_EVENTS: list[dict] = [
    {"event_id": "EVT-001", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:00", "stage": "Initialisation", "status": "complete", "message": "Skill SKILL-001 discovered: ExCo T&E Executive Diligence v1.2", "duration_s": 0.2},
    {"event_id": "EVT-002", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:01", "stage": "Source data", "status": "complete", "message": "8 source files selected from governed volume", "duration_s": 1.1},
    {"event_id": "EVT-003", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:03", "stage": "Data profiling", "status": "complete", "message": "Data profiling completed — 4,200 expense records, 850 approval records, 900 travel requests", "duration_s": 3.4},
    {"event_id": "EVT-004", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:07", "stage": "Data quality", "status": "complete", "message": "Data quality assessment: Good — no blocking issues", "duration_s": 1.8},
    {"event_id": "EVT-005", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:09", "stage": "Plan", "status": "complete", "message": "Playbook plan confirmed — 14 tests from Skill v1.2", "duration_s": 0.3},
    {"event_id": "EVT-006", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:10", "stage": "Tests", "status": "complete", "message": "14 deterministic audit tests executed", "duration_s": 8.2},
    {"event_id": "EVT-007", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:19", "stage": "Exceptions", "status": "complete", "message": "Exception classification completed — 847 exceptions across 6 risk categories", "duration_s": 2.1},
    {"event_id": "EVT-008", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:22", "stage": "Findings", "status": "complete", "message": "14 evidence-linked findings generated — 4 High, 6 Medium, 4 Low", "duration_s": 1.5},
    {"event_id": "EVT-009", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:24", "stage": "Insights", "status": "complete", "message": "Risk prioritisation and exposure quantification completed", "duration_s": 0.9},
    {"event_id": "EVT-010", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:25", "stage": "Actions", "status": "complete", "message": "6 management actions drafted for review", "duration_s": 0.4},
    {"event_id": "EVT-011", "run_id": "RUN-A1B2C3D4", "timestamp": "2026-08-14T09:30:26", "stage": "Exports", "status": "complete", "message": "PPTX and Excel exports prepared", "duration_s": 2.3},
    {"event_id": "EVT-012", "run_id": "RUN-E5F6G7H8", "timestamp": "2026-08-11T14:05:00", "stage": "Initialisation", "status": "complete", "message": "Skill SKILL-002 discovered: T4.8 Input GST v2.0", "duration_s": 0.2},
    {"event_id": "EVT-013", "run_id": "RUN-E5F6G7H8", "timestamp": "2026-08-11T14:05:01", "stage": "Source data", "status": "complete", "message": "AP transaction and vendor master tables selected", "duration_s": 0.8},
    {"event_id": "EVT-014", "run_id": "RUN-E5F6G7H8", "timestamp": "2026-08-11T14:05:03", "stage": "Data profiling", "status": "complete", "message": "185,000 AP transactions and 12,400 vendors profiled", "duration_s": 5.1},
    {"event_id": "EVT-015", "run_id": "RUN-E5F6G7H8", "timestamp": "2026-08-11T14:05:09", "stage": "Tests", "status": "complete", "message": "11 GST eligibility and coding tests executed", "duration_s": 12.4},
    {"event_id": "EVT-016", "run_id": "RUN-E5F6G7H8", "timestamp": "2026-08-11T14:05:22", "stage": "Findings", "status": "complete", "message": "8 findings generated — 2 High, 4 Medium, 2 Low", "duration_s": 1.8},
    {"event_id": "EVT-017", "run_id": "RUN-I9J0K1L2", "timestamp": "2026-08-07T11:20:00", "stage": "Initialisation", "status": "complete", "message": "Explorer mode — no Skill selected", "duration_s": 0.1},
    {"event_id": "EVT-018", "run_id": "RUN-I9J0K1L2", "timestamp": "2026-08-07T11:20:02", "stage": "Plan", "status": "awaiting_confirmation", "message": "Proposed approach requires auditor confirmation", "duration_s": None},
]
