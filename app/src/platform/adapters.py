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


def is_demo_mode() -> bool:
    """The prototype's `is_demo_mode()` meant "not connected to a live
    backend, everything on screen is a fixture" (reference_app/src/platform/
    adapters.py: `_LIVE_MODE = False`). This process is always connected to
    a real, working backend (local test data or Unity Catalog) -- never the
    fabricated-data sense the prototype's demo_indicator text describes
    (CLAUDE.md NN13) -- so it is always False here."""
    return False


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


def list_skill_versions(skill_id: str) -> list[dict]:
    return service.list_skill_versions(get_context(), skill_id)


def get_skill_version_plan(skill_id: str, version: str) -> dict | None:
    return service.get_skill_version_plan(get_context(), skill_id, version)


# ── Governed data discovery ──────────────────────────────────────────────────

def list_governed_tables() -> list[dict]:
    return service.list_governed_tables(get_context())


def suggest_bindings(skill_id: str) -> dict:
    return service.suggest_bindings(get_context(), skill_id)


def search_governed_data(query: str, limit: int | None = None) -> list[dict]:
    """Real Unity Catalog / local-source discovery (orchestrator.service.
    list_data_asset_cards), filtered by `query` against the fully-qualified
    name and comment -- the same fields the prototype's mock filtered on
    (name, description). `limit`, when given, is the number of cards the
    caller is actually about to render -- row count and UC tag
    classification are only fetched for that many (CLAUDE.md §5 UI item 3),
    never for every matching table."""
    return service.list_data_asset_cards(get_context(), query=query, limit=limit)


# ── File upload ──────────────────────────────────────────────────────────────

def get_upload_base_path() -> str:
    return service.get_upload_base_path(get_context())


def upload_audit_file(filename: str, content: bytes, uploaded_by: str,
                       engagement_id: str = "ENG-DEFAULT") -> dict:
    return service.upload_file(
        get_context(), filename=filename, content=content, uploaded_by=uploaded_by,
        engagement_id=engagement_id,
    )


def list_uploaded_files(engagement_id: str | None = None) -> list[dict]:
    return service.list_uploaded_files(get_context(), engagement_id)


# ── Workflow preview ─────────────────────────────────────────────────────────

def propose_plan(run_config: dict) -> dict:
    """`run_config` mirrors the prototype adapter's own call shape
    ({"mode", "skill", "sources_count"}) -- only `mode` and the skill's
    `skill_id` are actually needed against the real pipeline's node
    sequence (orchestrator.nodes.fieldwork.NODES_FOR)."""
    skill = run_config.get("skill") or {}
    return service.propose_plan(
        get_context(), skill_id=skill.get("skill_id"), mode=run_config.get("mode", "playbook")
    )


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
    business_unit: str | None = None,
    materiality: float | None = None,
    generate_management_actions: bool = True,
    jira_preview_requested: bool = False,
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
        business_unit=business_unit,
        materiality=materiality,
        generate_management_actions=generate_management_actions,
        jira_preview_requested=jira_preview_requested,
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


# ── P6 narration review (docs/specs/P6_narration_design.md §7 UI-1/UI-2/
# UI-7) ──────────────────────────────────────────────────────────────────

_EMPTY_NARRATION_REVIEW = {
    "narration_enabled": False, "degraded_label": None,
    "findings": [], "themes": [], "exec_summary": None,
    "candidates": [], "undecided_candidate_ids": [],
}


