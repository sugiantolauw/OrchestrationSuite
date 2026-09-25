"""Perf review 2026-09-25 (`orchestrator.nodes.narration._run_bounded`,
`NARRATION_MAX_PARALLEL`, `orchestrator.narration.runner.RunnerContext`'s
thread-safe `next_seq`/`mark_role_pair_dead`/`record_origin`): `narrate`'s
independent narration calls (profile, one per finding, synthesis, priority,
remediation, candidates, captions) now run through a bounded thread pool
instead of one at a time. These tests exercise what changed:

- the configured bound is actually respected (never more than N in flight),
  and 1 recovers the old strictly-sequential behaviour;
- two runs over identical data, with randomised per-call delays that force
  a different completion order each time, still store byte-identical
  narratives, the same `seq` per finding, and the same `events` (G9 —
  concurrency must not leak into anything this run persists or returns);
- a cache primed by a concurrent run still replays with zero live calls
  under `LLM_CACHE_MODE=replay`, itself run concurrently;
- every `llm_calls` row is still exactly one row per logical call (task,
  seq, transport_attempt), never duplicated or lost, when several finding
  narrations are in flight at once;
- the circuit breaker's two halves -- a call already in flight when the
  breaker trips still finishes and logs its own row, and a call not yet
  dispatched once the breaker HAS tripped makes no call at all -- proven
  first at the unit level (`_generate_item`/`RunnerContext`, deterministic,
  no thread timing involved) and then exercised end-to-end through a real
  thread pool without hanging, duplicating a call, or raising."""

from __future__ import annotations

import random
import threading
import time

import pytest

from orchestrator.adapters.model_fake import RaisingModelClient
from orchestrator.llm.errors import ModelUnavailable
from orchestrator.llm.gateway import CallContext, LLMResult
from orchestrator.narration import runner
from orchestrator.nodes.narration import narrate
from tests.narration_test_support import (
    MARKER_FIND_T1,
    MARKER_FIND_T2,
    MODEL_GPT_OSS_ENDPOINT,
    MODEL_SONNET_ENDPOINT,
    DispatchingModelClient,
    happy_responses,
    make_narration_harness,
    resp,
    run_to_narrate_input,
)


class ConcurrencyTrackingModelClient:
    """Wraps `DispatchingModelClient`'s marker matching (see that class's
    own docstring -- narrate()'s internal call order is not something a
    test should have to track) with a small per-call delay and a
    thread-safe running/peak in-flight counter. The delay is drawn from a
    per-instance `random.Random(seed)` -- a different seed gives a
    different completion order for the SAME set of calls, which is exactly
    what the determinism tests below need to vary."""

    def __init__(self, by_marker, *, delay_range: tuple[float, float] = (0.01, 0.05), seed: int = 0, default=None):
        self._inner = DispatchingModelClient(by_marker, default=default)
        self._delay_range = delay_range
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0
        self.calls = self._inner.calls

    def chat(self, **kwargs):
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            delay = self._rng.uniform(*self._delay_range)
        try:
            time.sleep(delay)
            return self._inner.chat(**kwargs)
        finally:
            with self._lock:
                self._in_flight -= 1

    def describe_endpoint(self, endpoint: str) -> dict:
        return self._inner.describe_endpoint(endpoint)


def _strip_run_prefix(run_id: str, target_id: str) -> str:
    prefix = f"{run_id}:"
    return target_id[len(prefix):] if target_id.startswith(prefix) else target_id


def _comparable_narratives(persistence, run_id: str) -> dict[tuple, object]:
    return {
        (r["target_kind"], _strip_run_prefix(run_id, r["target_id"]), r["field"]): r["template_text"]
        for r in persistence.get_narratives(run_id)
    }


_ESCAPED_MARKER_FIND_T1 = MARKER_FIND_T1.replace('"', '\\"')
_ESCAPED_MARKER_FIND_T2 = MARKER_FIND_T2.replace('"', '\\"')


