"""ThreadExecutor tests (CLAUDE.md build brief P3 §3): admission, the
concurrency cap, lease takeover after expiry, and StaleStateError being a
normal admission skip rather than a crash. Uses trivial injected node
functions (not the real fieldwork nodes) -- the executor does not know or
care what a node does, only the admission/lease/pool protocol around it, so
these stay fast and independent of any Skill or data fixture."""

from __future__ import annotations

import dataclasses
import threading
import time

from orchestrator import runs as runs_module
from orchestrator.executor import ThreadExecutor
from tests.conftest import canonical_ts


def _fingerprint(fp_id: str) -> dict:
    return dict(
        fingerprint_id=fp_id,
        source_table_versions="{}",
        uploaded_file_hashes="{}",
        reference_data_hashes="{}",
        skill_content_hash=None,
        code_revision="rev1",
        dependency_lock_hash="dep1",
        runtime_config_hash="rc1",
        endpoint_config="{}",
        prompt_template_version="none",
        created_at=canonical_ts(0),
    )


def _create(persistence, run_id, now_fn):
    fp = _fingerprint(f"FP-{run_id}")
    return runs_module.create_run(
        persistence, run_id=run_id, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None,
        skill_version=None, mode="playbook", audit_period=("2026-01-01", "2026-01-31"), objective="t",
        run_owner="alice", options={"auto_confirm_plan": True}, fingerprint=fp, now=now_fn(),
    )


class _Settings:
    max_concurrent_runs = 2


def _make_clock():
    n = {"v": 0}

    def clock():
        n["v"] += 1
        return canonical_ts(n["v"])

    return clock


# ── admission + concurrency cap ─────────────────────────────────────────────


def test_three_queued_runs_max_two_running_at_once(local_persistence):
    persistence = local_persistence
    clock = _make_clock()
    run_ids = [f"RUN-CAP-{i}" for i in range(3)]
    for rid in run_ids:
        _create(persistence, rid, clock)

    entered = threading.Semaphore(0)
    release = threading.Event()
    concurrent_count = {"n": 0, "max": 0}
    lock = threading.Lock()

    def blocking_node(ctx, state):
        with lock:
            concurrent_count["n"] += 1
            concurrent_count["max"] = max(concurrent_count["max"], concurrent_count["n"])
        entered.release()
        release.wait(timeout=10)
        with lock:
            concurrent_count["n"] -= 1
        return dataclasses.replace(state, events=state.events + [{"node": "block"}])

    nodes_for = {"fieldwork": {"plan": [("block", blocking_node)], "execute": [], "export": []}}

    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-cap",
        ctx_factory=lambda run_id: object(),
        fingerprint_factory=lambda run_id: _fingerprint(f"FP-{run_id}"),
        clock=clock, nodes_for=nodes_for, poll_interval_s=0.1, lease_ttl_s=30,
    )
    try:
        executor.start()
        # Exactly two of the three should enter the blocking node -- the third
        # stays `queued` in Delta (never an in-memory queue, CLAUDE.md §2.3).
        assert entered.acquire(timeout=10)
        assert entered.acquire(timeout=10)
        assert not entered.acquire(timeout=1)  # the third must NOT have started
        with lock:
            assert concurrent_count["max"] == 2

        statuses = {rid: persistence.load_state(rid).status for rid in run_ids}
        assert list(statuses.values()).count("queued") == 1
        assert list(statuses.values()).count("running") == 2

        release.set()
        assert entered.acquire(timeout=10)  # the third now gets admitted

        # auto_confirm_plan is set (options in _create), and the `execute`
        # phase's node list is empty here, so each run walks straight through
        # plan -> execute -> awaiting_signoff with no gate in between.
        deadline = time.time() + 10
        while time.time() < deadline:
            if all(persistence.load_state(rid).status == "awaiting_signoff" for rid in run_ids):
                break
            time.sleep(0.1)
        final = {rid: persistence.load_state(rid).status for rid in run_ids}
        assert all(s == "awaiting_signoff" for s in final.values()), final
    finally:
        release.set()
        executor.stop()


# ── lease takeover after expiry ─────────────────────────────────────────────


def test_admission_takes_over_an_expired_lease(local_persistence):
    persistence = local_persistence
    clock = _make_clock()
    run_id = "RUN-LEASE-TAKEOVER"
    _create(persistence, run_id, clock)

    # Simulate a prior worker that claimed this run's lease and never released
    # it (crashed before completing), then let it expire.
    persistence.acquire_lease(run_id, "dead-worker", ttl_s=1, now="2026-01-01T00:00:00.000000Z")
    later = "2026-01-01T00:05:00.000000Z"  # well past the 1s TTL
    assert persistence.expired_leases(later) == [run_id]

    done = threading.Event()

    def quick_node(ctx, state):
        done.set()
        return state

    nodes_for = {"fieldwork": {"plan": [("quick", quick_node)], "execute": [], "export": []}}
    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-new",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=lambda: later, nodes_for=nodes_for, poll_interval_s=0.1, lease_ttl_s=30,
    )
    try:
        executor.start()
        assert done.wait(timeout=10)
    finally:
        executor.stop()

    lease_rows_after = persistence.expired_leases("9999-12-31T23:59:59.999999Z")
    # released on completion -- no lingering lease row for this run
    assert run_id not in lease_rows_after


# ── StaleStateError is a normal admission skip, not a crash ────────────────


def test_run_one_swallows_stale_state_error(local_persistence):
    from orchestrator.errors import StaleStateError

    persistence = local_persistence
    clock = _make_clock()
    run_id = "RUN-STALE"
    _create(persistence, run_id, clock)

    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-stale",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=clock, nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
    )

    # Simulates another worker (or a resume) having already moved the run on
    # between this worker's lease acquisition and its first save_state call
    # inside run_phase (the queued -> running transition) -- the exact race
    # CLAUDE.md §9C's failure-injection requirement names ("two resume
    # requests racing on the same run -- exactly one must succeed, the other
    # must get a CAS rejection").
    orig_save_state = persistence.save_state
    calls = {"n": 0}

    def flaky_save_state(state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StaleStateError(run_id, state.state_version, state.state_version + 1)
        return orig_save_state(state)

    persistence.save_state = flaky_save_state
    try:
        # This must not raise -- _run_one catches StaleStateError internally
        # and treats it as a normal admission skip, not a failure.
        executor._run_one(run_id)
    finally:
        persistence.save_state = orig_save_state

    assert calls["n"] == 1
    # The run was never advanced past `queued` by this worker.
    assert persistence.load_state(run_id).status == "queued"
