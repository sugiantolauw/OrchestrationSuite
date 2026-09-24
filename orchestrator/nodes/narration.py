"""The `narrate` node (P6 WP N7, docs/specs/P6_narration_design.md §2, §4).
Runs after `prioritise` and before `act` in the `fieldwork` execute phase
(CLAUDE.md §3 non-negotiable 2's amendment, §4.6): every run-time LLM call
except T4.3's row-level classification happens here -- profile narrative,
finding prose, theme synthesis (plus any severity proposals it makes),
priority rationale, remediation drafts, the exec summary and chart captions.
`find`/`prioritise` stay rules-only; this node never adds, removes or
re-severities a RULE finding -- it only writes prose (as `narratives` rows,
referenced from `RunState` by id, CLAUDE.md §6.2) and, where a theme's
synthesis proposes one, `findings.theme_id` / `proposed_severity` /
`proposed_severity_reason` (existing columns, migration 002/011).

AI-proposed CANDIDATE findings (§5, `find_candidates`, P6 WP N8) are proposed
here too, behind the `ai_proposed_findings_enabled` switch (default off,
§1 #9) -- one call, after synthesis and before `act`'s remediation drafts,
via `orchestrator.narration.candidates.narrate_candidates`. A candidate is
never merged into `findings`: it stays a separate, undecided row in
`finding_candidates` until an auditor accepts or rejects it (N9's job), so
`narrate_priority`/`narrate_remediation` below still see only rule
findings.

Idempotent per CLAUDE.md §2.3 rule 1: every persistence write below either
upserts by a deterministic id (`upsert_narrative` by `narrative_id`,
`write_themes` by `theme_id`) or replaces this run's own finding rows
wholesale (`write_findings`), so a re-executed `narrate` for the same
generation overwrites its own prior output rather than appending."""

from __future__ import annotations

import dataclasses

from orchestrator import exposure
from orchestrator.catalogue_counts import catalogue_tests_for_skill
from orchestrator.narration import candidates as candidates_module
from orchestrator.narration import resolve
from orchestrator.narration import runner
from orchestrator.narration.payloads import finding_key, pii_columns_masked
from orchestrator.narration.prompts import NarrationPromptRepository
from orchestrator.nodes.context import NodeContext
from orchestrator.state import RunState

__all__ = ["narrate", "finalise"]


def _event(node: str, message: str, now: str) -> dict:
    return {"node": node, "message": message, "at": now}


def _build_narration_gateway(ctx: NodeContext):
    from orchestrator.config import NODE_MODELS
    from orchestrator.llm.gateway import LLMGateway
    from orchestrator.llm.tasks import FALLBACK_ROLE

    client = ctx.model_client
    if client is None:  # pragma: no cover -- real client, exercised only with RUN_LIVE_LLM=1
        from orchestrator.adapters.model_databricks import DatabricksModelClient

        client = DatabricksModelClient()
    return LLMGateway(
        settings=ctx.settings, client=client, persistence=ctx.persistence, node_models=NODE_MODELS,
        retry_backoff_s=getattr(ctx.settings, "llm_retry_backoff_s", 5.0),
        timeout_s=getattr(ctx.settings, "llm_timeout_s", 180.0), clock=ctx.clock,
        fallback_role=FALLBACK_ROLE,
    )


