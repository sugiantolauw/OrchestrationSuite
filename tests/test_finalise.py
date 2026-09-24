"""T-C2/T-X2 (P6 WP N10, docs/specs/P6_narration_design.md §5.2, §5.3, §11):
the deterministic `finalise` node -- the first node of the export phase.

An accepted AI-proposed candidate reaches `findings` (origin='ai_proposed',
the auditor's decided severity, `candidate_id`, `accepted_by`/`accepted_at`,
the stable `rule_id`), the run headline is recomputed as rule findings plus
accepted candidates via `orchestrator.exposure`'s own line-value rules over
already-persisted `test_line_values` (never a source re-read), and an issue
plus a management action are written for it. A rejected candidate reaches
none of that -- it is never copied into `findings`, never counted, never
in the headline. `finalise` is idempotent: a re-execution for the same run
recomputes the SAME rows and the SAME headline.

Also covers the `orchestrator.narration.resolve.effective_prose` resolver's
own precedence directly (human edit > accepted model text > reviewed
template fallback, with the right NN13 label), independent of `finalise`.

Uses `tests/fixtures/skills/mini_candidates` (not the shared `mini` fixture)
because its T2 rule finding deliberately leaves `missing_amount` uncited
(see `tests/test_candidates.py`'s own note) -- exactly the shape a
genuinely NEW, headline-affecting AI-proposed finding needs. Candidates here
are written directly via `write_candidates`/`upsert_narrative` (never
through a model call), the same "N8 is not this WP's dependency" pattern
`tests/test_candidate_races.py` already uses."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import service
from orchestrator.narration import resolve
from orchestrator.narration.runner import narrative_id
from orchestrator.nodes.narration import finalise
from orchestrator.nodes.registry import NODES_FOR as REGISTRY_NODES_FOR
from orchestrator.pipeline import run_phase
from tests.n9_test_support import app_context_for
from tests.narration_test_support import DispatchingModelClient, happy_responses, make_narration_harness

MINI_CANDIDATES_SKILL_DIR = Path(__file__).parent / "fixtures" / "skills" / "mini_candidates"


def _harness_at_awaiting_signoff(local_persistence, tmp_path, clock):
    client = DispatchingModelClient(happy_responses())
    h = make_narration_harness(local_persistence, tmp_path, model_client=client, skill_dir=MINI_CANDIDATES_SKILL_DIR)
    fingerprint = h.persistence.get_fingerprint(h.state.fingerprint_id)
    state = run_phase(
        h.persistence, h.state.run_id, nodes_for=REGISTRY_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "awaiting_signoff", state.status_reason
    ctx_app = app_context_for(h, clock=clock)
    return h, ctx_app, state


# ── T-C2: an accepted candidate reaches findings/issues/actions/headline ───


def test_accepted_candidate_reaches_findings_issues_actions_and_headline(local_persistence, tmp_path, clock):
    h, ctx_app, state = _harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id

    metrics_before = h.persistence.get_run_metrics(run_id)
    headline_before = metrics_before["run_exposure_headline"]["value"]
    missing_amount = metrics_before["missing_amount"]["value"]
    findings_before = {f["rule_id"].split(".")[-1] for f in h.persistence.list_findings(run_id)}
    assert findings_before == {"T1", "T2"}

    now = h.ctx.clock()
    candidate_id = "C-MISSING-1"
    exposure_amount = metrics_before["missing_amount"]["value"]
    h.persistence.write_candidates(
        run_id,
        [
            {
                "candidate_id": candidate_id, "run_id": run_id, "engagement_id": "ENG-DEFAULT",
                "skill_id": "SKILL-MINI-CAND", "generation": 0, "rule_id": "SKILL-MINI-CAND.ai.cmissing1",
                "title": "Missing Register Match Amount", "metrics_cited": ["missing_amount"],
                "producing_test_ids": ["T2"], "proposed_severity": "Low",
                "severity_reason": "A modest amount relative to total spend.",
                "rationale": "No rule finding currently writes up this test's dollar impact.",
                "monetary_basis": "spend", "monetary_basis_note": None, "exposure_amount": exposure_amount,
                "headline_eligible": True, "headline_ineligible_reason": None, "candidate_status": "candidate",
                "call_id": f"CALL-{candidate_id}",
            }
        ],
        now=now,
    )
    import json as _json

    for field, text, is_list in (
        ("observation", "The claims missing a register match total {money:missing_amount} in this run.", False),
        ("recommendation", "Quantify the dollar impact of unmatched claims in the review.", False),
        ("management_questions", ["What is the dollar impact of claims missing a register match?"], True),
    ):
        h.persistence.upsert_narrative(
            {
                "narrative_id": narrative_id(run_id, "candidate", candidate_id, field),
                "run_id": run_id, "engagement_id": "ENG-DEFAULT", "target_kind": "candidate",
                "target_id": candidate_id, "field": field, "version": 1, "generation": 0, "origin": "model",
                "template_text": _json.dumps(text) if is_list else text,
                "sources": [{"placeholder": "{money:missing_amount}", "source_field": "run_metrics.missing_amount", "unit": "AUD"}],
                "call_ids": [f"CALL-{candidate_id}"], "served_model_version": "test-model-v1", "violations": None,
                "updated_by": "model", "updated_at": now,
            }
        )
    decided = service.decide_candidate(
        ctx_app, run_id, candidate_id, decision="accepted", reason=None, decided_severity="High", actor="alice",
    )
    assert decided["candidate_status"] == "accepted"

    signed_off = service.sign_off(ctx_app, run_id, "alice")
    assert signed_off.phase == "export"
    state = h.persistence.load_state(run_id)

    result = finalise(h.ctx, state)
    h.persistence.save_state(result)

    findings_after = h.persistence.list_findings(run_id)
    ai_finding = next(f for f in findings_after if f.get("origin") == "ai_proposed")
    assert ai_finding["candidate_id"] == candidate_id
    assert ai_finding["rule_id"] == "SKILL-MINI-CAND.ai.cmissing1"
    assert ai_finding["severity"] == "High"  # the auditor's decided severity, never the model's proposal
    assert ai_finding["proposed_severity"] == "Low"
    assert ai_finding["severity_basis"] == "ai_proposed"
    assert ai_finding["analyst_set_severity"] is True
    assert ai_finding["accepted_by"] == "alice"
    assert ai_finding["accepted_at"] is not None
    assert ai_finding["exposure_amount"] == exposure_amount
    assert "missing_amount" in ai_finding["metrics_cited"]
    assert "{money:missing_amount}" not in ai_finding["observation"]  # rendered, never a raw placeholder
    from orchestrator.findings import format_metric_value

    assert format_metric_value(missing_amount, "AUD") in ai_finding["observation"]

    # Rule findings T1/T2 are untouched, still present.
    rule_ids = {f["rule_id"].split(".")[-1] for f in findings_after if f.get("origin") != "ai_proposed"}
    assert rule_ids == {"T1", "T2"}

    # An issue and a management action exist for the new finding.
    issue_id = f"ISS-{ai_finding['finding_id']}"
    action_id = f"MA-{ai_finding['finding_id']}"
    actions = h.persistence.list_management_actions(filters={"run_id": run_id})
    action = next(a for a in actions if a["action_id"] == action_id)
    assert action["issue_id"] == issue_id
    assert action["description"]
    assert action["description_origin"] == "model"
    assert action["risk"] == "High"

    # The headline: two of T2's three "missing" lines (the $700 and $900
    # claims) are ALSO T1's own high-value spend lines -- T1's own map
    # already counts them. Only the candidate's third line (the $100 claim,
    # never cited by any rule finding) is genuinely NEW to the headline --
    # its own $1,700 exposure_amount is NOT what gets added, only the
    # de-duplicated $100. Max-per-line dedup, never a sum (CLAUDE.md §0.3).
    metrics_after = h.persistence.get_run_metrics(run_id)
    headline_after = metrics_after["run_exposure_headline"]["value"]
    rules_only = metrics_after["run_exposure_headline_rules_only"]["value"]
    assert rules_only == headline_before
    assert round(headline_after - headline_before, 2) == 100.0
    assert metrics_after["run_exposure_headline"]["source_ref"]["included"]["accepted_ai_proposed"] == [
        "SKILL-MINI-CAND.ai.cmissing1"
    ]

    # pending_recompute is now false (get_run_payload's own gap-closing check).
    payload = service.get_run_payload(ctx_app, run_id)
    assert payload["exposure"]["pending_recompute"] is False
    assert payload["exposure"]["headline"] == headline_after


# ── rejected candidates never reach findings/headline/exports ──────────────


def test_rejected_candidate_never_reaches_findings_or_headline(local_persistence, tmp_path, clock):
    h, ctx_app, state = _harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    metrics_before = h.persistence.get_run_metrics(run_id)
    headline_before = metrics_before["run_exposure_headline"]["value"]

    now = h.ctx.clock()
    h.persistence.write_candidates(
        run_id,
        [
            {
                "candidate_id": "C-REJECT-1", "run_id": run_id, "engagement_id": "ENG-DEFAULT",
                "skill_id": "SKILL-MINI-CAND", "generation": 0, "rule_id": "SKILL-MINI-CAND.ai.crejected",
                "title": "Rejected candidate", "metrics_cited": ["missing_amount"], "producing_test_ids": ["T2"],
                "proposed_severity": "Low", "severity_reason": "reason", "rationale": "rationale",
                "monetary_basis": "spend", "monetary_basis_note": None,
                "exposure_amount": metrics_before["missing_amount"]["value"], "headline_eligible": True,
                "headline_ineligible_reason": None, "candidate_status": "candidate", "call_id": "CALL-REJECT-1",
            }
        ],
        now=now,
    )
    rejected = service.decide_candidate(
        ctx_app, run_id, "C-REJECT-1", decision="rejected", reason="Not material enough to write up.",
        decided_severity=None, actor="bob",
    )
    assert rejected["candidate_status"] == "rejected"
    assert rejected["decision_reason"] == "Not material enough to write up."

    service.sign_off(ctx_app, run_id, "alice")
    state = h.persistence.load_state(run_id)
    result = finalise(h.ctx, state)
    h.persistence.save_state(result)

    findings_after = h.persistence.list_findings(run_id)
    assert all(f.get("origin") != "ai_proposed" for f in findings_after)
    assert all(f.get("candidate_id") != "C-REJECT-1" for f in findings_after)

    metrics_after = h.persistence.get_run_metrics(run_id)
    assert metrics_after["run_exposure_headline"]["value"] == headline_before

    actions = h.persistence.list_management_actions(filters={"run_id": run_id})
    assert all(a["finding_id"] != "C-REJECT-1" for a in actions)

    # Kept, never deleted, with its reason.
    still_there = next(c for c in h.persistence.list_candidates(run_id) if c["candidate_id"] == "C-REJECT-1")
    assert still_there["candidate_status"] == "rejected"
    assert still_there["decision_reason"] == "Not material enough to write up."


# ── idempotency: a re-executed finalise recomputes the SAME rows ───────────


def test_finalise_is_idempotent(local_persistence, tmp_path, clock):
    h, ctx_app, state = _harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    metrics_before = h.persistence.get_run_metrics(run_id)

    now = h.ctx.clock()
    h.persistence.write_candidates(
        run_id,
        [
            {
                "candidate_id": "C-IDEMP-1", "run_id": run_id, "engagement_id": "ENG-DEFAULT",
                "skill_id": "SKILL-MINI-CAND", "generation": 0, "rule_id": "SKILL-MINI-CAND.ai.cidemp1",
                "title": "Missing Register Match Amount", "metrics_cited": ["missing_amount"],
                "producing_test_ids": ["T2"], "proposed_severity": "Low", "severity_reason": "reason",
                "rationale": "rationale", "monetary_basis": "spend", "monetary_basis_note": None,
                "exposure_amount": metrics_before["missing_amount"]["value"], "headline_eligible": True,
                "headline_ineligible_reason": None, "candidate_status": "candidate", "call_id": "CALL-IDEMP-1",
            }
        ],
        now=now,
    )
    import json as _json

    for field, text, is_list in (
        ("observation", "The claims missing a register match total {money:missing_amount} in this run.", False),
        ("recommendation", "Quantify the dollar impact of unmatched claims in the review.", False),
        ("management_questions", ["What is the dollar impact?"], True),
    ):
        h.persistence.upsert_narrative(
            {
                "narrative_id": narrative_id(run_id, "candidate", "C-IDEMP-1", field),
                "run_id": run_id, "engagement_id": "ENG-DEFAULT", "target_kind": "candidate",
                "target_id": "C-IDEMP-1", "field": field, "version": 1, "generation": 0, "origin": "model",
                "template_text": _json.dumps(text) if is_list else text, "sources": [],
                "call_ids": ["CALL-IDEMP-1"], "served_model_version": "test-model-v1", "violations": None,
                "updated_by": "model", "updated_at": now,
            }
        )
    service.decide_candidate(
        ctx_app, run_id, "C-IDEMP-1", decision="accepted", reason=None, decided_severity="Medium", actor="alice",
    )
    service.sign_off(ctx_app, run_id, "alice")
    state = h.persistence.load_state(run_id)

    state1 = finalise(h.ctx, state)
    h.persistence.save_state(state1)
    findings_1 = h.persistence.list_findings(run_id)
    metrics_1 = h.persistence.get_run_metrics(run_id)
    actions_1 = h.persistence.list_management_actions(filters={"run_id": run_id})
    issues_1 = [i for i in h.persistence.list_issues(run_id)] if hasattr(h.persistence, "list_issues") else None

    state = h.persistence.load_state(run_id)
    state2 = finalise(h.ctx, state)
    h.persistence.save_state(state2)
    findings_2 = h.persistence.list_findings(run_id)
    metrics_2 = h.persistence.get_run_metrics(run_id)
    actions_2 = h.persistence.list_management_actions(filters={"run_id": run_id})

    assert len(findings_1) == len(findings_2)
    assert sorted(f["finding_id"] for f in findings_1) == sorted(f["finding_id"] for f in findings_2)
    assert metrics_1["run_exposure_headline"]["value"] == metrics_2["run_exposure_headline"]["value"]
    assert sorted(a["action_id"] for a in actions_1) == sorted(a["action_id"] for a in actions_2)
    assert len(actions_1) == len(actions_2)


# ── real pipeline wiring: sign-off -> finalise -> export via the executor's
# own node registry (proves NODES_FOR carries `finalise`, not only a direct
# call to the function) ─────────────────────────────────────────────────────


def test_finalise_runs_through_the_real_export_phase_registry(local_persistence, tmp_path, clock):
    h, ctx_app, state = _harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    signed_off = service.sign_off(ctx_app, run_id, "alice")
    assert signed_off.phase == "export"
    assert signed_off.status == "queued"

    fingerprint = h.persistence.get_fingerprint(state.fingerprint_id)
    final_state = run_phase(
        h.persistence, run_id, nodes_for=REGISTRY_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert final_state.status == "completed"
    node_names = [a["node_name"] for a in h.persistence.list_node_attempts(run_id)]
    assert "finalise" in node_names
    assert node_names.index("finalise") < node_names.index("export")


# ── the effective_prose resolver's own precedence, independent of finalise ─


def _table():
    from orchestrator.narration.placeholders import PlaceholderEntry

    return {"x": PlaceholderEntry(name="x", unit="count", value=3, source_field="run_metrics.x", meaning="count metric")}


def test_resolver_precedence_human_edit_beats_model_text():
    narratives_by_target = {
        ("finding", "F1", "observation"): {
            "narrative_id": "N1", "origin": "human_edit", "version": 2,
            "template_text": "A human wrote this: {count:x}.", "sources": [],
        },
    }
    result = resolve.effective_prose(
        target_kind="finding", target_id="F1", field="observation",
        narratives_by_target=narratives_by_target, table=_table(), fallback_text="template fallback",
    )
    assert result["status"] == "human_edit"
    assert "A human wrote this: 3." == result["text"]
    assert result["label"] is None


def test_resolver_precedence_model_text_beats_fallback():
    narratives_by_target = {
        ("finding", "F1", "observation"): {
            "narrative_id": "N1", "origin": "model", "version": 1,
            "template_text": "Model text: {count:x}.", "sources": [{"placeholder": "{count:x}", "source_field": "run_metrics.x", "unit": "count"}],
        },
    }
    result = resolve.effective_prose(
        target_kind="finding", target_id="F1", field="observation",
        narratives_by_target=narratives_by_target, table=_table(), fallback_text="template fallback",
    )
    assert result["status"] == "model"
    assert result["text"] == "Model text: 3."
    assert result["label"] is None
    assert result["sources"]


def test_resolver_fallback_invalid_label():
    narratives_by_target = {
        ("finding", "F1", "observation"): {
            "narrative_id": "N1", "origin": "fallback_invalid", "version": 1, "template_text": None, "sources": [],
        },
    }
    result = resolve.effective_prose(
        target_kind="finding", target_id="F1", field="observation",
        narratives_by_target=narratives_by_target, table=_table(), fallback_text="template fallback",
    )
    assert result["status"] == "fallback_invalid"
    assert result["text"] == "template fallback"
    assert result["label"] == resolve.LABEL_MODEL_TEXT_INVALID


def test_resolver_no_row_at_all_is_llm_unavailable():
    result = resolve.effective_prose(
        target_kind="finding", target_id="F1", field="observation",
        narratives_by_target={}, table=_table(), fallback_text="template fallback",
    )
    assert result["status"] == "fallback_unavailable"
    assert result["text"] == "template fallback"
    assert result["label"] == resolve.LABEL_LLM_UNAVAILABLE


def test_resolver_accepted_label_overrides_only_on_real_text():
    narratives_by_target = {
        ("candidate", "C1", "observation"): {
            "narrative_id": "N1", "origin": "model", "version": 1,
            "template_text": "Model text: {count:x}.", "sources": [],
        },
    }
    result = resolve.effective_prose(
        target_kind="candidate", target_id="C1", field="observation",
        narratives_by_target=narratives_by_target, table=_table(), fallback_text=None,
        accepted_label=resolve.accepted_candidate_label("alice"),
    )
    assert result["label"] == "AI-proposed, accepted by alice"


def test_deterministic_exec_summary_paragraphs_matches_pptx_exports_own_text():
    # §6.4: "produce today's deterministic paragraphs ... through the
    # resolver, so there is one source" -- this reproduces
    # `orchestrator.pptx_export._build_exec_summary`'s own three inline
    # paragraphs verbatim; this test pins that word-for-word match so the
    # two never silently drift before N11 retargets the exporter to call
    # this function instead of recomputing the text itself.
    from orchestrator.pptx_export import _money0

    state = type("S", (), {"audit_period": ("2026-01-01", "2026-02-28")})()
    findings = [{"severity": "High"}, {"severity": "Medium"}, {"severity": "Medium"}]
    metrics = {"run_exposure_headline": {"value": 12345.0}}

    paragraphs = resolve.deterministic_exec_summary_paragraphs(state, findings, metrics, 5)
    assert len(paragraphs) == 3
    assert paragraphs[0] == (
        "This run assessed 5 deterministic test(s) over the 2026-01-01 to 2026-02-28 audit "
        "period and raised 3 finding(s): 1 High, 2 Medium, 0 Low."
    )
    assert _money0(12345.0) in paragraphs[1]
    assert "never sums individual findings" in paragraphs[1]
    assert "none is" in paragraphs[2] and "recomputed by the export step" in paragraphs[2]


def test_deterministic_exec_summary_paragraphs_zero_findings():
    state = type("S", (), {"audit_period": ("2026-01-01", "2026-02-28")})()
    paragraphs = resolve.deterministic_exec_summary_paragraphs(state, [], {}, 5)
    assert "raised no findings" in paragraphs[0]
    assert "—" in paragraphs[1]  # no headline metric at all -- never a fabricated $0 (CLAUDE.md NN14)


def test_resolver_versions_mismatch_raises():
    from orchestrator.errors import NarrativeVersionMismatch

    narratives_by_target = {
        ("finding", "F1", "observation"): {
            "narrative_id": "N1", "origin": "model", "version": 2, "template_text": "x", "sources": [],
        },
    }
    with pytest.raises(NarrativeVersionMismatch):
        resolve.effective_prose(
            target_kind="finding", target_id="F1", field="observation",
            narratives_by_target=narratives_by_target, table=_table(), fallback_text=None,
            versions={"N1": 1},
        )