def _seq_by_finding_marker(persistence, run_id: str) -> dict[str, list[int]]:
    """`llm_calls.messages_json` is the OUTGOING request re-serialised to
    JSON (`LLMGateway._log_and_return`'s own `_canonical_json(messages)`),
    so the payload's own embedded JSON (`'"finding_key":"T1"'`, the plain
    marker `DispatchingModelClient` matches against `str(messages)` with)
    appears here with its quotes escaped, one level deeper -- a message's
    `content` is itself a JSON string carrying JSON text."""
    out: dict[str, list[int]] = {}
    for c in persistence.list_llm_calls(run_id):
        if c["task"] != "find":
            continue
        haystack = c["messages_json"]
        if _ESCAPED_MARKER_FIND_T1 in haystack:
            key = "T1"
        elif _ESCAPED_MARKER_FIND_T2 in haystack:
            key = "T2"
        else:
            continue
        out.setdefault(key, []).append(c["seq"])
    return {k: sorted(v) for k, v in out.items()}


# ── bound respected, and 1 recovers strictly-sequential behaviour ──────────


def test_max_in_flight_never_exceeds_the_configured_bound(local_persistence, tmp_path):
    client = ConcurrencyTrackingModelClient(happy_responses(), delay_range=(0.02, 0.06), seed=1)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client, narration_max_parallel=3)
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    assert client.max_in_flight <= 3, f"observed {client.max_in_flight} calls in flight, bound was 3"
    # The mini Skill's `narrate` execution makes 8 independent stage-one
    # calls (profile, find x2, synthesis, priority, remediation, captions)
    # -- with a bound of 3 and a real per-call delay, genuine concurrency
    # should be observable, not just an accidental serial run.
    assert client.max_in_flight >= 2, "no concurrency was ever observed -- the pool never overlapped calls"


def test_narration_max_parallel_of_one_runs_strictly_sequentially(local_persistence, tmp_path):
    client = ConcurrencyTrackingModelClient(happy_responses(), delay_range=(0.01, 0.03), seed=2)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client, narration_max_parallel=1)
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    assert client.max_in_flight == 1


# ── determinism under concurrency (G9) ──────────────────────────────────────


def test_stored_output_is_identical_across_two_runs_with_different_completion_order(tmp_path):
    from orchestrator.adapters.persistence_local import LocalPersistence

    collected = []
    for seed in (11, 97):
        # A FRESH persistence per iteration, deliberately never the shared
        # `local_persistence` fixture: two runs over identical data give the
        # same prompt_sha256 (§1 point 8 -- no run_id/timestamps in a
        # narration prompt), so sharing one persistence would let the
        # second run's calls hit the FIRST run's `llm_cache` and never call
        # the model at all -- which would prove NN8 replay, not what this
        # test is actually after (that two genuinely live, differently-
        # ordered runs still store the same thing).
        persistence = LocalPersistence(":memory:")
        persistence.migrate()
        client = ConcurrencyTrackingModelClient(happy_responses(), delay_range=(0.0, 0.08), seed=seed)
        h = make_narration_harness(
            persistence, tmp_path / f"run-{seed}", model_client=client, narration_max_parallel=4,
        )
        state = run_to_narrate_input(h)
        result_state = narrate(h.ctx, state)
        assert client.max_in_flight >= 2, f"seed {seed}: no concurrency observed, test proves nothing"

        collected.append({
            "narratives": _comparable_narratives(persistence, state.run_id),
            "seq_by_finding": _seq_by_finding_marker(persistence, state.run_id),
            "events": [e["message"] for e in result_state.events],
            "finding_narrative_keys": sorted(_strip_run_prefix(state.run_id, k) for k in result_state.finding_narratives),
            # Values are `narrative_id`s -- sha256(run_id|...), so they
            # necessarily differ between two different runs even when their
            # CONTENT is identical (checked separately via "narratives"
            # above). Only WHICH items got a rationale is comparable here.
            "priority_rationale_keys": sorted(_strip_run_prefix(state.run_id, k) for k in result_state.priority_rationale),
            # Chart ids are not run-scoped, but the values are `narrative_id`s
            # again -- same reasoning as priority_rationale_keys above.
            "chart_caption_keys": sorted(result_state.chart_captions),
        })

    run_a, run_b = collected
    assert run_a["narratives"] == run_b["narratives"]
    assert run_a["events"] == run_b["events"]
    assert run_a["finding_narrative_keys"] == run_b["finding_narrative_keys"]
    assert run_a["priority_rationale_keys"] == run_b["priority_rationale_keys"]
    assert run_a["chart_caption_keys"] == run_b["chart_caption_keys"]
    # The `find` task's seq numbers are precomputed sequentially, in
    # `findings`' own deterministic order, BEFORE any worker thread is
    # dispatched (runner._generate_item's own `seq_pair` docstring) -- so
    # which finding gets which seq must be identical across both runs
    # regardless of which one's model call actually returned first.
    assert run_a["seq_by_finding"] == run_b["seq_by_finding"]
    assert run_a["seq_by_finding"], "no find-task calls recorded -- test is not exercising anything"


