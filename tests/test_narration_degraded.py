"""T-N2 (P6 WP N7, docs/specs/P6_narration_design.md §11, §8): degraded
narration -- a raising client still gives a successful run with the exact
labels and template text, and `FALLBACK_ROLE` degrades `profile`/
`prioritise`/`act` to GPT-OSS while `find`/`export_summary`/`export_caption`
have no fallback and show the LLM-unavailable label instead."""

from __future__ import annotations

from orchestrator.adapters.model_fake import RaisingModelClient
from orchestrator.llm.errors import ModelUnavailable
from orchestrator.nodes.fieldwork import act
from orchestrator.nodes.narration import narrate
from tests.narration_test_support import (
    MODEL_GPT_OSS_ENDPOINT,
    MODEL_SONNET_ENDPOINT,
    make_narration_harness,
    run_to_narrate_input,
)


def test_narration_off_makes_no_calls_and_labels_the_event(local_persistence, tmp_path):
    class _Boom:
        def chat(self, **kwargs):
            raise AssertionError("no call should be made while narration is off")

        def describe_endpoint(self, endpoint):
            raise AssertionError("no call should be made while narration is off")

    h = make_narration_harness(local_persistence, tmp_path, model_client=_Boom(), narration_enabled=False)
    state = run_to_narrate_input(h)
    result = narrate(h.ctx, state)

    assert result.events[-1]["message"] == "Narration off — deterministic output only"
    assert local_persistence.get_narratives(state.run_id) == []
    assert local_persistence.list_llm_calls(state.run_id) == []
    # Every narration ref is left exactly as `find`/`prioritise` already had
    # it -- no fabricated ref to a row that does not exist.
    assert result.profile_narrative is None
    assert result.finding_narratives == {}
    assert result.exec_summary is None

    # `act` still runs and produces a real run: its description falls back
    # to the rule-authored recommendation, never breaking on a missing
    # remediation draft.
    result2 = act(h.ctx, result)
    actions = local_persistence.list_management_actions({"run_id": state.run_id})
    assert actions
    assert all(a["description"] for a in actions)
    assert result2.management_actions


def test_every_endpoint_unavailable_still_completes_the_run(local_persistence, tmp_path):
    client = RaisingModelClient(ModelUnavailable(MODEL_SONNET_ENDPOINT, "rate limit 0", permanent=True))
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    result = narrate(h.ctx, state)  # must not raise -- a degraded run is still a completed run (NN13)

    narratives = local_persistence.get_narratives(state.run_id)
    assert narratives
    assert {row["origin"] for row in narratives} == {"fallback_unavailable"}
    assert all(row["template_text"] is None for row in narratives)

    # `act` reads the fallback: no remediation draft ever validated, so it
    # falls back to each finding's own rule-authored recommendation.
    result2 = act(h.ctx, result)
    findings = local_persistence.list_findings(state.run_id)
    actions_by_finding = {a["finding_id"]: a for a in local_persistence.list_management_actions({"run_id": state.run_id})}
    for f in findings:
        assert actions_by_finding[f["finding_id"]]["description"] == f["recommendation"]
    assert result2.status == result.status  # narrate/act never change lifecycle fields


def test_fallback_role_degrades_profile_prioritise_act_to_gpt_oss(local_persistence, tmp_path):
    """Sonnet is down; GPT-OSS answers. `find`/`find_synthesis`/
    `export_summary` have no configured fallback (CLAUDE.md §6: never
    silently degrade what a CAO/executive reads) and stay
    `fallback_unavailable`; `profile`/`prioritise`/`act` degrade to GPT-OSS
    and succeed."""
    from tests.narration_test_support import DispatchingModelClient, happy_responses

    responses = happy_responses()

    class _SonnetDownOtherwiseHappy(DispatchingModelClient):
        def chat(self, *, endpoint, messages, params, timeout_s):
            if endpoint == MODEL_SONNET_ENDPOINT:
                raise ModelUnavailable(endpoint, "rate limit 0", permanent=True)
            return super().chat(endpoint=endpoint, messages=messages, params=params, timeout_s=timeout_s)

    client = _SonnetDownOtherwiseHappy(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    narratives_by_target = {
        (row["target_kind"], row["target_id"], row["field"]): row for row in local_persistence.get_narratives(state.run_id)
    }
    assert narratives_by_target[("profile", "run", "profile")]["origin"] == "model"
    assert narratives_by_target[("profile", "run", "profile")]["served_model_version"] == "test-model-v1"

    findings = local_persistence.list_findings(state.run_id)
    for f in findings:
        assert narratives_by_target[("finding", f["finding_id"], "rationale")]["origin"] == "model"
        assert narratives_by_target[("finding", f["finding_id"], "remediation")]["origin"] == "model"
        # `find` itself has no fallback role -- stays degraded.
        assert narratives_by_target[("finding", f["finding_id"], "observation")]["origin"] == "fallback_unavailable"

    assert narratives_by_target[("run", "run", "exec_summary")]["origin"] == "fallback_unavailable"

    calls = local_persistence.list_llm_calls(state.run_id)
    fallback_calls = [c for c in calls if c["endpoint_role"] == "model_gpt_oss" and c["task"] in ("profile", "prioritise", "act")]
    assert fallback_calls  # the fallback endpoint really was used, not skipped
    assert all(c["outcome"] == "succeeded" for c in fallback_calls)


def test_fallback_is_skipped_when_it_is_the_same_endpoint_as_primary(local_persistence, tmp_path):
    """CLAUDE.md §6 development-workspace override: when MODEL_SONNET and
    MODEL_GPT_OSS resolve to the SAME endpoint, a failed primary attempt
    must not be retried a second, pointless time against that identical
    endpoint under the fallback role's name."""
    client = RaisingModelClient(ModelUnavailable(MODEL_SONNET_ENDPOINT, "down", permanent=True))
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client,
        model_sonnet=MODEL_SONNET_ENDPOINT, model_gpt_oss=MODEL_SONNET_ENDPOINT,
    )
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    calls = local_persistence.list_llm_calls(state.run_id)
    profile_calls = [c for c in calls if c["task"] == "profile"]
    assert len(profile_calls) == 1  # not 2 -- the fallback attempt against the same endpoint never happens
    assert profile_calls[0]["endpoint_role"] == "model_sonnet"


