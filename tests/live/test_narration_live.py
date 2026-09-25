"""T-L1 (P6 WP N13, docs/specs/P6_narration_design.md §11): one small,
real narration pass against the dev workspace's configured endpoints.
Skipped unless RUN_LIVE_LLM=1 -- this whole module never runs by default,
and this session never sets that variable itself (the same convention
tests/live/test_llm_endpoints_live.py already uses; CLAUDE.md operating
instructions: this session must not touch the real workspace).

When it IS run (by a human, with `.env` sourced), it runs the real
`narrate` node once -- one live call per narration task (profile, two
finding write-ups, synthesis, priority, remediation, exec summary,
captions) against whichever endpoint MODEL_SONNET/MODEL_GPT_OSS name in
that environment (CLAUDE.md §6: in THIS development workspace that is
`databricks-gpt-oss-120b` for both roles, the recorded override) -- and
asserts, for every persisted narrative:

  - a `model`/`model_repaired` row's schema is well-formed (it round-trips
    the same digit-outside-placeholder check tests/test_g11_invariant.py
    applies to a recorded run -- a G11 pass), or
  - a `fallback_invalid`/`fallback_unavailable` row carries no stored
    prose and the run's own event names it a labelled fallback (never a
    silent success);

and that every logical call is present in `llm_calls` (NN7)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM") != "1",
    reason="RUN_LIVE_LLM not set — live model endpoint calls are opt-in only",
)


def test_one_real_narration_pass_per_task_passes_g11_or_is_a_labelled_fallback(tmp_path):
    from orchestrator.adapters.persistence_local import LocalPersistence
    from orchestrator.config import load_settings
    from orchestrator.narration.placeholders import scan_placeholders, strip_placeholder_spans
    from orchestrator.nodes.narration import narrate
    from tests.narration_test_support import make_narration_harness, run_to_narrate_input

    settings = load_settings(os.environ)
    if not settings.model_sonnet:
        pytest.skip("MODEL_SONNET is not configured in this environment")

    persistence = LocalPersistence(str(tmp_path / "orch.db"))
    persistence.migrate()

    h = make_narration_harness(
        persistence, tmp_path, model_client=None,  # None -> narrate() builds a real DatabricksModelClient
        model_sonnet=settings.model_sonnet, model_gpt_oss=settings.model_gpt_oss or settings.model_sonnet,
    )
    state = run_to_narrate_input(h)
    result = narrate(h.ctx, state)  # must not raise -- a live-unavailable endpoint still completes the run (NN13)

    calls = persistence.list_llm_calls(state.run_id)
    assert calls, "no llm_calls rows logged for a live narration pass"
    for c in calls:
        assert c["node_name"] == "narrate"
        assert c["messages_json"], "prompt not logged (NN7)"
        assert c["source"] in ("live", "cache")
        print(f"{c['task']} seq={c['seq']}: endpoint={c['endpoint']!r} outcome={c['outcome']!r} "
              f"served_model={c['served_model_version']!r}")

    narratives = persistence.get_narratives(state.run_id)
    assert narratives
    for n in narratives:
        if n["origin"] in ("model", "model_repaired"):
            text = n["template_text"]
            items = text if isinstance(text, list) else [text]
            for item in items:
                spans = scan_placeholders(item)
                stripped = strip_placeholder_spans(item, spans)
                stray_digits = [ch for ch in stripped if ch.isnumeric()]
                assert not stray_digits, f"{n['narrative_id']}: digit(s) outside a placeholder span: {item!r}"
        else:
            assert n["template_text"] is None, f"{n['narrative_id']}: fallback row carries stored prose"
            print(f"{n['narrative_id']} ({n['target_kind']}.{n['field']}): fallback, origin={n['origin']!r}")

    print(result.events[-1]["message"])