def get_narration_review(run_id: str) -> dict | None:
    """Resolves this run's model-written text and AI-proposed candidates for
    the `/run/<id>` review panel (UI-1/UI-2/UI-7). No `orchestrator.service`
    read-model exists for this yet (`decide_candidate`/`edit_narrative`/
    `regenerate_narration`/`sign_off` are the WRITE side; `get_run` returns
    only `RunState`, which never carries prose or candidates) -- built here,
    read-only, from the same primitives the export node uses:
    `ctx.persistence.list_findings`/`get_narratives`/`list_candidates`/
    `list_themes`/`get_run_metrics` for the data, and
    `orchestrator.narration.resolve.effective_prose`/`metrics_placeholder_
    table` (CLAUDE.md §3 NN12) to turn a narrative row into `{text, status,
    label, sources}`. One shared placeholder table (every run metric) is
    used for every resolution call: `render()` only looks up the
    placeholders actually present in a given piece of text, so a table
    carrying more entries than one item cites is harmless -- and this avoids
    needing the Skill-dependent `build_finding_table`/`build_theme_table`/
    `run_values` builders (§4.6) just to show a reviewer what the model
    wrote.

    The fake backend (app/tests/fake_service.py) has no `.persistence` to
    read candidates/narratives from -- there is genuinely nothing to review
    there (a fake run never had narration), not a hidden default (CLAUDE.md
    NN14), so that case returns the same empty, "narration off" shape a real
    run with NARRATION_ENABLED=false would."""
    from orchestrator.catalogue_counts import catalogue_tests_for_skill
    from orchestrator.errors import RunNotFound
    from orchestrator.narration import resolve as narration_resolve
    from orchestrator.narration.run_values import run_values as narration_run_values

    ctx = get_context()
    persistence = getattr(ctx, "persistence", None)
    if persistence is None:
        return dict(_EMPTY_NARRATION_REVIEW)
    try:
        state = persistence.load_state(run_id)
    except RunNotFound:
        return None

    findings = persistence.list_findings(run_id)
    metrics = persistence.get_run_metrics(run_id)
    candidates = persistence.list_candidates(run_id)
    themes_rows = persistence.list_themes(run_id)
    narratives_by_target = {
        (r["target_kind"], r["target_id"], r["field"]): r for r in persistence.get_narratives(run_id)
    }
    # One shared placeholder table for every resolution call below (see the
    # docstring): every run metric by name, plus the `run_*` derived
    # entries (finding/test counts, the exposure headline, the audit
    # period) that only the exec summary cites -- `run_values` needs this
    # run's Skill only to compute test counts at catalogue grain rather
    # than the plan's larger sub-test grain; `[]` (no Skill resolved, e.g.
    # an unconfirmed Explorer run) still gives a correct, just coarser,
    # count.
    skill = service.resolve_run_skill(ctx, state)
    catalogue_tests = catalogue_tests_for_skill(skill) if skill else []
    run_table = narration_run_values(state, findings, metrics, catalogue_tests=catalogue_tests)
    table = {**narration_resolve.metrics_placeholder_table(list(metrics.keys()), metrics), **run_table}
    generation = int((state.options or {}).get("narration_generation", 0) or 0)

    def _resolve(*, target_kind: str, target_id: str, field: str, fallback_text=None, is_list: bool = False) -> dict:
        row = narratives_by_target.get((target_kind, target_id, field))
        resolved = narration_resolve.effective_prose(
            target_kind=target_kind, target_id=target_id, field=field,
            narratives_by_target=narratives_by_target, table=table,
            fallback_text=fallback_text, is_list=is_list,
        )
        resolved["narrative_id"] = row["narrative_id"] if row else None
        return resolved

    findings_out = []
    for f in findings:
        resolved = _resolve(
            target_kind="finding", target_id=f["finding_id"], field="observation",
            fallback_text=f.get("observation"),
        )
        findings_out.append({
            "finding_id": f["finding_id"], "title": f.get("title"), "severity": f.get("severity"),
            **resolved,
        })

    findings_by_id = {f["finding_id"]: f for f in findings}
    themes_out = []
    for theme in themes_rows:
        if theme.get("generation") != generation or theme.get("superseded"):
            continue
        title = _resolve(target_kind="theme", target_id=theme["theme_id"], field="title")
        if title["text"] is None:
            continue
        summary = _resolve(target_kind="theme", target_id=theme["theme_id"], field="summary")
        if summary["text"] is None:
            continue
        root_cause = _resolve(target_kind="theme", target_id=theme["theme_id"], field="root_cause")
        review_observations = _resolve(
            target_kind="theme", target_id=theme["theme_id"], field="review_observations",
            fallback_text=[], is_list=True,
        )
        members = [findings_by_id[fid] for fid in theme.get("finding_ids", []) if fid in findings_by_id]
        themes_out.append({
            "theme_id": theme["theme_id"], "title": title, "summary": summary,
            "root_cause": root_cause, "review_observations": review_observations,
            "members": [{"severity": m.get("severity"), "title": m.get("title")} for m in members],
        })

    exec_summary = _resolve(target_kind="run", target_id="run", field="exec_summary", fallback_text=[], is_list=True)

    candidates_out = []
    for c in candidates:
        observation = _resolve(target_kind="candidate", target_id=c["candidate_id"], field="observation")
        candidates_out.append({
            "candidate_id": c["candidate_id"], "rule_id": c.get("rule_id"), "title": c.get("title"),
            "proposed_severity": c.get("proposed_severity"), "severity_reason": c.get("severity_reason"),
            "metrics_cited": c.get("metrics_cited") or [], "exposure_amount": c.get("exposure_amount"),
            "candidate_status": c.get("candidate_status"), "decided_by": c.get("decided_by"),
            "decided_at": c.get("decided_at"), "decision_reason": c.get("decision_reason"),
            "decided_severity": c.get("decided_severity"),
            "observation_text": observation["text"], "observation_sources": observation["sources"],
        })

    undecided = [c["candidate_id"] for c in candidates_out if c["candidate_status"] == "candidate"]

    degraded_label = None
    if exec_summary["status"] not in ("model", "model_repaired", "human_edit"):
        degraded_label = exec_summary["label"]

    return {
        "narration_enabled": bool(getattr(ctx.settings, "narration_enabled", False)),
        "degraded_label": degraded_label,
        "findings": findings_out,
        "themes": themes_out,
        "exec_summary": exec_summary,
        "candidates": candidates_out,
        "undecided_candidate_ids": undecided,
    }


def decide_candidate(
    run_id: str, candidate_id: str, *, decision: str, reason: str | None, decided_severity: str | None, actor: str,
) -> dict:
    return service.decide_candidate(
        get_context(), run_id, candidate_id, decision=decision, reason=reason,
        decided_severity=decided_severity, actor=actor,
    )


def edit_narrative(run_id: str, narrative_id: str, new_text, actor: str) -> dict:
    return service.edit_narrative(get_context(), run_id, narrative_id, new_text, actor=actor)


def regenerate_narration(run_id: str, actor: str) -> None:
    service.regenerate_narration(get_context(), run_id, actor)


def restart_stale_run(run_id: str, actor: str) -> str:
    return service.restart_stale_run(get_context(), run_id, actor)


# ── Audit runs ───────────────────────────────────────────────────────────────

def list_audit_runs(filters: dict | None = None) -> list[dict]:
    return service.list_runs(get_context(), filters=filters)


# ── Management actions ───────────────────────────────────────────────────────

def list_management_actions(filters: dict | None = None) -> list[dict]:
    return service.list_management_actions(get_context(), filters=filters)


def update_management_action(
    action_id: str, *, owner: str | None, status: str, target_date: str | None,
    response: str | None, actor: str,
) -> dict:
    return service.update_management_action(
        get_context(), action_id, owner=owner, status=status, target_date=target_date,
        response=response, actor=actor,
    )


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
