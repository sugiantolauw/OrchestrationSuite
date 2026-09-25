"""T-N1 (P6 WP N7, docs/specs/P6_narration_design.md §11): the `narrate`
node's per-item generate->repair->fallback loop, the circuit breaker, the
per-item call budget, `seq` determinism and idempotent re-execution.
FakeModelClient/RaisingModelClient/DispatchingModelClient only -- no network
(tests/narration_test_support.py)."""

from __future__ import annotations

from orchestrator.adapters.model_fake import RaisingModelClient
from orchestrator.llm.errors import ModelUnavailable
from orchestrator.nodes.narration import narrate
from tests.narration_test_support import (
    MARKER_FIND_T1,
    MARKER_PRIORITY,
    DispatchingModelClient,
    happy_responses,
    make_narration_harness,
    resp,
    run_to_narrate_input,
)


def _invalid_finding_response(key: str):
    return resp(
        {
            "schema_version": "finding-narration/1", "finding_key": key,
            # a literal digit: N-D1, and missing the cited-metric coverage: N-C1
            "observation": f"This run found 12 high-value claims for {key}.",
            "recommendation": "Review high-value claims for legitimacy before reimbursement.",
            "management_questions": ["What review step currently catches a claim like this before payment?"],
        }
    )


def test_valid_output_is_stored_as_model_origin(local_persistence, tmp_path):
    client = DispatchingModelClient(happy_responses())
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    result = narrate(h.ctx, state)

    narratives = local_persistence.get_narratives(state.run_id)
    assert narratives, "narrate() must persist at least one narratives row"
    origins = {row["origin"] for row in narratives}
    assert origins == {"model"}

    calls = local_persistence.list_llm_calls(state.run_id)
    # One generate call per logical item, no repairs: 2 findings + synthesis +
    # profile + prioritise + act + export_summary + export_caption = 8.
    assert len(calls) == 8
    assert all(c["outcome"] == "succeeded" for c in calls)

    finding_id = next(iter(result.finding_narratives))
    observation_row = next(
        r for r in narratives if r["target_kind"] == "finding" and r["target_id"] == finding_id and r["field"] == "observation"
    )
    assert observation_row["template_text"] is not None
    assert "{count:" in observation_row["template_text"] or "{money:" in observation_row["template_text"]


def test_invalid_output_triggers_exactly_one_repair_then_succeeds(local_persistence, tmp_path):
    responses = happy_responses()
    valid = responses[MARKER_FIND_T1]
    responses[MARKER_FIND_T1] = [_invalid_finding_response("T1"), valid]
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    result = narrate(h.ctx, state)

    finding_id = next(fid for fid, key in ((f["finding_id"], f["rule_id"]) for f in local_persistence.list_findings(state.run_id)) if key.endswith(".T1"))
    observation_row = next(
        r for r in local_persistence.get_narratives(state.run_id)
        if r["target_kind"] == "finding" and r["target_id"] == finding_id and r["field"] == "observation"
    )
    assert observation_row["origin"] == "model_repaired"
    assert "violations" not in observation_row or observation_row.get("violations") is None

    find_calls = [c for c in local_persistence.list_llm_calls(state.run_id) if c["task"] == "find"]
    # T1: seq 1 (invalid, "succeeded" transport-wise) then seq 2 (repair,
    # valid) -- exactly the seq=2k+1/2k+2 pairing (§3.5), and never a third
    # call for this item (the repair budget is at most one round).
    t1_seqs = sorted(c["seq"] for c in find_calls if c["seq"] in (1, 2))
    assert t1_seqs == [1, 2]
    assert all(c["outcome"] == "succeeded" for c in find_calls)
    assert result.finding_narratives[finding_id] == observation_row["narrative_id"]


def test_invalid_then_invalid_repair_falls_back(local_persistence, tmp_path):
    responses = happy_responses()
    responses[MARKER_FIND_T1] = [_invalid_finding_response("T1"), _invalid_finding_response("T1")]
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    finding_id = next(f["finding_id"] for f in local_persistence.list_findings(state.run_id) if f["rule_id"].endswith(".T1"))
    find_task_fields = {"observation", "recommendation", "management_questions"}
    rows = [
        r for r in local_persistence.get_narratives(state.run_id)
        if r["target_kind"] == "finding" and r["target_id"] == finding_id and r["field"] in find_task_fields
    ]
    assert len(rows) == 3
    for row in rows:
        assert row["origin"] == "fallback_invalid"
        # §6.1 DDL: template_text is null for every fallback origin -- the
        # finding's own rule-authored template text is what a reader falls
        # back to (find() never touched by narrate()), not a second copy
        # stored here.
        assert row["template_text"] is None
        assert row["violations"] is not None

    find_calls = [c for c in local_persistence.list_llm_calls(state.run_id) if c["task"] == "find" and c["seq"] in (1, 2)]
    assert len(find_calls) == 2  # never a third (budget: at most 2 logical calls per item)