# ── replay: a cache primed by a concurrent run still replays with zero live
# calls, itself replayed concurrently ───────────────────────────────────────


def test_replay_mode_serves_a_concurrently_primed_cache_with_zero_live_calls(local_persistence, tmp_path):
    primer = ConcurrencyTrackingModelClient(happy_responses(), delay_range=(0.0, 0.05), seed=3)
    h1 = make_narration_harness(
        local_persistence, tmp_path / "record", model_client=primer, narration_max_parallel=4,
    )
    state1 = run_to_narrate_input(h1)
    narrate(h1.ctx, state1)
    recorded = _comparable_narratives(local_persistence, state1.run_id)
    assert primer.calls, "primer run made no live calls -- nothing was cached"

    raising = RaisingModelClient(ModelUnavailable("should-not-be-called", "replay mode never calls live", permanent=True))
    h2 = make_narration_harness(
        local_persistence, tmp_path / "replay", model_client=raising, llm_cache_mode="replay",
        narration_max_parallel=4,
    )
    state2 = run_to_narrate_input(h2)
    narrate(h2.ctx, state2)  # must not raise -- every call, dispatched concurrently, is a cache hit

    replayed = _comparable_narratives(local_persistence, state2.run_id)
    assert replayed == recorded

    calls2 = local_persistence.list_llm_calls(state2.run_id)
    assert calls2
    assert all(c["source"] == "cache" for c in calls2)


# ── one llm_calls row per logical call, even with candidates enabled (more
# calls sharing the SAME primary role/pair) and real concurrency ───────────


def test_llm_calls_rows_are_one_per_logical_call_under_concurrency(local_persistence, tmp_path):
    responses = dict(happy_responses())
    responses["finding-candidates/1"] = resp({"schema_version": "finding-candidates/1", "candidates": []})
    client = ConcurrencyTrackingModelClient(responses, delay_range=(0.0, 0.05), seed=5)
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client, narration_max_parallel=4,
        ai_proposed_findings_enabled=True,
    )
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    calls = local_persistence.list_llm_calls(state.run_id)
    assert calls
    seen_keys = set()
    seen_call_ids = set()
    for c in calls:
        key = (c["task"], c["seq"], c["transport_attempt"], c["endpoint_role"])
        assert key not in seen_keys, f"duplicate llm_calls row for {key} -- a concurrent write was lost or doubled"
        seen_keys.add(key)
        assert c["call_id"] not in seen_call_ids, f"duplicate call_id {c['call_id']!r} across two different calls"
        seen_call_ids.add(c["call_id"])
        assert c["node_name"] == "narrate"
        assert c["messages_json"]


# ── circuit breaker: unit-level (deterministic) proof of both halves ───────


def _fake_ctx() -> CallContext:
    return CallContext(run_id="RUN-X", node_name="narrate", actor="tester")


class _OneShotGateway:
    """Returns `result` once, then raises if called again -- for asserting
    a caller made AT MOST one call."""

    def __init__(self, result: LLMResult):
        self._result = result
        self.call_count = 0

    def call(self, **kwargs) -> LLMResult:
        self.call_count += 1
        if self.call_count > 1:
            raise AssertionError("gateway.call() invoked a second time -- the breaker should have skipped it")
        return self._result


class _FakePrompts:
    def template_set_version(self) -> str:
        return "v1"

    def render_task(self, task, *, payload_json, generation_line, **extra):
        return [{"role": "user", "content": payload_json}]

    def render_repair(self, task, *, violations_json, previous_output):
        return [{"role": "user", "content": violations_json}]


def _rc(gateway) -> runner.RunnerContext:
    return runner.RunnerContext(
        gateway=gateway, prompts=_FakePrompts(), persistence=None, clock=lambda: "2026-01-01T00:00:00Z",
        run_id="RUN-X", engagement_id=None, actor="tester", generation=0, pii_columns_masked=[],
    )


