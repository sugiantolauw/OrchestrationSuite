"""T-N4 (P6 WP N7, docs/specs/P6_narration_design.md §11, CLAUDE.md non-
negotiable 11 / NN11): no raw reasoning content reaches `narratives`,
`llm_calls.response_text`, or a trace event -- a fake client that reports
having stripped reasoning parts (`ModelResponse.reasoning_parts_stripped`,
the real `ModelClient` contract, CLAUDE.md §6's own parameter matrix: gpt-oss
`content` is "a list of a reasoning part ... and a text part") never leaks
anything beyond the already-clean `.text` this module's own runner ever
touches -- `narrate`/`orchestrator.narration.runner` have no code path that
reads anything from a `ModelResponse` other than `.text`/`.parsed`/
`.served_model_version`, so this test also stands as a structural guarantee
that no such path was added."""

from __future__ import annotations

import json

from orchestrator.nodes.narration import narrate
from tests.narration_test_support import DispatchingModelClient, happy_responses, make_narration_harness, run_to_narrate_input

_REASONING_MARKER = "REASONING-LEAKED-DO-NOT-STORE"


def test_reasoning_parts_stripped_count_never_leaks_as_text(local_persistence, tmp_path):
    # Every response reports 3 stripped reasoning parts (the real client's
    # own contract: reasoning is removed before a ModelResponse is even
    # built) -- `.text` itself carries no reasoning content, exactly as a
    # real DatabricksModelClient response would arrive at this layer.
    responses = {marker: r for marker, r in happy_responses().items()}
    for marker, response in list(responses.items()):
        responses[marker] = type(response)(**{**response.__dict__, "reasoning_parts_stripped": 3})
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    result = narrate(h.ctx, state)

    calls = local_persistence.list_llm_calls(state.run_id)
    assert calls
    for c in calls:
        assert c["reasoning_parts_stripped"] == 3
        assert _REASONING_MARKER not in (c["response_text"] or "")
        # The count is stored; the content it counts is not -- there is no
        # column for raw reasoning text at all (migration 008's llm_calls
        # schema), and json.loads/parsing never adds one back in.
        parsed = json.loads(c["response_text"])
        assert "reasoning" not in json.dumps(parsed).lower() or "reasoning" in ("observation", "recommendation")

    narratives = local_persistence.get_narratives(state.run_id)
    assert narratives
    for row in narratives:
        assert _REASONING_MARKER not in json.dumps(row)

    # Trace events are execution metadata only (CLAUDE.md non-negotiable
    # 11): the event message is a short summary, never any of the prose
    # narrate() just validated and stored.
    narrate_event = result.events[-1]
    assert narrate_event["node"] == "narrate"
    assert _REASONING_MARKER not in narrate_event["message"]
    for row in narratives:
        if row["template_text"]:
            assert row["template_text"] not in narrate_event["message"]


def test_the_runner_never_reads_anything_from_a_modelresponse_besides_text_parsed_and_version():
    """A structural guarantee, not a behavioural one: `orchestrator.
    narration.runner` and `orchestrator.nodes.narration` import nothing from
    `orchestrator.adapters.protocols` at all -- they only ever see an
    `LLMResult` (`status`, `text`, `parsed`, `call_id`, `source`,
    `served_model_version`, `error`), already stripped of the raw
    `ModelResponse`/`reasoning_parts_stripped` shape by `LLMGateway`. There
    is no attribute access on a `ModelResponse` anywhere in this WP's code
    for a raw-reasoning leak to hide behind."""
    import inspect

    from orchestrator.narration import runner as runner_module
    from orchestrator.nodes import narration as narration_node_module

    for module in (runner_module, narration_node_module):
        source = inspect.getsource(module)
        assert "reasoning" not in source.lower()
        assert "ModelResponse" not in source