def test_unavailable_gives_fallback_with_no_repair_and_breaks_the_role(local_persistence, tmp_path):
    client = RaisingModelClient(ModelUnavailable("ep-sonnet", "rate limit 0", permanent=True))
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    result = narrate(h.ctx, state)

    narratives = local_persistence.get_narratives(state.run_id)
    assert narratives
    assert {row["origin"] for row in narratives} == {"fallback_unavailable"}
    for row in narratives:
        assert row["template_text"] is None

    # Circuit breaker (§3.5), keyed on (primary, fallback) role PAIR, not on
    # the primary role alone: `profile`/`prioritise`/`act` share ONE pair
    # (model_sonnet, model_gpt_oss); `find`/`find_synthesis`/`export_summary`
    # share a DIFFERENT pair (model_sonnet, None) since they have no
    # fallback role; `export_caption` is its own pair (model_gpt_oss, None)
    # with no sibling at all in this test.
    #
    # `narrate`'s independent items run through a bounded thread pool
    # (orchestrator.nodes.narration._run_bounded, perf review 2026-09-25).
    # This used to assert an EXACT total of 4 calls -- one attempt per pair,
    # on the theory that whichever item is scheduled earliest always wins
    # the race and trips the breaker before any sibling sharing its pair is
    # even dispatched. That is the common case, not a guarantee: this
    # harness's default `narration_max_parallel=2` still lets a second item
    # sharing the (model_sonnet, None) pair (two `find` findings + one
    # `find_synthesis`, all with no fallback and so no repair round to slow
    # them down) be claimed by the pool's other worker and start its own
    # attempt before the first item's `RunnerContext.mark_role_pair_dead`
    # has run -- a real, if narrow, unprotected race between "is this pair
    # already dead?" and "mark it dead", not a bug in the breaker itself.
    # tests/test_narration_concurrency.py's own
    # test_breaker_trip_under_real_concurrency_completes_without_raising
    # documents the identical mechanism for the same reason: "the breaker
    # guarantees at least one call was made ... and never more than one per
    # item -- it cannot guarantee exactly how many were already in flight
    # before the first failure was recorded (that is a genuine race), only
    # that it is bounded." This test asserts the same kind of bound instead
    # of an exact count that a real (if rare, observed live roughly 1 run in
    # 6) scheduling order can violate -- forcing every pair's attempts to be
    # strictly serialized would trade away the concurrency this perf review
    # was for, to guarantee an exact call count nothing downstream actually
    # depends on: every property that DOES matter -- no more than one
    # attempt per item, every attempt logged, every outcome "unavailable",
    # no duplicate row -- already holds regardless of how many items raced.
    calls = local_persistence.list_llm_calls(state.run_id)
    assert all(c["outcome"] == "unavailable" for c in calls)
    assert len({c["call_id"] for c in calls}) == len(calls), "a call_id must never repeat across two rows"

    by_task: dict[str, list[dict]] = {}
    for c in calls:
        by_task.setdefault(c["task"], []).append(c)

    # (model_sonnet, model_gpt_oss): `profile` is job index 0 -- nothing
    # sharing its pair (`prioritise`/`act`) can possibly be dispatched
    # before it, so it always attempts. `prioritise`/`act` may or may not,
    # depending on whether the pool reaches them before `profile`'s own
    # attempt (2 rows: primary then fallback) trips the breaker.
    assert "profile" in by_task
    for task in ("profile", "prioritise", "act"):
        if task in by_task:
            assert {c["endpoint_role"] for c in by_task[task]} == {"model_sonnet", "model_gpt_oss"}, task
    ab_calls = sum(len(by_task.get(t, [])) for t in ("profile", "prioritise", "act"))
    assert 2 <= ab_calls <= 6, f"expected 2-6 calls across profile/prioritise/act, got {ab_calls}"

    # (model_sonnet, None): `find`'s first finding is job index 1 -- same
    # reasoning, it always attempts (never a fallback row: no fallback role
    # is configured for it). `find_synthesis` (job index 3) and the run's
    # second `find` item (job index 2) may or may not race in before it
    # trips the breaker. `export_summary` shares this exact pair too but
    # runs strictly AFTER this thread pool (it needs synthesis's themes,
    # narrate()'s own sequential step below) -- by then the pair is always
    # already dead (find guarantees at least one attempt), so it is never
    # in `calls` at all: "there is no llm_calls row, because no call was
    # made".
    assert "find" in by_task
    assert all(c["endpoint_role"] == "model_sonnet" for c in by_task["find"])
    if "find_synthesis" in by_task:
        assert {c["endpoint_role"] for c in by_task["find_synthesis"]} == {"model_sonnet"}
    assert "export_summary" not in by_task
    a_calls = len(by_task["find"]) + len(by_task.get("find_synthesis", []))
    assert 1 <= a_calls <= 3, f"expected 1-3 calls across find/find_synthesis, got {a_calls}"

    # (model_gpt_oss, None): `export_caption` has no sibling sharing this
    # pair (find_candidates is disabled by default here) -- always exactly
    # one attempt, no race possible.
    assert by_task.get("export_caption") and {c["endpoint_role"] for c in by_task["export_caption"]} == {"model_gpt_oss"}
    assert len(by_task["export_caption"]) == 1

    assert set(by_task) <= {"profile", "prioritise", "act", "find", "find_synthesis", "export_caption"}
    assert len(calls) == ab_calls + a_calls + 1

    assert result.exec_summary is not None  # a narratives row still exists, fallback-origin


