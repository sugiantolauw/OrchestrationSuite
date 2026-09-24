"""T-N3 (P6 WP N7, docs/specs/P6_narration_design.md §11, §1 point 8, §6.3):
NN8 replayability for narration -- a second run on the same data gives
every narration item a cache hit with identical `template_text`; a
regeneration (a higher `options["narration_generation"]`, which changes
`$generation_line`) gets fresh cache keys instead; and `LLM_CACHE_MODE=
replay` with a `RaisingModelClient` reproduces stored prose with no live
call at all."""

from __future__ import annotations

import dataclasses

import pytest

from orchestrator.adapters.model_fake import RaisingModelClient
from orchestrator.llm.errors import ModelUnavailable, LLMReplayMiss
from orchestrator.nodes.narration import narrate
from tests.narration_test_support import DispatchingModelClient, happy_responses, make_narration_harness, run_to_narrate_input


def _run_independent_narratives(persistence, run_id: str) -> dict[tuple, str | None]:
    """`target_id` embeds `run_id` for every target_kind that has one
    (`finding_id`/`theme_id` are both `f"{run_id}:..."`) -- two different
    runs over identical data therefore never share a literal `target_id`,
    even on a full cache hit. Strip the run_id prefix so two runs' rows can
    be compared by what they actually mean (this finding/theme's own
    fields), not by their run-scoped row identity."""
    out: dict[tuple, str | None] = {}
    for row in persistence.get_narratives(run_id):
        target_id = row["target_id"]
        if target_id.startswith(f"{run_id}:"):
            target_id = target_id[len(run_id) + 1 :]
        out[(row["target_kind"], target_id, row["field"])] = row["template_text"]
    return out


def test_a_second_run_on_the_same_data_is_served_entirely_from_cache(local_persistence, tmp_path):
    client1 = DispatchingModelClient(happy_responses())
    h1 = make_narration_harness(local_persistence, tmp_path / "run1", model_client=client1)
    state1 = run_to_narrate_input(h1)
    narrate(h1.ctx, state1)
    narratives1 = _run_independent_narratives(local_persistence, state1.run_id)
    assert client1.calls  # the first run genuinely called out

    # A second, independent run over the SAME source data and Skill -- the
    # prompt never contains run_id/timestamps/actors (§1 point 8), so its
    # prompt_sha256 is identical to run 1's, and every call is a cache hit.
    client2 = RaisingModelClient(ModelUnavailable("should-not-be-called", "cache should have answered", permanent=True))
    h2 = make_narration_harness(local_persistence, tmp_path / "run2", model_client=client2)
    state2 = run_to_narrate_input(h2)
    narrate(h2.ctx, state2)  # must not raise -- every call is a cache hit, never live

    narratives2 = _run_independent_narratives(local_persistence, state2.run_id)
    assert narratives1 == narratives2

    calls2 = local_persistence.list_llm_calls(state2.run_id)
    assert calls2  # rows are still logged for run 2 (NN7), every one a cache hit
    assert all(c["source"] == "cache" and c["cache_hit"] for c in calls2)


def test_regenerate_generation_gets_fresh_cache_keys(local_persistence, tmp_path):
    client1 = DispatchingModelClient(happy_responses())
    h1 = make_narration_harness(local_persistence, tmp_path / "run1", model_client=client1)
    state1 = run_to_narrate_input(h1)
    narrate(h1.ctx, state1)

    # A "regeneration": the same run_id, generation 1 -- $generation_line is
    # no longer empty (§4.3), so the prompt --and therefore prompt_sha256--
    # differs from generation 0's. This WP does not build the regenerate()
    # service call (WP N9); bumping options directly exercises the same
    # cache-key consequence `narrate` itself is responsible for.
    state1_gen1 = dataclasses.replace(state1, options={**state1.options, "narration_generation": 1})
    client_gen1 = DispatchingModelClient(happy_responses())
    h1.ctx.model_client = client_gen1
    narrate(h1.ctx, state1_gen1)

    assert client_gen1.calls  # a fresh generation is NOT served from generation 0's cache
    gen1_calls = [c for c in local_persistence.list_llm_calls(state1.run_id) if c["source"] == "live"]
    assert gen1_calls
    assert all("Regeneration request" in str(c["messages_json"]) for c in gen1_calls)


def test_replay_mode_with_raising_client_reproduces_stored_prose(local_persistence, tmp_path):
    client1 = DispatchingModelClient(happy_responses())
    h1 = make_narration_harness(local_persistence, tmp_path / "record", model_client=client1)
    state1 = run_to_narrate_input(h1)
    narrate(h1.ctx, state1)
    recorded = _run_independent_narratives(local_persistence, state1.run_id)

    raising = RaisingModelClient(ModelUnavailable("should-not-be-called", "replay mode never calls live", permanent=True))
    h2 = make_narration_harness(
        local_persistence, tmp_path / "replay", model_client=raising, llm_cache_mode="replay",
    )
    state2 = run_to_narrate_input(h2)
    narrate(h2.ctx, state2)  # must not raise -- replay mode is served entirely from llm_cache

    replayed = _run_independent_narratives(local_persistence, state2.run_id)
    assert replayed == recorded

    calls2 = local_persistence.list_llm_calls(state2.run_id)
    assert calls2
    assert all(c["source"] == "cache" for c in calls2)


def test_replay_mode_with_no_prior_recording_raises_loudly_not_silently(local_persistence, tmp_path):
    raising = RaisingModelClient(ModelUnavailable("x", "unused", permanent=True))
    h = make_narration_harness(local_persistence, tmp_path, model_client=raising, llm_cache_mode="replay")
    state = run_to_narrate_input(h)
    with pytest.raises(LLMReplayMiss):
        narrate(h.ctx, state)
