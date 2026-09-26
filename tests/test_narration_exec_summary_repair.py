"""BUG-R5-2 item 2 (independent review 2026-09-26): RUN-8A1EDA1D86C5's
exec_summary ended `fallback_invalid` because its repair-round response
invented placeholders (`{pct:run_high_finding_1_share}`, `_2_share`,
`_3_share`) that do not exist in the item's own table -- N-G3 -- rather
than reusing the real, finding-specific placeholder each High-severity
finding's own PAYLOAD.top_findings[] entry actually offers (for example
`{pct:att_missing_pct}`). The repair round's OWN previous-output text had
correctly used those real names; the rewrite replaced them with a
plausible-looking but nonexistent name built by analogy with
`run_high_finding_N_title`.

This reproduces that exact shape against the real narration pipeline
(the mini Skill's own harness, `DispatchingModelClient`, `narrate()`) and
asserts N-G3 still correctly downgrades it to `fallback_invalid` -- the
fix (exec_summary_user.md's explicit "no _share by analogy" rule,
repair_user.md's "never introduce a new placeholder name" rule) targets
what the MODEL is told, not the validator, so detection must be
unaffected. It also proves a repair that correctly follows those same
rules -- reusing the real per-finding placeholder instead of inventing
one -- persists as `model_repaired`, i.e. a valid summary is achievable
once the repair keeps to a real placeholder name."""

from __future__ import annotations

from orchestrator.nodes.narration import narrate
from tests.narration_test_support import (
    MARKER_EXEC_SUMMARY,
    DispatchingModelClient,
    happy_responses,
    make_narration_harness,
    resp,
    run_to_narrate_input,
)

_ROUND1_NUMBER_WORD = resp(
    {
        "schema_version": "exec-summary/1",
        "paragraphs": [
            "This run raised {count:run_finding_count} finding(s), with a single high-severity "
            "issue: {value:run_high_finding_1_title}, which found twelve claim(s) totalling "
            "{money:hv_amount}.",
            "The amount at risk this run identified totals {money:run_exposure_headline}, "
            "driven mainly by {value:run_exposure_dominant_title}, which accounts for "
            "{money:run_exposure_dominant_amount} of that total.",
        ],
    }
)

_REPAIR_HALLUCINATED_SHARE = resp(
    {
        "schema_version": "exec-summary/1",
        "paragraphs": [
            "This run raised {count:run_finding_count} finding(s), including the high-severity "
            "issue {value:run_high_finding_1_title}, affecting {pct:run_high_finding_1_share} of "
            "the population.",
            "The amount at risk this run identified totals {money:run_exposure_headline}, "
            "driven mainly by {value:run_exposure_dominant_title}, which accounts for "
            "{money:run_exposure_dominant_amount} of that total.",
        ],
    }
)

_REPAIR_REAL_PLACEHOLDER = resp(
    {
        "schema_version": "exec-summary/1",
        "paragraphs": [
            "This run raised {count:run_finding_count} finding(s), including the high-severity "
            "issue {value:run_high_finding_1_title}, which found {count:hv_count} claim(s) "
            "totalling {money:hv_amount}.",
            "The amount at risk this run identified totals {money:run_exposure_headline}, "
            "driven mainly by {value:run_exposure_dominant_title}, which accounts for "
            "{money:run_exposure_dominant_amount} of that total.",
        ],
    }
)


def _exec_summary_row(local_persistence, run_id):
    return next(
        r for r in local_persistence.get_narratives(run_id)
        if r["target_kind"] == "run" and r["target_id"] == "run" and r["field"] == "exec_summary"
    )


def test_repair_that_invents_a_placeholder_by_analogy_still_falls_back(local_persistence, tmp_path):
    responses = happy_responses()
    responses[MARKER_EXEC_SUMMARY] = [_ROUND1_NUMBER_WORD, _REPAIR_HALLUCINATED_SHARE]
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    row = _exec_summary_row(local_persistence, state.run_id)
    assert row["origin"] == "fallback_invalid"
    assert row["template_text"] is None
    assert any(v["rule_id"] == "N-G3" for v in row["violations"])


def test_repair_that_reuses_the_real_placeholder_persists_as_model_repaired(local_persistence, tmp_path):
    responses = happy_responses()
    responses[MARKER_EXEC_SUMMARY] = [_ROUND1_NUMBER_WORD, _REPAIR_REAL_PLACEHOLDER]
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    row = _exec_summary_row(local_persistence, state.run_id)
    assert row["origin"] == "model_repaired"
    assert row["template_text"] is not None