def test_batched_prioritise_repair_exhausting_retries_does_not_trip_the_breaker_for_act(
    local_persistence, tmp_path,
):
    """RUN-E12561BA6322 (independent narration-content review 2026-09-25,
    round 3): `prioritise`'s single batched call fell back for EVERY
    finding on that live run, after its repair round exhausted 3 transport
    attempts against a genuinely rate-limited endpoint (verified from that
    run's own `llm_calls` rows -- `tests/test_model_databricks.py`'s two
    `test_live_..._429_is_rate_limited_not_permanent` tests use the
    VERBATIM error messages). The review asked whether this was a circuit-
    breaker misclassification. It was not: `prioritise` and `act` share the
    exact same (model_sonnet, model_gpt_oss) role pair (`orchestrator.llm.
    tasks.FALLBACK_ROLE`), and on the live run `act` still made its own
    independent call afterwards and succeeded -- proof the breaker was
    never tripped, because `LLMGateway` correctly returns `permanent=False`
    for a RateLimited that only exhausted ONE item's retry budget
    (`orchestrator.llm.gateway._call_live`), and `orchestrator.narration.
    runner._generate_item` only calls `mark_role_pair_dead` when
    `result.permanent` is True.

    Reproduced here with a fake client, same-endpoint override (matching
    the live run's dev-workspace config, CLAUDE.md §6) so no fallback
    attempt intervenes: `prioritise`'s generate call returns a policy-
    assertion violation ("breach") forcing a repair round; the repair round
    exhausts its 3 transport attempts with the real live `RateLimited`
    messages; `act` -- same role pair, a later, independent
    `_generate_item` call within the same `narrate()` execution -- must
    still be ATTEMPTED (a real `llm_calls` row, origin `model`), not
    skipped by `is_role_pair_dead`."""
    from orchestrator.llm.errors import RateLimited
    from tests.narration_test_support import (
        MARKER_PRIORITY,
        DispatchingModelClient,
        happy_responses,
        resp,
    )

    live_message_1 = (
        "REQUEST_LIMIT_EXCEEDED: Exceeded workspace output tokens per minute rate limit "
        "for databricks-gpt-oss-120b."
    )
    live_message_2 = "REQUEST_LIMIT_EXCEEDED: Exceeded workspace QPS rate limit for databricks-gpt-oss-120b."

    responses = happy_responses()
    responses[MARKER_PRIORITY] = [
        resp({
            "schema_version": "priority-rationale/1",
            "items": [
                {"key": "T1", "rationale": "This finding may indicate a breach of policy in this run."},
                {"key": "T2", "rationale": "This matter carries a Low severity rating in this run."},
            ],
        }),
        RateLimited("ep", live_message_1),
        RateLimited("ep", live_message_2),
        RateLimited("ep", live_message_1),
    ]

    client = DispatchingModelClient(responses)
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client,
        model_sonnet=MODEL_SONNET_ENDPOINT, model_gpt_oss=MODEL_SONNET_ENDPOINT,
    )
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    narratives_by_target = {
        (row["target_kind"], row["target_id"], row["field"]): row for row in local_persistence.get_narratives(state.run_id)
    }
    findings = local_persistence.list_findings(state.run_id)
    for f in findings:
        assert narratives_by_target[("finding", f["finding_id"], "rationale")]["origin"] == "fallback_unavailable"
        # `act` -- the SAME role pair -- still got its own attempt, and
        # succeeded: the breaker never trips on a non-permanent
        # (retries-exhausted) unavailability.
        assert narratives_by_target[("finding", f["finding_id"], "remediation")]["origin"] == "model"

    calls = local_persistence.list_llm_calls(state.run_id)
    act_calls = [c for c in calls if c["task"] == "act"]
    assert act_calls  # act really was attempted, not skipped by the breaker
    assert all(c["outcome"] == "succeeded" for c in act_calls)

    priority_calls = sorted(
        (c for c in calls if c["task"] == "prioritise"), key=lambda c: (c["seq"], c["transport_attempt"]),
    )
    assert [c["outcome"] for c in priority_calls] == [
        "succeeded", "failed_transport", "failed_transport", "unavailable",
    ]
    assert [c["error_status_code"] for c in priority_calls if c["error_status_code"] is not None] == [429, 429, 429]
