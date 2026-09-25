"""Independent narration-content review 2026-09-25: the live run's synthesis
step fell back to the deterministic "no themes" text (`orchestrator.
narration.runner.narrate_synthesis`'s own `else` branch, "model text failed
validation") even though the repair round produced a structurally-valid
response -- ONE theme's own prose tripped a content rule and that single
violation discarded every OTHER theme too, the same shape
`_persist_batch_items` already fixes for `narrate_priority`/
`narrate_remediation`. This regression test reproduces that shape directly:
a synthesis response with two themes, one individually clean and one with a
genuine N-S4 violation in BOTH the generate and repair rounds (so the whole
call ends up `fallback_invalid`), and asserts the clean theme still survives
while the bad one is dropped -- never substituted with invented text, and
never silently smuggled through unvalidated."""

from __future__ import annotations

from orchestrator.nodes.narration import narrate
from tests.narration_test_support import (
    MARKER_FIND_T1,
    MARKER_FIND_T2,
    MARKER_SYNTHESIS,
    DispatchingModelClient,
    happy_responses,
    make_narration_harness,
    resp,
    run_to_narrate_input,
)


def _synthesis_response(*, bad_theme_summary: str):
    return resp(
        {
            "schema_version": "finding-synthesis/1",
            "themes": [
                {
                    "title": "Expense review gaps",
                    "summary": "This matter concerns a gap in reviewing claims before they are reimbursed.",
                    "root_cause_hypothesis": "This may reflect a review step that runs after payment.",
                    "finding_keys": ["T1"],
                    "review_observations": [],
                },
                {
                    "title": "Register reconciliation",
                    "summary": bad_theme_summary,
                    "root_cause_hypothesis": "This may reflect a gap in the reconciliation process.",
                    "finding_keys": ["T2"],
                    "review_observations": [],
                },
            ],
            "severity_proposals": [],
        }
    )


def test_one_bad_theme_does_not_discard_a_sibling_that_validates_on_its_own(local_persistence, tmp_path):
    # "This results in a compliance failure." is a bare, unhedged causal
    # connective (N-S4) -- the exact shape the live run's T5.2/T3.2a prose
    # had before the reword. Both the generate AND the repair round return
    # it, so `narrate_synthesis`'s own `validate_fn` gate never passes and
    # the outcome is genuinely `fallback_invalid` with a structurally-valid
    # `attempted_parsed` -- the salvage path this test exists to exercise.
    bad_summary = "This results in a compliance failure."
    responses = dict(happy_responses())
    responses[MARKER_SYNTHESIS] = [_synthesis_response(bad_theme_summary=bad_summary) for _ in range(2)]
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)

    narrate(h.ctx, state)

    themes = local_persistence.list_themes(state.run_id)
    assert len(themes) == 1, f"expected exactly the individually-clean theme to survive, got {themes}"

    narratives = local_persistence.get_narratives(state.run_id)
    theme_rows = [n for n in narratives if n["target_kind"] == "theme"]
    kept_target_ids = {n["target_id"] for n in theme_rows if n["target_id"] != "run"}
    assert len(kept_target_ids) == 1

    for row in theme_rows:
        if row["target_id"] == "run":
            continue
        assert row["origin"] in ("model", "model_repaired")
        assert row["field"] != "summary" or bad_summary not in (row["template_text"] or "")

    # G12-lite must not have been loosened: the bad theme's text is nowhere
    # in what got persisted as valid narration.
    assert all(bad_summary not in (row.get("template_text") or "") for row in theme_rows)


def test_every_theme_individually_bad_still_falls_back_to_the_deterministic_no_themes_row(
    local_persistence, tmp_path,
):
    bad_summary_1 = "This results in a compliance failure."
    bad_summary_2 = "This causes a downstream reconciliation gap."
    responses = dict(happy_responses())
    both_bad = resp(
        {
            "schema_version": "finding-synthesis/1",
            "themes": [
                {
                    "title": "Expense review gaps",
                    "summary": bad_summary_1,
                    "root_cause_hypothesis": "This may reflect a review step that runs after payment.",
                    "finding_keys": ["T1"],
                    "review_observations": [],
                },
                {
                    "title": "Register reconciliation",
                    "summary": bad_summary_2,
                    "root_cause_hypothesis": "This may reflect a gap in the reconciliation process.",
                    "finding_keys": ["T2"],
                    "review_observations": [],
                },
            ],
            "severity_proposals": [],
        }
    )
    responses[MARKER_SYNTHESIS] = [both_bad, both_bad]
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)

    narrate(h.ctx, state)

    themes = local_persistence.list_themes(state.run_id)
    assert themes == []
    narratives = local_persistence.get_narratives(state.run_id)
    run_rows = [n for n in narratives if n["target_kind"] == "theme" and n["target_id"] == "run"]
    assert len(run_rows) == 1
    assert run_rows[0]["origin"] == "fallback_invalid"
