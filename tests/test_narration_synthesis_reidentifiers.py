"""BUG-SYNTH-T1T2 (independent review round 3, 2026-09-25): a theme's own
root_cause/summary legitimately citing a member finding's `test_id` (the
real live evidence: "The coexistence of unlinked travel requests (T3.1a)
and bookings without pre-approval (T3.1b) may indicate inconsistent
application of the pre-approval workflow...") is valid model output at
GENERATION time (`orchestrator.narration.runner.narrate_synthesis`
validates a theme's fields against `identifiers_for_findings(findings)` --
this run's full finding set), but every later RE-validation of that same
stored prose (the G11 invariant, an auditor's edit review) passed NO
identifiers at all before this fix, so a valid citation failed there even
though generation accepted it.

This file proves the fix two ways: (1) a theme whose stored text cites a
REAL member's test_id passes both at generation and at re-validation
through the exact table/identifier-set `orchestrator.service` now builds
for it (`_narrative_table` / `_narrative_allowed_identifiers`); (2) a theme
whose text cites a test_id that is NOT among this run's findings is
rejected at generation (never persisted as real prose at all, so there is
nothing for re-validation to wrongly pass either).

Named `..._reidentifiers` (not `..._synthesis_identifiers`) to avoid
colliding with the existing, unrelated
`tests/test_narration_synthesis_identifiers.py` (round-4's `key`/`test_id`
schema-field-confusion fix, commit 80eb4e8) -- a near-identical name this
round's own first pass at this file accidentally overwrote before the
mistake was caught and that file was restored from HEAD."""

from __future__ import annotations

from orchestrator import service
from orchestrator.narration.validate import validate_prose
from orchestrator.nodes.narration import narrate
from tests.narration_test_support import (
    MARKER_SYNTHESIS,
    MINI_SKILL_DIR,
    DispatchingModelClient,
    happy_responses,
    make_narration_harness,
    resp,
    run_to_narrate_input,
)

# Verbatim from exports/narration_RUN-3CCC2FB66739.md (round-3 evidence,
# theme RUN-3CCC2FB66739:G0:TH4), with the real T3.1a/T3.1b swapped for
# this fixture Skill's own T1/T2 (mini has no dotted test ids) -- the
# sentence SHAPE (a bare test-id citation, no placeholder) is what
# BUG-SYNTH-T1T2 is about, not the specific ids.
_REAL_LIVE_ROOT_CAUSE = (
    "The coexistence of unlinked travel requests (T1) and bookings without pre-approval (T2) "
    "may indicate inconsistent application of the pre-approval workflow across travel booking "
    "and expense reporting processes."
)


def _synthesis_response(*, root_cause: str):
    return resp(
        {
            "schema_version": "finding-synthesis/1",
            "themes": [
                {
                    "title": "Pre-approval workflow gaps",
                    "summary": "Both matters concern gaps in the pre-approval workflow.",
                    "root_cause_hypothesis": root_cause,
                    "finding_keys": ["T1", "T2"],
                    "review_observations": [],
                }
            ],
            "severity_proposals": [],
        }
    )


def test_theme_citing_a_real_member_test_id_passes_generation_and_revalidation(local_persistence, tmp_path):
    responses = dict(happy_responses())
    responses[MARKER_SYNTHESIS] = _synthesis_response(root_cause=_REAL_LIVE_ROOT_CAUSE)
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)

    narrate(h.ctx, state)

    themes = local_persistence.list_themes(state.run_id)
    assert len(themes) == 1, f"a legitimate member-test-id citation must not fall back: {themes}"

    narratives = local_persistence.get_narratives(state.run_id)
    row = next(
        n for n in narratives
        if n["target_kind"] == "theme" and n["field"] == "root_cause" and n["target_id"] != "run"
    )
    assert row["origin"] in ("model", "model_repaired"), row
    assert row["template_text"] == _REAL_LIVE_ROOT_CAUSE

    # The re-validation path (G11 invariant, edit review): the SAME table
    # and identifier set orchestrator.service now builds for this row --
    # before the fix, `_narrative_allowed_identifiers` did not exist and
    # every re-validation passed an empty allow-list, failing N-D1 on "T1"
    # and "T2" even though generation had just accepted them.
    # `resolve_run_skill` (called by `_narrative_table`) only needs
    # `ctx.skills_dir` beyond what `NodeContext` already carries -- the
    # real service.AppContext attribute this harness's lighter NodeContext
    # has no reason to duplicate for every other narration test.
    h.ctx.skills_dir = MINI_SKILL_DIR.parent
    new_state = local_persistence.load_state(state.run_id)
    table = service._narrative_table(h.ctx, new_state, row)
    allowed = service._narrative_allowed_identifiers(h.ctx, new_state, row)
    assert allowed >= {"T1", "T2"}, allowed
    result = validate_prose(row["template_text"], table, field="root_cause", origin="model", allowed_identifiers=allowed)
    assert result.valid, [(v.rule_id, v.message) for v in result.violations]


def test_theme_citing_a_test_id_not_in_this_run_is_rejected_at_generation(local_persistence, tmp_path):
    bad_root_cause = (
        "The coexistence of unlinked travel requests (T1) and missing register matches (T9.9) "
        "may indicate a broader gap."
    )
    responses = dict(happy_responses())
    responses[MARKER_SYNTHESIS] = [_synthesis_response(root_cause=bad_root_cause) for _ in range(2)]
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)

    narrate(h.ctx, state)

    narratives = local_persistence.get_narratives(state.run_id)
    theme_rows = [n for n in narratives if n["target_kind"] == "theme"]
    # T9.9 is not among this run's findings (only T1/T2 exist) -- generation's
    # own validate_fn must reject it (N-D1), so no theme carries this text,
    # and the run-level fallback bookkeeping row is what gets written instead.
    assert all(bad_root_cause not in (row.get("template_text") or "") for row in theme_rows)
    assert any(row["target_id"] == "run" and row["origin"] not in ("model", "model_repaired") for row in theme_rows)