def _chart_specs(findings: list[dict], metrics: dict[str, dict]) -> tuple[list[dict], dict[str, dict]]:
    """A small, domain-agnostic pair of run-level charts (severity mix and
    the exposure headline) -- the actual PPTX chart set is WP N11's rebuild
    (CLAUDE.md §4.7); this WP only needs something real for `export_caption`
    to narrate against. Skipped entirely on a clean run (no findings), the
    same G10 spirit as `find_candidates`/`export_summary` skipping their own
    calls rather than narrating "nothing here"."""
    if not findings:
        return [], {}
    counts = {"High": 0, "Medium": 0, "Low": 0}
    for f in findings:
        severity = f.get("severity")
        if severity in counts:
            counts[severity] += 1
    chart_metrics = dict(metrics)
    chart_metrics["run_high_count"] = {"value": counts["High"], "unit": "count"}
    chart_metrics["run_medium_count"] = {"value": counts["Medium"], "unit": "count"}
    chart_metrics["run_low_count"] = {"value": counts["Low"], "unit": "count"}
    specs = [
        {
            "chart_id": "severity_distribution",
            "what_it_plots": "the count of this run's findings at each severity level",
            "metric_names": ["run_high_count", "run_medium_count", "run_low_count"],
        }
    ]
    if metrics.get("run_exposure_headline") is not None:
        specs.append(
            {
                "chart_id": "risk_and_exposure",
                "what_it_plots": "the run's amount-at-risk headline",
                "metric_names": ["run_exposure_headline"],
            }
        )
    return specs, chart_metrics


def narrate(ctx: NodeContext, state: RunState) -> RunState:
    now = ctx.clock()

    if not getattr(ctx.settings, "narration_enabled", False):
        # §8: "Narration off -> narrate makes no calls, writes no
        # narratives, records the event ... every surface shows the
        # template text with the run-level label". Every RunState narration
        # ref is left as it already is (empty/None -- `find`'s templates
        # are what every reader falls back to regardless).
        message = "Narration off — deterministic output only"
        return dataclasses.replace(state, events=state.events + [_event("narrate", message, now)])

    skill = ctx.skill
    findings = ctx.persistence.list_findings(state.run_id)
    metrics = ctx.persistence.get_run_metrics(state.run_id)
    period = tuple(state.audit_period) if state.audit_period else None
    generation = int((state.options or {}).get("narration_generation", 0) or 0)

    # §5.5 point 2 (P6 WP N10): a REGENERATE must never overwrite a human
    # edit. Read BEFORE this execution writes anything, once, so `_persist`
    # can check it per field without a live re-query per write (runner.py's
    # own `existing_human_edited` docstring).
    human_edited = frozenset(
        r["narrative_id"] for r in ctx.persistence.get_narratives(state.run_id) if r.get("origin") == "human_edit"
    )

    gateway = _build_narration_gateway(ctx)
    rc = runner.RunnerContext(
        gateway=gateway, prompts=NarrationPromptRepository(), persistence=ctx.persistence, clock=ctx.clock,
        run_id=state.run_id, engagement_id=state.engagement_id, actor=state.run_owner, generation=generation,
        pii_columns_masked=pii_columns_masked(skill), execution_key=state.current_node_attempt_id,
        existing_human_edited=human_edited,
    )

    profile_narrative_id = runner.narrate_profile(rc, state, skill)

    finding_narratives: dict[str, str] = {}
    for finding in findings:
        finding_narratives[finding["finding_id"]] = runner.narrate_finding(rc, finding, skill=skill, period=period)

    themes, severity_proposals = runner.narrate_synthesis(rc, findings, skill=skill)
    if themes:
        ctx.persistence.write_themes(state.run_id, themes, now=now)

    theme_by_finding_id: dict[str, str] = {}
    for theme in themes:
        for fid in theme["finding_ids"]:
            theme_by_finding_id[fid] = theme["theme_id"]

    if severity_proposals or theme_by_finding_id:
        updated = []
        for f in findings:
            patch: dict = {}
            proposal = severity_proposals.get(finding_key(f))
            if proposal:
                patch.update(proposal)
            theme_id = theme_by_finding_id.get(f["finding_id"])
            if theme_id:
                patch["theme_id"] = theme_id
            updated.append({**f, **patch} if patch else f)
        findings = ctx.persistence.write_findings(
            state.run_id, updated, engagement_id=state.engagement_id, skill_id=state.skill_id,
            skill_version=state.skill_version, now=now,
        )

    candidate_rows: list[dict] = []
    superseded_count = 0
    ai_proposed_enabled = bool(getattr(ctx.settings, "ai_proposed_findings_enabled", False))
    if ai_proposed_enabled:
        candidate_rows, superseded_count = candidates_module.narrate_candidates(
            rc, state=state, skill=skill, metrics=metrics, findings=findings,
            max_candidates=int(getattr(ctx.settings, "narration_max_candidates", 3) or 0),
        )

    priority_rationale = runner.narrate_priority(rc, findings, skill=skill, period=period)
    remediation_drafts = runner.narrate_remediation(rc, findings, skill=skill, period=period)
    catalogue_tests = catalogue_tests_for_skill(skill)
    exec_summary_id = runner.narrate_exec_summary(rc, state, findings, metrics, catalogue_tests=catalogue_tests, themes=themes)
    chart_specs, chart_metrics = _chart_specs(findings, metrics)
    chart_captions = runner.narrate_captions(rc, chart_specs, chart_metrics)

    counts = rc.origin_counts
    fallback_count = counts.get("fallback_invalid", 0) + counts.get("fallback_unavailable", 0)
    message = (
        f"Narration: {counts.get('model', 0)} model, {counts.get('model_repaired', 0)} repaired, "
        f"{fallback_count} fallback; {len(themes)} theme(s)"
    )
    if ai_proposed_enabled:
        message += f"; {len(candidate_rows)} AI-proposed"

    events = [_event("narrate", message, now)]
    if superseded_count:
        events.append(
            _event("narrate", f"{superseded_count} AI-proposed candidate(s) superseded (generation {generation})", now)
        )
    for row in candidate_rows:
        events.append(
            _event(
                "narrate",
                f"AI-proposed candidate {row['rule_id']}: {row['proposed_severity']}, "
                f"headline_eligible={row['headline_eligible']}",
                now,
            )
        )

    return dataclasses.replace(
        state,
        profile_narrative=profile_narrative_id,
        finding_narratives=finding_narratives,
        priority_rationale=priority_rationale,
        remediation_drafts=remediation_drafts,
        exec_summary=exec_summary_id,
        chart_captions=chart_captions,
        events=state.events + events,
    )