def test_call_budget_never_exceeds_two_logical_calls_per_item(local_persistence, tmp_path):
    responses = happy_responses()
    responses[MARKER_FIND_T1] = [
        _invalid_finding_response("T1"), _invalid_finding_response("T1"), _invalid_finding_response("T1"),
    ]
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    find_calls = [c for c in local_persistence.list_llm_calls(state.run_id) if c["task"] == "find" and c["seq"] in (1, 2)]
    assert len(find_calls) == 2  # never the 3rd scripted response -- the loop stops after one repair


def test_idempotent_reexecution_produces_the_same_rows(local_persistence, tmp_path):
    client = DispatchingModelClient(happy_responses())
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)
    before = {row["narrative_id"]: row for row in local_persistence.get_narratives(state.run_id)}
    themes_before = local_persistence.list_themes(state.run_id)

    # A fresh client (that would raise on any call it did not expect) proves
    # the re-execution served entirely from cache -- CLAUDE.md §2.3 rule 1's
    # idempotency, backed by NN8 replayability.
    h.ctx.model_client = DispatchingModelClient(happy_responses())
    narrate(h.ctx, state)
    after = {row["narrative_id"]: row for row in local_persistence.get_narratives(state.run_id)}
    themes_after = local_persistence.list_themes(state.run_id)

    assert set(before) == set(after)
    assert len(before) == len(after)
    for narrative_id, row in before.items():
        assert row["template_text"] == after[narrative_id]["template_text"]
        assert row["origin"] == after[narrative_id]["origin"]
    assert len(themes_before) == len(themes_after) == 1
    assert h.ctx.model_client.calls == []  # every second-pass call was a cache hit


def _priority_response(t1_rationale: str, t2_rationale: str):
    return resp(
        {
            "schema_version": "priority-rationale/1",
            "items": [
                {"key": "T1", "rationale": t1_rationale},
                {"key": "T2", "rationale": t2_rationale},
            ],
        }
    )


def test_one_bad_item_in_a_batch_call_does_not_discard_its_clean_batchmate(local_persistence, tmp_path):
    """Quality review 2026-09-25: `prioritise` (and `act`) answer for every
    finding in ONE call. Before this fix, `_generate_item`'s all-or-nothing
    contract meant T1's own violation (a literal digit -- N-D1) made the
    WHOLE batch `fallback_invalid`, discarding T2's perfectly clean text
    too. T2 must keep its model-written text; only T1, which never becomes
    valid even after the one repair round, falls back."""
    responses = happy_responses()
    # Generate: T1 has a literal digit (N-D1); T2 is clean. Repair: the
    # model "fixes" nothing about T1 (still a literal digit) but T2 stays
    # clean -- proving repair's own JSON is what T2's kept text comes from.
    responses[MARKER_PRIORITY] = [
        _priority_response(
            "This matter has recurred 3 times and carries a High severity rating.",
            "This matter carries a Low severity rating in this run.",
        ),
        _priority_response(
            "This matter has recurred 3 times in this jurisdiction and carries a High severity rating.",
            "This matter carries a Low severity rating in this run, unchanged from the prior period.",
        ),
    ]
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    findings = {f["rule_id"].rsplit(".", 1)[-1]: f["finding_id"] for f in local_persistence.list_findings(state.run_id)}
    narratives = {
        (r["target_id"], r["field"]): r
        for r in local_persistence.get_narratives(state.run_id)
        if r["target_kind"] == "finding" and r["field"] == "rationale"
    }

    t1_row = narratives[(findings["T1"], "rationale")]
    assert t1_row["origin"] == "fallback_invalid"
    assert t1_row["template_text"] is None
    assert t1_row["violations"] is not None

    t2_row = narratives[(findings["T2"], "rationale")]
    assert t2_row["origin"] == "model_repaired"
    assert t2_row["template_text"] is not None
    assert "unchanged from the prior period" in t2_row["template_text"]
    assert t2_row["violations"] is None

    # Exactly the batch's own two logical calls (generate + repair) -- T2's
    # recovered text did not cost an extra call.
    priority_calls = [c for c in local_persistence.list_llm_calls(state.run_id) if c["task"] == "prioritise"]
    assert len(priority_calls) == 2