def test_a_call_already_dead_is_never_dispatched():
    """The breaker's "stop new calls cleanly" half: once a (role, fallback)
    pair is already marked dead, `_generate_item` returns
    `fallback_unavailable` without ever calling `gateway.call()` --
    `_OneShotGateway` would raise if it were."""
    gateway = _OneShotGateway(LLMResult("ok", "{}", {}, "cid", "live", "v1", None))
    rc = _rc(gateway)
    rc.mark_role_pair_dead(("model_sonnet", None))  # "find" has no fallback -> pair is (role, None)

    outcome = runner._generate_item(
        rc, task="find", payload={}, schema={"type": "object"}, extra_params={},
        validate_fn=lambda parsed: (True, []),
    )

    assert outcome.origin == "fallback_unavailable"
    assert outcome.call_ids == []
    assert gateway.call_count == 0


def test_a_call_that_trips_the_breaker_still_logs_its_own_row():
    """The breaker's "in-flight ones finish/log" half: a call made while
    the pair is NOT yet dead still goes through the gateway and gets a
    call_id back, even though its own outcome is what marks the pair dead
    for whoever asks next."""
    gateway = _OneShotGateway(LLMResult("unavailable", None, None, "cid-1", None, None, "endpoint down"))
    rc = _rc(gateway)
    assert not rc.is_role_pair_dead(("model_sonnet", None))

    outcome = runner._generate_item(
        rc, task="find", payload={}, schema={"type": "object"}, extra_params={},
        validate_fn=lambda parsed: (True, []),
    )

    assert outcome.origin == "fallback_unavailable"
    assert outcome.call_ids == ["cid-1"], "the call that tripped the breaker must still be logged, not dropped"
    assert rc.is_role_pair_dead(("model_sonnet", None))


# ── circuit breaker: real thread pool, proving the mechanism above is what
# actually runs under concurrency -- completes cleanly, never duplicates a
# call beyond what genuine in-flight overlap can explain, never hangs ──────


def test_breaker_trip_under_real_concurrency_completes_without_raising(local_persistence, tmp_path):
    responses = dict(happy_responses())
    responses["finding-candidates/1"] = resp({"schema_version": "finding-candidates/1", "candidates": []})

    class _AllSonnetUnavailable:
        """Every call against the Sonnet endpoint fails; GPT-OSS answers
        normally -- profile/priority/remediation (fallback to GPT-OSS) still
        succeed, while find/synthesis/candidates (no fallback) all end up
        `fallback_unavailable`, exercising the SAME (model_sonnet, None)
        pair from four different concurrently-dispatched items."""

        def __init__(self):
            self._gpt_oss = ConcurrencyTrackingModelClient(responses, delay_range=(0.0, 0.03), seed=7)
            self.sonnet_calls = 0
            self._lock = threading.Lock()

        def chat(self, *, endpoint, **kwargs):
            if endpoint == MODEL_SONNET_ENDPOINT:
                with self._lock:
                    self.sonnet_calls += 1
                time.sleep(0.02)
                raise ModelUnavailable(endpoint, "endpoint down")
            return self._gpt_oss.chat(endpoint=endpoint, **kwargs)

        def describe_endpoint(self, endpoint: str) -> dict:
            return {"foundation_model": None, "ready": True}

    client = _AllSonnetUnavailable()
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client, narration_max_parallel=2,
        ai_proposed_findings_enabled=True,
    )
    state = run_to_narrate_input(h)
    result_state = narrate(h.ctx, state)  # must not raise, hang, or crash

    # Four items share the (model_sonnet, None) pair here: find x2,
    # synthesis, candidates. The breaker guarantees at least one call was
    # made (there is no way to learn the endpoint is down without trying)
    # and never more than one per item -- it cannot guarantee exactly how
    # many of the four were already in flight before the first failure was
    # recorded (that is a genuine race), only that it is bounded.
    assert 1 <= client.sonnet_calls <= 4

    assert result_state.finding_narratives  # both findings still got a (fallback) narrative id
    calls = local_persistence.list_llm_calls(state.run_id)
    seen = set()
    for c in calls:
        key = (c["task"], c["seq"], c["transport_attempt"], c["endpoint_role"])
        assert key not in seen
        seen.add(key)