# ── `finalise`: the first export-phase node (P6 WP N10, §2, §5.2, §5.3) ────


def _finding_skill_ref(ctx: NodeContext, state: RunState) -> tuple[str | None, str | None]:
    """Local copy of `orchestrator.nodes.fieldwork._finding_skill_ref` --
    duplicated, not imported: `fieldwork.py` already imports `narrate`/
    `finalise` from THIS module at module load, so importing back from
    fieldwork.py here would be a circular import. Same rationale as this
    module's `_build_narration_gateway`."""
    if state.skill_id:
        return state.skill_id, state.skill_version
    if ctx.skill is not None:
        return ctx.skill.skill_id, ctx.skill.version
    return None, None


def _finding_id_for_candidate(run_id: str, rule_id: str) -> str:
    # §5.2's own table: "the SAME 12-character hash as the rule_id" --
    # rule_id is f"{skill_prefix}.ai.{hash12}" (candidates.rule_id_for),
    # where skill_prefix itself may contain dots (e.g. "EXPLORER-<run_id>"),
    # so split on the LITERAL ".ai." marker from the right, never on ".".
    hash12 = rule_id.rsplit(".ai.", 1)[-1]
    return f"{run_id}:ai.{hash12}"


def _candidate_to_finding(
    candidate: dict, *, metrics: dict[str, dict], test_by_id: dict[str, dict],
    narratives_by_target: dict[tuple, dict], versions: dict[str, int] | None,
) -> dict:
    """§5.2's `finalise` findings-row table: an accepted candidate copied
    into the SAME shape `write_findings` expects for a rule finding, with
    `origin='ai_proposed'` and the fields §5.2 lists. `versions` (the
    signed-off narrative versions, §5.2/§6.4) makes this render EXACTLY what
    the auditor saw at sign-off, never a narrative a later regenerate or
    edit might have moved on to since (which cannot happen for THIS
    candidate's own narratives post-acceptance -- §5.5 point 3 -- but
    `effective_prose`'s own check is what makes that an assertion, not an
    assumption, CLAUDE.md NN14)."""
    candidate_id = candidate["candidate_id"]
    rule_id = candidate["rule_id"]
    cited_names = candidate.get("metrics_cited") or []
    metrics_cited = {name: metrics[name] for name in cited_names if name in metrics}
    table = resolve.metrics_placeholder_table(cited_names, metrics)

    observation = resolve.effective_prose(
        target_kind="candidate", target_id=candidate_id, field="observation",
        narratives_by_target=narratives_by_target, table=table, fallback_text=None, versions=versions,
    )
    recommendation = resolve.effective_prose(
        target_kind="candidate", target_id=candidate_id, field="recommendation",
        narratives_by_target=narratives_by_target, table=table, fallback_text=None, versions=versions,
    )
    management_questions = resolve.effective_prose(
        target_kind="candidate", target_id=candidate_id, field="management_questions",
        narratives_by_target=narratives_by_target, table=table, fallback_text=[], is_list=True, versions=versions,
    )

    producing = candidate.get("producing_test_ids") or []
    if len(producing) == 1:
        test = test_by_id.get(producing[0]) or {}
        test_id, control_id, risk_id, assertion = producing[0], test.get("control_id"), test.get("risk_id"), test.get("assertion")
    else:
        test_id = control_id = risk_id = assertion = None

    exposure_amount = candidate.get("exposure_amount")
    if exposure_amount is None:
        exposure_basis = "non-monetary finding"
    else:
        exposure_basis = (
            "Sum of this AI-proposed finding's own cited amount metric(s) -- the same figure(s) "
            "the auditor reviewed in its observation text at sign-off, never re-derived from raw rows."
        )

    return {
        "finding_id": _finding_id_for_candidate(candidate["run_id"], rule_id),
        "rule_id": rule_id,
        "test_id": test_id,
        "control_id": control_id,
        "risk_id": risk_id,
        "assertion": assertion,
        "title": candidate["title"],
        "severity": candidate["decided_severity"],
        "severity_rule": None,
        "threshold_refs": [],
        "proposed_severity": candidate.get("proposed_severity"),
        "proposed_severity_reason": candidate.get("severity_reason"),
        "metrics_cited": metrics_cited,
        "evidence_refs": [],
        "observation": observation["text"],
        "recommendation": recommendation["text"],
        "management_questions": management_questions["text"] or [],
        "exposure_amount": exposure_amount,
        "exposure_basis": exposure_basis,
        "theme_id": None,
        "review_state": "draft",
        "analyst_set_severity": True,
        "severity_basis": "ai_proposed",
        "monetary_basis": candidate.get("monetary_basis"),
        "origin": "ai_proposed",
        "candidate_id": candidate_id,
        "accepted_by": candidate.get("decided_by"),
        "accepted_at": candidate.get("decided_at"),
    }


