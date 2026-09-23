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
     "threshold": "0", "flag": "RF_CS_MissingReceipt"},
    {"test_id": "T5.1", "category": "Split claims", "test_name": "Split claims same day",
     "threshold": "n/a", "flag": "RF_CS_SplitClaims_SameDay"},
]

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
    return {**_SKILL, "tests": _TEST_CATALOGUE}


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
    return []


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
        "metrics": {},
        "test_results": run.get("test_results", []),
        "findings": run.get("findings", []),
        "reconciliation": None,
        "exposure": {"headline": None, "basis": None},
    }


def get_run_frames(ctx, run_id) -> dict:
    df = pd.DataFrame({
        "Employee": ["Alice", "Bob", "Alice"],
        "Transaction Date": ["2025-02-01", "2025-02-03", "2025-03-01"],
        "Expense Type": ["Airfare", "Meals", "Meals"],
        "Vendor": ["Qantas", "Cafe", "Cafe"],
        "Expense Amount (reimbursement currency)": [1200.0, 45.0, 60.0],
        "RF_CS_MissingReceipt": [1, 0, 1],
    })
    return {"expense_report": df}


def get_export(ctx, run_id, kind: str):
    if run_id not in ctx.runs:
        raise KeyError(run_id)
    if kind == "pptx":
        raise NotImplementedError("PPTX export arrives in P6")
    return f"{run_id}.{kind}", b"fake-bytes"
