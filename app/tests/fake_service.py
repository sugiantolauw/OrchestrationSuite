"""A minimal, self-contained stand-in for orchestrator.service, used only by
app/tests. It exists because orchestrator/service.py is being built by a
different agent in this repo — app/ is written against the documented
contract (see the task brief), and this fixture lets app/'s own routing,
validation and callback logic be exercised without a live backend or a
Databricks workspace. It is NOT used by app/app.py itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pandas as pd

_SKILL = {
    "skill_id": "SKILL-001",
    "name": "ExCo T&E Executive Diligence",
    "domain": "Travel & Entertainment",
    "version": "1.2",
    "owner": "Internal Audit",
    "status": "Published",
    "tests": 2,
    "previous_runs": 0,
    "has_workspace": True,
    "sources": [
        {"source": "expense_report", "columns": ["Employee", "Transaction Date"]},
        {"source": "attendee_validity", "columns": ["Employee"]},
    ],
}

_TEST_CATALOGUE = [
    {"test_id": "T4.1", "category": "Documentation", "test_name": "Missing receipts",
     "threshold": "0", "flag": "RF_CS_MissingReceipt",
     "plan_tests": [{"test_id": "T4.1", "flag": "RF_CS_MissingReceipt", "primitive": "anti_join_gap"}]},
    {"test_id": "T5.1", "category": "Split claims", "test_name": "Split claims same day",
     "threshold": "n/a", "flag": "RF_CS_SplitClaims_SameDay",
     "plan_tests": [{"test_id": "T5.1", "flag": "RF_CS_SplitClaims_SameDay", "primitive": "split_detection"}]},
]

_FLAG_TO_TEST = {"RF_CS_MissingReceipt": "T4.1", "RF_CS_SplitClaims_SameDay": "T5.1"}

_GOVERNED_TABLES = [
    {"name": "expense_report", "table_fqn": "test_catalog.tne_source.expense_report",
     "type": "Local file", "exists": True},
    {"name": "attendee_validity", "table_fqn": "test_catalog.tne_source.attendee_validity",
     "type": "Local file", "exists": True},
    {"name": "secret_table", "table_fqn": "test_catalog.restricted.secret_table",
     "type": "Local file", "exists": True, "restricted": True},
]


class _FakeExecutor:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


@dataclass
class FakeAppContext:
    executor: _FakeExecutor = field(default_factory=_FakeExecutor)
    runs: dict = field(default_factory=dict)


def build_app_context(env=None) -> FakeAppContext:
    return FakeAppContext()


def health(ctx) -> dict:
    return {"status": "ok"}


def ready(ctx) -> dict:
    return {"ready": True, "backend": "local", "detail": None}


def list_skills(ctx) -> list:
    return [_SKILL]


def get_skill(ctx, skill_id: str):
    if skill_id != _SKILL["skill_id"]:
        return None
    return {**_SKILL, "tests": _TEST_CATALOGUE, "flag_to_test": _FLAG_TO_TEST}


def list_governed_tables(ctx) -> list:
    return _GOVERNED_TABLES


def suggest_bindings(ctx, skill_id: str) -> dict:
    return {"expense_report": "test_catalog.tne_source.expense_report", "attendee_validity": None}


def _make_findings(run_id: str) -> list:
    return [
        {
            "finding_id": f"{run_id}:T4_1",
            "rule_id": "SKILL-001.T4_1",
            "test_id": "T4.1",
            "title": "Missing Receipt Documentation",
            "severity": "High",
            "severity_basis": "threshold",
            "analyst_set_severity": True,
            "threshold_refs": [],
            "metrics_cited": {
                "missing_receipt_count": {"value": 12, "unit": "count", "source_ref": "expense_report@v1"},
                "missing_receipt_pct": {"value": 8.5, "unit": "%", "source_ref": "expense_report@v1"},
            },
            "observation": "12 claims (8.5% of total) are missing receipt documentation.",
            "recommendation": "Enforce mandatory receipt attachment.",
            "management_questions": ["What is the current policy for claims without receipts?"],
            "control_id": "CTL-TNE-04",
            "risk_id": "RSK-TNE-04",
            "assertion": "operating",
            "exposure_amount": None,
            "exposure_basis": "pending P3 de-duplicated exposure",
            "review_state": "draft",
        },
    ]


def start_audit_run(
    ctx,
    *,
    skill_id,
    bindings,
    audit_period,
    objective,
    run_owner,
    mode="playbook",
    review_plan_first=False,
    engagement_id="ENG-DEFAULT",
) -> str:
    run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"
    status = "awaiting_confirmation" if review_plan_first else "awaiting_signoff"
    ctx.runs[run_id] = {
        "run_id": run_id,
        "skill_id": skill_id,
        "skill_version": "1.2",
        "bindings": bindings,
        "audit_period": list(audit_period),
        "objective": objective,
        "run_owner": run_owner,
        "mode": mode,
        "engagement_id": engagement_id,
        "status": status,
        "status_label": status.replace("_", " ").title(),
        "status_reason": None,
        "state_version": 1,
        "progress": {"node_index": 2, "total_nodes": 9, "current_stage": "plan"},
        "findings": _make_findings(run_id),
        "test_results": [
            {"test_id": "T4.1", "status": "exception", "reason": None, "exception_units": 12},
            {"test_id": "T5.1", "status": "pass", "reason": None, "exception_units": 0},
        ],
        "signoff": None,
    }
    return run_id


def get_run(ctx, run_id: str):
    return ctx.runs.get(run_id)


def list_runs(ctx, filters=None) -> list:
    runs = list(ctx.runs.values())
    out = []
    for r in runs:
        out.append({
            "run_id": r["run_id"],
            "skill_id": r["skill_id"],
            "skill_name": _SKILL["name"],
            "status": "Completed" if r["status"] == "completed" else r["status_label"],
            "run_owner": r["run_owner"],
            "last_updated": "2026-09-23T00:00:00",
            "run_timestamp": "2026-09-23T00:00:00",
            "findings_count": len(r["findings"]),
            "high_risk_count": sum(1 for f in r["findings"] if f.get("severity") == "High"),
            "open_actions": 0,
            "potential_exposure": 0,
            "data_mode": "demo",
        })
    if filters:
        skill_id = filters.get("skill_id")
        if skill_id:
            out = [r for r in out if r["skill_id"] == skill_id]
    return out


def list_trace_events(ctx, run_id=None) -> list:
    events = [{"event_id": "EVT-1", "run_id": rid, "timestamp": "2026-09-23T00:00:00",
               "stage": "plan", "status": "complete", "message": "fake event", "duration_s": 0.1}
              for rid in ctx.runs]
    if run_id:
        events = [e for e in events if e["run_id"] == run_id]
    return events


def list_management_actions(ctx, filters=None) -> list:
    out = []
    for run_id, run in ctx.runs.items():
        for f in run.get("findings", []):
            out.append({
                "action_id": f"MA-{f['finding_id']}",
                "finding_id": f["finding_id"],
                "finding_title": f["title"],
                "run_id": run_id,
                "skill_id": run.get("skill_id"),
                "risk": f.get("severity"),
                "owner": None,
                "status": "draft",
                "target_date": None,
                "potential_exposure": f.get("exposure_amount"),
                "evidence_link": f.get("test_id"),
                "description": f.get("recommendation"),
                "last_updated": "2026-09-23T00:00:00",
            })
    if filters:
        run_id = filters.get("run_id")
        if run_id:
            out = [a for a in out if a.get("run_id") == run_id]
    return out


def confirm_plan(ctx, run_id, actor) -> None:
    run = ctx.runs[run_id]
    run["status"] = "awaiting_signoff"
    run["status_label"] = "Awaiting Signoff"
    run["state_version"] += 1


def sign_off(ctx, run_id, actor) -> None:
    run = ctx.runs[run_id]
    run["status"] = "completed"
    run["status_label"] = "Completed"
    run["signoff"] = {"approver": actor, "timestamp": "2026-09-23T00:00:00"}
    run["state_version"] += 1


def resume_run(ctx, run_id, actor) -> None:
    run = ctx.runs[run_id]
    run["status"] = "completed"
    run["status_label"] = "Completed"
    run["state_version"] += 1


def get_run_payload(ctx, run_id) -> dict:
    run = ctx.runs.get(run_id, {})
    return {
        "run_id": run_id,
        "status": run.get("status"),
        "metrics": {
            "total_records": {"value": 12, "unit": "count", "source_ref": {}},
            "total_files": {"value": 2, "unit": "count", "source_ref": {}},
            "months_covered": {"value": 5, "unit": "count", "source_ref": {}},
            "claims_prepared_rows": {"value": 5, "unit": "count", "source_ref": {}},
            "claims_prepared_amount": {"value": 3000.0, "unit": "AUD", "source_ref": {}},
            "claims_approved_rows": {"value": 4, "unit": "count", "source_ref": {}},
            "claims_approved_amount": {"value": 2500.0, "unit": "AUD", "source_ref": {}},
            "claims_combined_rows": {"value": 6, "unit": "count", "source_ref": {}},
            "claims_combined_amount": {"value": 6825.0, "unit": "AUD", "source_ref": {}},
        },
        "test_results": run.get("test_results", []),
        "findings": run.get("findings", []),
        "reconciliation": {
            "expense_report": {"engine_rows": 6, "independent_rows": 6, "variance": 0,
                               "amount": 6825.0, "min_date": "2025-02-01", "max_date": "2025-06-15"},
        },
        "exposure": {"headline": 1725.0, "basis": "sum of amount over the union of distinct flagged rows"},
    }


def get_run_frames(ctx, run_id) -> dict:
    """Realistic multi-source frames -- real contract column names
    (skills/tne_exco/contract.yaml) and RF_* flags from the real Skill's
    plan.yaml, so app/'s own workspace_tne.py exercises the same flag ->
    catalogue-test mapping it uses against a live backend."""
    expense = pd.DataFrame({
        "Employee": ["Alice Wu", "Bob Chen", "Alice Wu", "Carol Ng", "Bob Chen", "Alice Wu"],
        "Transaction Date": ["2025-02-01", "2025-02-03", "2025-03-01", "2025-03-15", "2025-04-02", "2025-06-15"],
        "Expense Type": ["Airfares - Domestic Travel", "Meals - Domestic Travel", "Meals - Domestic Travel",
                          "Staff/Client Function: Offsite Food/Drink", "Accommodation - Domestic Travel",
                          "Meals - Domestic Travel"],
        "Vendor": ["Qantas", "Cafe One", "Cafe One", "The Grill", "Hilton", "Cafe Two"],
        "Expense Amount (reimbursement currency)": [1200.0, 45.0, 60.0, 5500.0, 420.0, 1600.0],
        "RF_CS_MissingReceipt": [1, 0, 1, 0, 0, 1],
        "RF_CS_Reimbursement_GT_5K": [0, 0, 0, 1, 0, 0],
        "RF_ATT_Missing": [0, 0, 0, 1, 0, 0],
    })
    approval = pd.DataFrame({
        "Report ID": ["R-1", "R-2", "R-3", "R-4"],
        "Approver ID": [101, 102, 101, 103],
        "Approver Name": ["Dana Price", "Evan Cole", "Dana Price", "Farah Khan"],
        "Step": ["Manager", "Manager", "Finance", "Manager"],
        "Approved Date/Time": ["2025-02-02T09:00:00", "2025-02-04T14:00:00",
                                "2025-03-02T08:00:00", "2025-04-03T11:00:00"],
        "Approver Received Date": ["2025-02-02T08:55:00", "2025-02-04T13:00:00",
                                    "2025-03-02T07:00:00", "2025-04-03T10:00:00"],
        "Report Receipt Viewed": ["Y", "N", "Y", "N"],
        "All Entry Receipts Viewed": ["N", "N", "Y", "N"],
        "Minutes of Approval from Receipt View": [5, 0, 60, 0],
        "Receipts Viewed Date": ["2025-02-02T08:56:00", None, "2025-03-02T06:00:00", None],
        "RF_APR_InsufficientReview": [0, 1, 0, 1],
    })
    travel_requests = pd.DataFrame({
        "Employee": ["Alice Wu", "Bob Chen"],
        "Travel Request ID": ["TR-1", "TR-2"],
        "Approval Status": ["Approved", "Approved"],
        "Start Date": ["2025-01-20", "2025-01-28"],
        "Total Approved Amount (rpt)": [1500.0, 300.0],
        "RF_PRE_Unlinked": [0, 0],
    })
    booking = pd.DataFrame({
        "Lead Traveller Name": ["Alice Wu", "Bob Chen"],
        "Booking ID": [9001, 9002],
        "Dom | Int": ["Domestic", "Domestic"],
        "Advance Purchase Days": [2, 21],
        "Booking Status": ["Confirmed", "Confirmed"],
        "Depart Date": ["2025-01-30", "2025-02-01"],
        "RF_CS_LateBooking": [1, 0],
    })
    return {
        "expense_report": expense,
        "approval_aging": approval,
        "travel_requests_no_expense": travel_requests,
        "booking_detail": booking,
    }


def get_export(ctx, run_id, kind: str):
    if run_id not in ctx.runs:
        raise KeyError(run_id)
    if kind == "pptx":
        raise NotImplementedError("PPTX export arrives in P6")
    return f"{run_id}.{kind}", b"fake-bytes"
