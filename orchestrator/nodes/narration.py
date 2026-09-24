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

from orchestrator.narration import candidates as candidates_module
from orchestrator.narration import runner
from orchestrator.narration.payloads import finding_key, pii_columns_masked
from orchestrator.narration.prompts import NarrationPromptRepository
from orchestrator.nodes.context import NodeContext
from orchestrator.state import RunState

__all__ = ["narrate"]


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

    gateway = _build_narration_gateway(ctx)
    rc = runner.RunnerContext(
        gateway=gateway, prompts=NarrationPromptRepository(), persistence=ctx.persistence, clock=ctx.clock,
        run_id=state.run_id, engagement_id=state.engagement_id, actor=state.run_owner, generation=generation,
        pii_columns_masked=pii_columns_masked(skill), execution_key=state.current_node_attempt_id,
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
    exec_summary_id = runner.narrate_exec_summary(rc, state, findings, metrics, themes=themes)
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
