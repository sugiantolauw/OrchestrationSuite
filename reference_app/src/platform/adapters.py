"""Service adapter interfaces and mock implementations.

Each adapter function defines a clean boundary between the UI and backend
services. Mock implementations return fixture data so the app runs in demo
mode without live integrations.  Replace individual adapters with real
implementations (LangGraph, Unity Catalog, Delta, Jira) when available.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from src.platform.fixtures import (
    DEMO_SKILLS,
    DEMO_DATA_ASSETS,
    DEMO_AUDIT_RUNS,
    DEMO_MANAGEMENT_ACTIONS,
    DEMO_TRACE_EVENTS,
)


# ── Adapter state ────────────────────────────────────────────────────────────

_LIVE_MODE = False  # toggled when a real backend is detected


def is_demo_mode() -> bool:
    return not _LIVE_MODE


def get_environment_label() -> str:
    return "Demo" if is_demo_mode() else "Non-production"


# ── Skill registry ───────────────────────────────────────────────────────────

def list_skills(filters: dict | None = None) -> list[dict]:
    """Return available Skill definitions."""
    skills = DEMO_SKILLS
    if filters:
        domain = filters.get("domain")
        status = filters.get("status")
        if domain:
            skills = [s for s in skills if s["domain"] == domain]
        if status:
            skills = [s for s in skills if s["status"] == status]
    return skills


def get_skill(skill_id: str) -> dict | None:
    for s in DEMO_SKILLS:
        if s["skill_id"] == skill_id:
            return s
    return None


# ── Governed data discovery ──────────────────────────────────────────────────

def search_governed_data(query: str) -> list[dict]:
    """Search Unity Catalog for governed data assets. Mock implementation."""
    if not query:
        return DEMO_DATA_ASSETS
    q = query.lower()
    return [a for a in DEMO_DATA_ASSETS if q in a["name"].lower() or q in a.get("description", "").lower()]


# ── File upload ──────────────────────────────────────────────────────────────

_UPLOAD_BASE = "/Volumes/sdpt_gia/ep_temp/taxgovernance"


def upload_audit_file(filename: str, content: bytes, run_id: str) -> dict:
    """Upload a file to a governed Volume. Mock implementation."""
    dest = f"{_UPLOAD_BASE}/runs/{run_id}/input/{filename}"
    return {
        "filename": filename,
        "size_bytes": len(content),
        "destination": dest,
        "status": "Uploaded",
        "validation": "Profiling",
        "timestamp": datetime.now().isoformat(),
        "mock": True,
    }


def get_upload_base_path() -> str:
    return _UPLOAD_BASE


# ── Run lifecycle ────────────────────────────────────────────────────────────

def profile_sources(run_config: dict) -> dict:
    """Profile selected data sources. Mock implementation."""
    return {
        "status": "complete",
        "sources_profiled": len(run_config.get("data_assets", [])) + len(run_config.get("uploaded_files", [])),
        "quality": "Good",
        "mock": True,
    }


def propose_plan(run_config: dict) -> dict:
    """Generate an execution plan for auditor review. Mock implementation."""
    skill = run_config.get("skill")
    mode = run_config.get("mode", "playbook")
    stages = [
        {"stage": "Source data", "status": "ready", "detail": f"{run_config.get('sources_count', 0)} sources selected"},
        {"stage": "Data quality & reconciliation", "status": "pending"},
        {"stage": "Skill / Explorer plan", "status": "ready" if mode == "playbook" else "needs_confirmation",
         "detail": skill["name"] if skill and mode == "playbook" else "Auditor confirmation required"},
        {"stage": "Deterministic audit tests", "status": "pending",
         "detail": f"{skill['tests'] if skill else '?'} tests defined"},
        {"stage": "Exception classification", "status": "pending"},
        {"stage": "Evidence-linked findings", "status": "pending"},
        {"stage": "Insights & prioritisation", "status": "pending"},
        {"stage": "Management actions", "status": "pending"},
        {"stage": "Export & Jira preview", "status": "pending"},
    ]
    return {"stages": stages, "mode": mode, "mock": True}


def start_audit_run(run_config: dict) -> dict:
    """Start an audit run. Mock implementation returns a run_id."""
    run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"
    return {
        "run_id": run_id,
        "status": "Running",
        "started_at": datetime.now().isoformat(),
        "mock": True,
    }


def get_run_status(run_id: str) -> dict:
    """Get status of a running audit. Mock implementation."""
    for run in DEMO_AUDIT_RUNS:
        if run["run_id"] == run_id:
            return run
    return {"run_id": run_id, "status": "Not found"}


def load_run(run_id: str) -> dict | None:
    """Load a completed run's full results. Mock returns demo data."""
    for run in DEMO_AUDIT_RUNS:
        if run["run_id"] == run_id:
            return run
    return None


# ── Audit runs ───────────────────────────────────────────────────────────────

def list_audit_runs(filters: dict | None = None) -> list[dict]:
    """Return persisted audit runs."""
    runs = DEMO_AUDIT_RUNS
    if filters:
        skill_id = filters.get("skill_id")
        status = filters.get("status")
        if skill_id:
            runs = [r for r in runs if r["skill_id"] == skill_id]
        if status:
            runs = [r for r in runs if r["status"] == status]
    return runs


# ── Management actions ───────────────────────────────────────────────────────

def list_management_actions(filters: dict | None = None) -> list[dict]:
    """Return management actions across all Skills and runs."""
    actions = DEMO_MANAGEMENT_ACTIONS
    if filters:
        skill_id = filters.get("skill_id")
        risk = filters.get("risk")
        status = filters.get("status")
        owner = filters.get("owner")
        if skill_id:
            actions = [a for a in actions if a.get("skill_id") == skill_id]
        if risk:
            actions = [a for a in actions if a.get("risk") == risk]
        if status:
            actions = [a for a in actions if a.get("status") == status]
        if owner:
            actions = [a for a in actions if owner.lower() in a.get("owner", "").lower()]
    return actions


# ── Platform trace ───────────────────────────────────────────────────────────

def list_trace_events(run_id: str | None = None) -> list[dict]:
    """Return observable execution events. No chain-of-thought content."""
    events = DEMO_TRACE_EVENTS
    if run_id:
        events = [e for e in events if e.get("run_id") == run_id]
    return events


# ── Jira integration ────────────────────────────────────────────────────────

def create_jira_preview(run_id: str) -> dict:
    """Generate a Jira ticket preview for approval. Mock implementation."""
    run = load_run(run_id)
    if not run:
        return {"status": "error", "message": "Run not found"}
    return {
        "run_id": run_id,
        "tickets": [
            {
                "summary": f"[{run.get('skill_name', 'Audit')}] Finding requires remediation",
                "priority": "High",
                "assignee": "Unassigned",
                "status": "Preview — not submitted",
            }
        ],
        "approval_required": True,
        "mock": True,
    }
