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


# BUG-SYNTH-BARE-TESTID-STILL-LIVE (independent review round 4, 2026-09-25):
# a real run (RUN-6A95B7FDD22F) reported 10/12 G11 failures, all bare
# dotted test-id citations in theme root_cause/summary prose -- reported as
# a DIFFERENT, unfixed bug from BUG-SYNTH-T1T2 above. Pulled the real
# persisted rows from Delta to check: every one of the 5 distinct themes
# involved (verbatim below) cites ONLY its own theme's member finding(s)'
# real test_id -- exactly the shape the test above already proves is
# accepted at generation AND re-validation through
# identifiers_for_findings()/_narrative_allowed_identifiers(). None of
# these is a genuine N-D1 violation. The diagnosis: the round's own G11
# check almost certainly hit the same context-mismatch class of bug as
# BUG-2.4-LIVETEST-CTX (a bare NodeContext with no .skills_dir handed to
# service._narrative_table/_narrative_allowed_identifiers, or an
# equivalent gap in that script's own re-implementation), silently landing
# on an EMPTY allowed-identifiers set and failing every legitimate bare
# test-id citation. This test proves each real string validates cleanly
# through the actual product code (identifiers_for_findings + N-D1) given
# its real member finding set -- and, for contrast, that the SAME text
# fails N-D1 with an empty allowed set, which is exactly what a
# NodeContext-for-AppContext mixup silently produces.
_REAL_G1_THEME_CITATIONS = [
    # (member test_ids, root_cause/summary text, verbatim from Delta)
    (
        ["T4.2"],
        "The concentration of missing attendee records in test T4.2 may indicate insufficient "
        "capture of attendee information during expense entry.",
    ),
    (
        ["T4.2"],
        "Test T4.2 identified that {pct:att_missing_pct} of examined entertainment expense lines "
        "have no matching attendee record.",
    ),
    (
        ["T5.2"],
        "The pattern of duplicate groups detected by test T5.2 may reflect weaknesses in claim "
        "submission controls that allow multiple entries for the same expense.",
    ),
    (
        ["T6.1a"],
        "The high proportion of approvals without receipt review in test T6.1a may indicate gaps "
        "in the approver verification process.",
    ),
    (
        ["T3.1a", "T3.1b"],
        "The occurrence of unlinked pre‑approved travel requests in test T3.1a together with "
        "claims lacking pre‑approval in test T3.1b may indicate that the system’s "
        "validation of pre‑approval linkage is not consistently enforced.",
    ),
    (
        ["T4.4", "T3.3b", "T6.1d_dom"],
        "The detection of thresholds being exceeded in test T4.4, test T3.3b and test T6.1d_dom "
        "may reflect inconsistent application of spend‑limit checks during claim processing.",
    ),
]


def test_real_round4_theme_citations_are_legitimate_member_test_id_references():
    from orchestrator.narration.payloads import identifiers_for_findings
    from orchestrator.narration.placeholders import PlaceholderEntry

    # The run's full finding set (only test_id matters for
    # identifiers_for_findings; the theme's own allowed set at generation
    # AND re-validation is built from ALL of this run's findings, not just
    # the theme's own members -- CLAUDE.md §4.6, orchestrator/narration/
    # payloads.py's own identifiers_for_findings docstring).
    run_findings = [
        {"test_id": tid}
        for member_ids, _ in _REAL_G1_THEME_CITATIONS
        for tid in member_ids
    ] + [{"test_id": "T4.1"}]  # a real sibling finding this run also had
    allowed = identifiers_for_findings(run_findings)
    # The one real string that also cites a placeholder (unrelated to the
    # test-id question this test is about) needs it in the table, or N-G3
    # ("placeholder not in this item's table") fires first.
    table = {"att_missing_pct": PlaceholderEntry(name="att_missing_pct", unit="%", value=84.6)}

    for member_ids, text in _REAL_G1_THEME_CITATIONS:
        result = validate_prose(text, table, field="root_cause", origin="model", allowed_identifiers=allowed)
        assert result.valid, (member_ids, [(v.rule_id, v.message) for v in result.violations])

        # Contrast: the SAME text, with an EMPTY allowed-identifiers set --
        # what a NodeContext-for-AppContext mixup (BUG-2.4-LIVETEST-CTX's
        # class of bug) silently produces -- genuinely fails N-D1 on
        # exactly these tokens. This is not a hypothetical: it is the
        # precise shape of the round-4 false positives.
        broken = validate_prose(text, table, field="root_cause", origin="model", allowed_identifiers=frozenset())
        assert not broken.valid, "expected an empty allow-list to (wrongly) reject a legitimate test-id citation"
        assert any(v.rule_id == "N-D1" for v in broken.violations), broken.violations