def _action_for_finding(finding: dict, *, narratives_by_target: dict[tuple, dict], versions: dict[str, int] | None) -> dict:
    """The management action for an accepted AI-proposed finding -- same id
    scheme `act` uses for a rule finding (`MA-{finding_id}`, `ISS-{finding_id}`),
    same `description` discipline (the effective recommendation text -- a
    candidate has no separate 'remediation' narrative field, §5.2's own note
    that only the observation/recommendation/questions triad exists for a
    candidate), so §2's action-set rewrite ("rule actions plus accepted
    actions, with deterministic ids") produces one row per finding either
    way."""
    candidate_table_field = ("candidate", finding["candidate_id"], "recommendation")
    row = narratives_by_target.get(candidate_table_field)
    description = finding.get("recommendation")
    description_origin = "template"
    if row is not None and row.get("origin") in ("model", "model_repaired", "human_edit"):
        description_origin = "model" if row["origin"] != "human_edit" else "human"
    return {
        "action_id": f"MA-{finding['finding_id']}",
        "issue_id": f"ISS-{finding['finding_id']}",
        "finding_id": finding["finding_id"],
        "engagement_id": finding.get("engagement_id"),
        "skill_id": finding.get("skill_id"),
        "title": finding["title"],
        "description": description,
        "owner": None,
        "risk": finding["severity"],
        "status": "draft",
        "target_date": None,
        "potential_exposure": finding.get("exposure_amount"),
        "evidence_link": finding.get("test_id"),
        "description_origin": description_origin,
    }


