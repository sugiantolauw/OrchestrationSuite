"""Service adapter — thin calls into orchestrator.service.

This replaces the reference prototype's mock adapters (src/platform/adapters.py
in reference_app/). There is no `_LIVE_MODE` fakery here: the environment label
comes from the connected backend (orchestrator.service.health), and "demo" is
never shown except on a specific run whose own `data_mode` says so
(CLAUDE.md §9, §3 NN13). Every function here is a pass-through to
orchestrator.service — no data is computed, cached beyond one process-lifetime
context, or fabricated in this module.
"""

from __future__ import annotations

import os
from typing import Any

try:
    from orchestrator import service
except ImportError:  # orchestrator.service is being built alongside this app
    # (see app/'s task brief). Left unset rather than faked: any call below
    # fails loudly with a clear AttributeError until the real module lands,
    # never silently. app/tests substitutes a FakeService for `service` via
    # monkeypatch so app/'s own logic can be tested without it.
    service = None  # type: ignore[assignment]

_ctx: Any = None


def get_context():
    """Builds (once per process) and returns the AppContext used by every
    call below. Building it lazily means importing this module never talks
    to a workspace; only using it does."""
    global _ctx
    if _ctx is None:
        _ctx = service.build_app_context(env=os.environ)
    return _ctx


class MissingIdentityHeader(Exception):
    """CLAUDE.md §9A.1 / P2/P3 gate review item 7: the deployed (non-local)
    backend requires a verified identity (X-Forwarded-Email / X-Forwarded-
    User, set by the platform's front door) for run_owner/actor -- it must
    never silently fall back to "local-user" the way the local backend does.
    Raised by run_setup.py/run_status.py's identity helpers, caught by their
    callbacks and shown as a blocking error, never swallowed into a default."""


# ── Environment label ────────────────────────────────────────────────────────

def get_environment_label() -> str:
    """What backend this process is actually talking to. Never the word
    'Demo' unless the backend itself reports demo/local data (CLAUDE.md §9).
    orchestrator.service.ready()'s `backend` field is the same "local" | "uc"
    value service.list_runs() uses to set each run's own `data_mode`."""
    ctx = get_context()
    r = service.ready(ctx)
    return "Local test data" if r.get("backend") == "local" else "Unity Catalog"


def is_local_backend() -> bool:
    """True only when this process is actually talking to the local backend
    (LocalPersistence/local file sources) -- the one place run_owner/actor
    may fall back to the "local-user" label without a verified identity
    header (CLAUDE.md §9A.1, P2/P3 gate review item 7)."""
    return service.ready(get_context()).get("backend") == "local"


# ── Skill registry ───────────────────────────────────────────────────────────

def list_skills(filters: dict | None = None) -> list[dict]:
    skills = service.list_skills(get_context())
    if filters:
        domain = filters.get("domain")
        status = filters.get("status")
        if domain:
            skills = [s for s in skills if s.get("domain") == domain]
        if status:
            skills = [s for s in skills if s.get("status") == status]
    return skills


def get_skill(skill_id: str) -> dict | None:
    return service.get_skill(get_context(), skill_id)


# ── Governed data discovery ──────────────────────────────────────────────────

def list_governed_tables() -> list[dict]:
    return service.list_governed_tables(get_context())


def suggest_bindings(skill_id: str) -> dict:
    return service.suggest_bindings(get_context(), skill_id)


# ── Run lifecycle ────────────────────────────────────────────────────────────

def start_audit_run(
    *,
    skill_id: str,
    bindings: dict,
    audit_period: tuple[str, str],
    objective: str,
    run_owner: str,
    mode: str = "playbook",
    review_plan_first: bool = False,
    engagement_id: str = "ENG-DEFAULT",
) -> str:
    return service.start_audit_run(
        get_context(),
        skill_id=skill_id,
        bindings=bindings,
        audit_period=audit_period,
        objective=objective,
        run_owner=run_owner,
        mode=mode,
        review_plan_first=review_plan_first,
        engagement_id=engagement_id,
    )


def get_run(run_id: str) -> dict | None:
    from orchestrator.errors import RunNotFound
    try:
        return service.get_run(get_context(), run_id)
    except RunNotFound:
        return None


def confirm_plan(run_id: str, actor: str) -> None:
    service.confirm_plan(get_context(), run_id, actor)


def sign_off(run_id: str, actor: str) -> None:
    service.sign_off(get_context(), run_id, actor)


def resume_run(run_id: str, actor: str) -> None:
    service.resume_run(get_context(), run_id, actor)


def get_run_payload(run_id: str) -> dict:
    return service.get_run_payload(get_context(), run_id)


def get_run_frames(run_id: str) -> dict:
    return service.get_run_frames(get_context(), run_id)


def get_export(run_id: str, kind: str):
    return service.get_export(get_context(), run_id, kind)


# ── Audit runs ───────────────────────────────────────────────────────────────

def list_audit_runs(filters: dict | None = None) -> list[dict]:
    return service.list_runs(get_context(), filters=filters)


# ── Management actions ───────────────────────────────────────────────────────

def list_management_actions(filters: dict | None = None) -> list[dict]:
    return service.list_management_actions(get_context(), filters=filters)


# ── Platform trace ───────────────────────────────────────────────────────────

def list_trace_events(run_id: str | None = None) -> list[dict]:
    return service.list_trace_events(get_context(), run_id=run_id)


# ── Jira integration ────────────────────────────────────────────────────────

def create_jira_preview(run_id: str) -> dict:
    """Jira submission is not built (CLAUDE.md §8). Always a labelled preview,
    never a fake successful integration (NN13)."""
    return {
        "run_id": run_id,
        "tickets": [],
        "approval_required": True,
        "status": "Preview — not submitted",
    }