def finalise(ctx: NodeContext, state: RunState) -> RunState:
    """§2/§5.2/§5.3: the deterministic first node of the export phase.
    Copies every ACCEPTED AI-proposed candidate into `findings` (rejected
    and superseded candidates never reach it), recomputes the run headline
    over rule findings plus those accepted candidates purely from persisted
    `test_line_values` (never a source re-read), rewrites `run_metrics`,
    issues and management actions to match, and clears the sign-off-time
    `pending_recompute` gap (`service.get_run_payload`'s own check: it is
    true exactly until this node's `write_findings` call lands). Idempotent:
    `write_findings`/`write_issues_for_findings`/`write_management_actions`
    all upsert-or-replace by deterministic id, so a re-executed `finalise`
    (a resumed run, CLAUDE.md §2.3 rule 1) recomputes the SAME rows and the
    SAME headline, never appends a duplicate."""
    now = ctx.clock()
    run_id = state.run_id

    existing_findings = ctx.persistence.list_findings(run_id)
    accepted = [c for c in ctx.persistence.list_candidates(run_id) if c["candidate_status"] == "accepted"]
    metrics = ctx.persistence.get_run_metrics(run_id)
    narratives_by_target = {
        (r["target_kind"], r["target_id"], r["field"]): r for r in ctx.persistence.get_narratives(run_id)
    }
    versions = ((state.signoff or {}).get("narration") or {}).get("narrative_versions")

    plan_tests = ctx.skill.plan.get("tests", []) if ctx.skill else []
    test_by_id = {t["test_id"]: t for t in plan_tests if t.get("test_id")}
    skill_id, skill_version = _finding_skill_ref(ctx, state)

    new_ai_findings = [
        _candidate_to_finding(
            c, metrics=metrics, test_by_id=test_by_id, narratives_by_target=narratives_by_target, versions=versions,
        )
        for c in accepted
    ]

    persisted = ctx.persistence.write_findings(
        run_id, existing_findings + new_ai_findings, engagement_id=state.engagement_id,
        skill_id=skill_id, skill_version=skill_version, now=now,
    )
    ctx.persistence.write_issues_for_findings(run_id, persisted, engagement_id=state.engagement_id, now=now)
    # `persisted` (not `new_ai_findings`) below: engagement_id/skill_id/
    # skill_version are set by `write_findings` itself, never present on the
    # dict `_candidate_to_finding` built (persistence's own job, the same
    # split `find`'s own findings <-> persisted findings has).
    new_finding_ids = {f["finding_id"] for f in new_ai_findings}
    new_ai_persisted = [f for f in persisted if f["finding_id"] in new_finding_ids]

    # §5.3: the headline, recomputed from already-persisted test_line_values
    # (exposure.line_map_from_test_line_values) -- never re-reading source
    # data. Rule findings' own producing tests are re-derived the same way
    # exposure.compute_run_exposure did (via the recorded metric->test_id
    # mapping in `metrics`); an accepted candidate's are read straight off
    # its own stored `producing_test_ids` (§5.1 computed them, never this
    # node).
    rows_by_test_id: dict[str, list[dict]] = {}
    for row in ctx.persistence.list_test_line_values(run_id):
        rows_by_test_id.setdefault(row["test_id"], []).append(row)

    rule_line_maps: list[dict[str, float]] = []
    for f in persisted:
        if f.get("origin") == "ai_proposed":
            continue
        basis = f.get("monetary_basis")
        if basis not in ("spend", "excess"):
            continue
        cited = f.get("metrics_cited") or {}
        producing_test_ids = sorted(
            {metrics[name]["test_id"] for name in cited if name in metrics and metrics[name].get("test_id")}
        )
        rule_line_maps.append(exposure.line_map_from_test_line_values(basis, producing_test_ids, rows_by_test_id))

    candidate_line_maps: list[dict[str, float]] = []
    accepted_rule_ids: list[str] = []
    approved_not_spent_addition = 0.0
    for c in accepted:
        accepted_rule_ids.append(c["rule_id"])
        if c.get("monetary_basis") == "approved_not_spent" and c.get("exposure_amount") is not None:
            approved_not_spent_addition += c["exposure_amount"]
        if not c.get("headline_eligible"):
            continue
        candidate_line_maps.append(
            exposure.line_map_from_test_line_values(c["monetary_basis"], c.get("producing_test_ids") or [], rows_by_test_id)
        )

    headline_rules_only = exposure.headline(rule_line_maps)
    headline_exposure = exposure.headline(rule_line_maps + candidate_line_maps)

    existing_exposure_metric = metrics.get("run_exposure_headline") or {}
    existing_source_ref = dict(existing_exposure_metric.get("source_ref") or {})
    existing_source_ref["included"] = {"rule_findings": len(rule_line_maps), "accepted_ai_proposed": sorted(accepted_rule_ids)}

    existing_approved = metrics.get("run_approved_not_spent_total") or {}
    approved_not_spent_total = existing_approved.get("value")
    if approved_not_spent_addition:
        approved_not_spent_total = round((approved_not_spent_total or 0.0) + approved_not_spent_addition, 2)

    metrics_map = {
        name: {
            "metric_name": name, "value": m["value"], "unit": m.get("unit"),
            "source_ref": m.get("source_ref", {}), "test_id": m.get("test_id"),
        }
        for name, m in metrics.items()
    }
    metrics_map["run_exposure_headline"] = {
        "metric_name": "run_exposure_headline", "value": headline_exposure, "unit": "AUD",
        "source_ref": existing_source_ref, "test_id": None,
    }
    metrics_map["run_exposure_headline_rules_only"] = {
        "metric_name": "run_exposure_headline_rules_only", "value": headline_rules_only, "unit": "AUD",
        "source_ref": {"label": "Potential exposure (rule findings only, before AI-proposed acceptance)"},
        "test_id": None,
    }
    metrics_map["run_approved_not_spent_total"] = {
        "metric_name": "run_approved_not_spent_total", "value": approved_not_spent_total, "unit": "AUD",
        "source_ref": existing_approved.get("source_ref", {}), "test_id": None,
    }
    ctx.persistence.write_run_metrics(run_id, list(metrics_map.values()))

    existing_actions = [
        {k: v for k, v in a.items() if k not in ("finding_title", "finding_observation", "run_id", "created_at", "last_updated")}
        for a in ctx.persistence.list_management_actions(filters={"run_id": run_id})
    ]
    new_actions = [
        _action_for_finding(f, narratives_by_target=narratives_by_target, versions=versions) for f in new_ai_persisted
    ]
    ctx.persistence.write_management_actions(run_id, existing_actions + new_actions, now=now)

    compact = [
        {
            "finding_id": f["finding_id"], "rule_id": f["rule_id"], "test_id": f.get("test_id"),
            "severity": f["severity"], "title": f["title"], "analyst_set_severity": f.get("analyst_set_severity"),
            "exposure_amount": f["exposure_amount"], "origin": f.get("origin") or "rule",
            "theme_id": f.get("theme_id"), "proposed_severity": f.get("proposed_severity"),
        }
        for f in persisted
    ]
    compact_actions = [
        {"action_id": a["action_id"], "finding_id": a["finding_id"], "title": a["title"], "status": a["status"]}
        for a in existing_actions + new_actions
    ]

    if new_ai_findings:
        message = f"{len(new_ai_findings)} AI-proposed finding(s) added; headline recomputed"
    else:
        message = "No AI-proposed finding(s) accepted; headline recomputed"
    return dataclasses.replace(
        state, findings=compact, management_actions=compact_actions, events=state.events + [_event("finalise", message, now)],
    )
