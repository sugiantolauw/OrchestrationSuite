"""ThreadExecutor tests (CLAUDE.md build brief P3 §3): admission, the
concurrency cap, lease takeover after expiry, StaleStateError being a
normal admission skip rather than a crash, bounded admission retries with
backoff (P3 gate review item 3a), and a lost lease halting further node
output (item 3b). Uses trivial injected node functions (not the real
fieldwork nodes) -- the executor does not know or care what a node does,
only the admission/lease/pool protocol around it, so these stay fast and
independent of any Skill or data fixture."""

from __future__ import annotations

import dataclasses
import shutil
import threading
import time
from pathlib import Path

from orchestrator import runs as runs_module
from orchestrator import service
from orchestrator.config import Settings, runtime_config_hash
from orchestrator.executor import ThreadExecutor
from orchestrator.timeutil import utc_now
from tests.conftest import canonical_ts

MINI_SKILL_DIR = Path(__file__).parent / "fixtures" / "skills" / "mini"


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


# ── idle admission-loop backoff (found-live cost review: unconditional 2s
#    polling cost ~3,000 warehouse queries/hour with nothing queued or
#    running) ──────────────────────────────────────────────────────────────


def test_idle_admission_loop_backs_off_to_the_idle_interval(local_persistence):
    """With nothing queued or running, the loop's first tick (always
    immediate on start()) finds no work and must then wait the IDLE
    interval before its next persistence call, not the fast ACTIVE one.
    Uses real (but scaled-down) intervals rather than a fake clock --
    self._stop_event.wait() sleeps real wall-clock time, so there is no
    clock to fake here; ACTIVE/IDLE are scaled down so the assertion window
    stays well under a second while still being several multiples of
    ACTIVE and a small fraction of IDLE."""
    persistence = local_persistence
    calls = {"n": 0}
    orig_find_runs = persistence.find_runs

    def counting_find_runs(statuses):
        calls["n"] += 1
        return orig_find_runs(statuses)

    persistence.find_runs = counting_find_runs

    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-idle",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=_make_clock(), nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
        poll_interval_s=0.05, idle_poll_interval_s=5.0,
    )
    try:
        executor.start()
        time.sleep(0.6)  # ~12x ACTIVE, 1/8 of IDLE -- would be ~12 ticks at ACTIVE cadence
    finally:
        executor.stop()

    # _reap_orphans + _admit_all_queued each call find_runs once per tick, so
    # 2 calls = the one tick every idle-backed-off loop must still take
    # immediately on start(); anything beyond a couple of calls means the
    # loop never backed off to IDLE.
    assert calls["n"] <= 4, calls["n"]


def test_busy_admission_loop_keeps_the_active_interval(local_persistence):
    """The counterpart of the idle test above: while this worker has a run
    admitted (in `_active_runs`), the loop must keep polling at the fast
    ACTIVE cadence, not fall back to IDLE -- otherwise a queued run waiting
    on a freed concurrency slot could sit for the whole idle sweep."""
    persistence = local_persistence
    run_id = "RUN-BUSY-CADENCE"
    _create(persistence, run_id, _make_clock())

    entered = threading.Event()
    release = threading.Event()

    def blocking_node(ctx, state):
        entered.set()
        release.wait(timeout=10)
        return dataclasses.replace(state, events=state.events + [{"node": "block"}])

    nodes_for = {"fieldwork": {"plan": [("block", blocking_node)], "execute": [], "export": []}}
    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-busy",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=_make_clock(), nodes_for=nodes_for,
        poll_interval_s=0.05, idle_poll_interval_s=5.0, lease_ttl_s=30,
    )
    try:
        executor.start()
        assert entered.wait(timeout=10)
        assert executor._next_poll_interval() == 0.05
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


# ── mid-session reaping, no App restart (CLAUDE.md §2.3 rule 2, P2/P3 gate
#    review item 8) ───────────────────────────────────────────────────────


def test_admission_loop_reaps_an_orphan_without_an_app_restart(local_persistence):
    """Previously the reaper (reap_orphaned_runs_with_leases) ran only once,
    at App start (app/app.py's _start_executor_once) -- a run left `running`
    by a dead worker AFTER that point stayed `running` forever until the
    next full App restart (found live: a run sat `running` for ~58 minutes).
    The admission loop must reap on its own, every poll tick, with no
    restart and no external call to the reaper."""
    persistence = local_persistence
    run_id = "RUN-ORPHAN-MIDSESSION"
    state = _create(persistence, run_id, lambda: "2026-01-01T00:00:00.000000Z")
    # Simulate a worker that claimed this run, moved it to `running`, then
    # died -- lease acquired, then left to expire, with nobody around to
    # renew it. No queued->running transition goes through _try_admit here,
    # so this executor never touched this run itself.
    from orchestrator.status import transition

    running_state = transition(state, "running", now="2026-01-01T00:00:01.000000Z")
    persistence.save_state(running_state)
    persistence.acquire_lease(run_id, "dead-worker", ttl_s=1, now="2026-01-01T00:00:01.000000Z")

    later = "2026-01-01T00:05:00.000000Z"  # well past the 1s TTL
    nodes_for = {"fieldwork": {"plan": [], "execute": [], "export": []}}
    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-live",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=lambda: later, nodes_for=nodes_for, poll_interval_s=0.05, lease_ttl_s=30,
    )
    try:
        executor.start()  # no run_id/phase args -- this is the background-loop start, not a start_audit_run kick
        deadline = time.time() + 10
        while time.time() < deadline:
            if persistence.load_state(run_id).status == "interrupted":
                break
            time.sleep(0.05)
    finally:
        executor.stop()

    reaped = persistence.load_state(run_id)
    assert reaped.status == "interrupted"
    assert reaped.status_reason and "orphaned" in reaped.status_reason.lower()
    # never delete, never auto-resume (CLAUDE.md §2.3 rule 2) -- the row
    # still exists and stays interrupted, the executor does not queue it
    # back up on its own.
    assert persistence.load_state(run_id) is not None
    time.sleep(0.2)  # give a couple more ticks a chance to misbehave
    assert persistence.load_state(run_id).status == "interrupted"


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


# ── full pipeline regression: plan -> execute -> sign-off -> export -> completed ──


def _wait_for_status(ctx, run_id, statuses, timeout=30):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = service.get_run(ctx, run_id)["status"]
        if last in statuses:
            return last
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {statuses} in {timeout}s (last={last})")


def test_full_run_reaches_completed_via_thread_executor(tmp_path):
    """The regression case for the export stall (CLAUDE.md build brief P3
    §1's integration pass): drives plan -> execute -> sign_off -> export ->
    completed entirely through service.start_audit_run/sign_off and a real
    ThreadExecutor (never calling a node function directly), using the mini
    Skill fixture so this runs in well under a second rather than the
    minutes a real synthetic_data/ pass takes. The actual defect that
    produced the observed stall was in app/src/run_status.py's Dash
    callbacks (a poll-vs-confirm race that meant sign_off was never called),
    not here -- but this still asserts the orchestrator side of the journey
    an auditor's sign-off must complete: the export phase gets admitted,
    the export node runs and records a node_attempts row, and the run
    reaches `completed` with an xlsx export recorded, all via the SAME
    admission/execution path the deployed App uses."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    shutil.copytree(MINI_SKILL_DIR, skills_dir / "mini")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "claims.csv").write_text(
        "Employee ID,Transaction Date,Amount,Vendor\n"
        "1,2026-01-05,600,Acme\n"
        "2,2026-01-10,900,Beta\n"
    )
    (data_dir / "register.csv").write_text(
        "Employee ID,Transaction Date,Vendor\n"
        "1,2026-01-05,Acme\n"
    )

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "SKILLS_DIR": str(skills_dir),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "ORCH_WORKER_ID": "test-full-run",
    }
    ctx = service.build_app_context(env)
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        assert all(bindings.values()), bindings

        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-01-31"),
            objective="regression: full run via ThreadExecutor", run_owner="tester",
        )

        status = _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"})
        run = service.get_run(ctx, run_id)
        assert status == "awaiting_signoff", run.get("status_reason")

        service.sign_off(ctx, run_id, "approver")
        status = _wait_for_status(ctx, run_id, {"completed", "failed"})
        run = service.get_run(ctx, run_id)
        assert status == "completed", run.get("status_reason")

        # The export node actually ran (a node_attempts row for it, outcome
        # succeeded) -- not just that the run's status reads "completed".
        attempts = ctx.persistence.list_node_attempts(run_id)
        export_attempts = [a for a in attempts if a["node_name"] == "export"]
        assert export_attempts, f"no node_attempts row for the export node: {attempts}"
        assert export_attempts[-1]["outcome"] == "succeeded"

        filename, content = service.get_export(ctx, run_id, "xlsx")
        assert filename == "workpaper.xlsx"
        assert len(content) > 100
    finally:
        ctx.executor.stop()


# ── bounded admission retries with backoff (P3 gate review item 3a) ────────


class _SettingsBackoff:
    max_concurrent_runs = 2
    admission_max_attempts = 5
    admission_backoff_base_s = 10.0
    admission_backoff_max_s = 60.0


def test_admission_backoff_delays_the_next_retry(local_persistence):
    """A queued run whose lease repeatedly cannot be acquired must not retry
    on every poll tick forever -- it backs off. Drives `_try_admit` directly
    (never starting the background loop) so the backoff window is asserted
    deterministically against a controlled clock, not real wall-clock time."""
    persistence = local_persistence
    run_id = "RUN-BACKOFF"
    _create(persistence, run_id, lambda: canonical_ts(0))

    clock_state = {"t": "2026-01-01T00:00:00.000000Z"}
    executor = ThreadExecutor(
        persistence=persistence, settings=_SettingsBackoff(), worker_id="worker-backoff",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=lambda: clock_state["t"],
        nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
    )
    calls = {"n": 0}

    def failing_acquire_lease(rid, worker_id, *, ttl_s, now):
        calls["n"] += 1
        return False

    persistence.acquire_lease = failing_acquire_lease
    try:
        executor._try_admit(run_id)
        assert calls["n"] == 1

        # Same instant, immediate retry: backoff has not elapsed, acquire_lease
        # must not even be called again.
        executor._try_admit(run_id)
        assert calls["n"] == 1

        # Just short of the base_s=10 backoff for attempt 1: still gated.
        clock_state["t"] = "2026-01-01T00:00:09.000000Z"
        executor._try_admit(run_id)
        assert calls["n"] == 1

        # Past the backoff window: retried.
        clock_state["t"] = "2026-01-01T00:00:11.000000Z"
        executor._try_admit(run_id)
        assert calls["n"] == 2

        # The run itself was never touched -- still queued, no state written
        # outside the normal CAS path.
        assert persistence.load_state(run_id).status == "queued"
    finally:
        executor.stop()


def test_admission_success_clears_a_prior_backoff(local_persistence):
    """A run that failed admission once and then succeeds must not carry a
    stale backoff entry forward (e.g. if it is ever re-queued)."""
    persistence = local_persistence
    run_id = "RUN-BACKOFF-CLEAR"
    _create(persistence, run_id, lambda: canonical_ts(0))

    executor = ThreadExecutor(
        persistence=persistence, settings=_SettingsBackoff(), worker_id="worker-backoff-clear",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=lambda: canonical_ts(0),
        nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
    )
    orig_acquire = persistence.acquire_lease
    outcomes = iter([False, True])
    persistence.acquire_lease = lambda rid, wid, *, ttl_s, now: next(outcomes)
    try:
        executor._try_admit(run_id)  # fails, records a backoff entry
        assert run_id in executor._admission_failures
        executor._try_admit(run_id)  # still backing off at the same instant -- no-op

        # Advance past the backoff window and let the second call through.
        executor._clear_admission_failure(run_id)  # simulate time having passed by clearing directly
        persistence.acquire_lease = orig_acquire
    finally:
        executor.stop()
    assert run_id not in executor._admission_failures


def test_admission_exhaustion_fails_the_run_through_the_state_machine(local_persistence):
    """After admission_max_attempts consecutive failures, the run ends
    `failed` via transition()/save_state() (CAS) -- never a direct UPDATE --
    with a clear status_reason, and a trace event records why."""
    persistence = local_persistence
    run_id = "RUN-EXHAUST"
    _create(persistence, run_id, lambda: canonical_ts(0))

    class _SettingsExhaust:
        max_concurrent_runs = 2
        admission_max_attempts = 3
        admission_backoff_base_s = 0.0
        admission_backoff_max_s = 0.0

    clock_state = {"t": "2026-01-01T00:00:00.000000Z"}
    executor = ThreadExecutor(
        persistence=persistence, settings=_SettingsExhaust(), worker_id="worker-exhaust",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=lambda: clock_state["t"],
        nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
    )
    persistence.acquire_lease = lambda rid, wid, *, ttl_s, now: False
    try:
        for i in range(3):
            clock_state["t"] = canonical_ts(i)
            executor._try_admit(run_id)

        final = persistence.load_state(run_id)
        assert final.status == "failed"
        assert final.status_reason is not None
        assert "3 attempts" in final.status_reason

        events = [
            e for e in persistence.list_trace_events(run_id)
            if e["event_type"] == "run_admission_failed"
        ]
        assert events, "expected a run_admission_failed trace event"
        assert run_id not in executor._admission_failures  # tracking cleaned up once terminal
    finally:
        executor.stop()


def test_admission_exhaustion_is_a_noop_if_the_run_already_moved_on(local_persistence):
    """A rare race: the run is admitted by another path (or resumed) between
    this worker's last failed attempt and the exhaustion check running --
    _fail_admission_exhausted must see status != 'queued' and do nothing,
    never overwrite whatever the other path already recorded."""
    persistence = local_persistence
    run_id = "RUN-EXHAUST-RACE"
    state = _create(persistence, run_id, lambda: canonical_ts(0))
    from orchestrator.status import transition as _transition

    running_state = _transition(state, "running", now=canonical_ts(1))
    persistence.save_state(running_state)

    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-race",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=lambda: canonical_ts(2),
        nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
    )
    try:
        executor._fail_admission_exhausted(run_id, now=canonical_ts(2), attempts=99)
        assert persistence.load_state(run_id).status == "running"
    finally:
        executor.stop()


# ── a lost lease halts further node output (P3 gate review item 3b) ────────


def test_lease_renewal_failure_marks_worker_not_alive_for_that_run(local_persistence):
    persistence = local_persistence
    run_id = "RUN-LEASE-NOT-ALIVE"
    _create(persistence, run_id, lambda: canonical_ts(0))

    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-heartbeat",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=lambda: canonical_ts(1),
        nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
    )
    try:
        with executor._lock:
            executor._active_runs.add(run_id)
        assert executor.worker_alive(run_id) is True

        executor._mark_lease_lost(run_id, canonical_ts(1))
        assert executor.worker_alive(run_id) is False

        events = [
            e for e in persistence.list_trace_events(run_id)
            if e["event_type"] == "run_lease_lost"
        ]
        assert events, "expected a run_lease_lost trace event"
    finally:
        with executor._lock:
            executor._active_runs.discard(run_id)
        executor.stop()


def test_lease_lost_stops_further_node_output(local_persistence):
    """End-to-end through the real admission/heartbeat loop and run_phase: once
    the heartbeat observes a failed renew_lease for this run, worker_alive()
    flips false and orchestrator.pipeline.run_phase's own per-node check
    (CLAUDE.md §9C) stops before the NEXT node -- proving no further node
    output is written after the lease is lost, not merely that the flag
    changed in isolation."""
    persistence = local_persistence
    run_id = "RUN-LEASE-LOST-E2E"
    # A REAL wall-clock, not the fast-forwarding `_make_clock()` -- with a
    # per-call incrementing clock, the many clock() calls the admission and
    # heartbeat loops make each real-time tick would race the lease past its
    # own TTL and let the (correct, separate) reaper mark the run
    # `interrupted` before this test's own assertions run, which would be
    # testing the reaper's behaviour by accident, not the lease-lost gate.
    clock = utc_now
    _create(persistence, run_id, clock)

    node1_entered = threading.Event()
    node1_release = threading.Event()
    node2_entered = threading.Event()

    def node1(ctx, state):
        node1_entered.set()
        node1_release.wait(timeout=10)
        return dataclasses.replace(state, events=state.events + [{"node": "n1"}])

    def node2(ctx, state):
        node2_entered.set()
        return dataclasses.replace(state, events=state.events + [{"node": "n2"}])

    nodes_for = {"fieldwork": {"plan": [("n1", node1), ("n2", node2)], "execute": [], "export": []}}

    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-lease-lost-e2e",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=clock, nodes_for=nodes_for, poll_interval_s=0.05, lease_ttl_s=30,
        heartbeat_interval_s=0.05,
    )
    persistence.renew_lease = lambda rid, wid, *, ttl_s, now: False
    try:
        executor.start()
        assert node1_entered.wait(timeout=10)

        deadline = time.time() + 10
        while time.time() < deadline and executor.worker_alive(run_id):
            time.sleep(0.02)
        assert not executor.worker_alive(run_id), "lease loss was never observed by the heartbeat"

        # Stop the background admission/heartbeat loops now, before letting node1
        # finish -- otherwise the (correct, separate) mid-session reaper would
        # race this test's own assertions: once node1 finishes, _on_done releases
        # this worker's lease row for run_id, and the very next admission tick
        # would legitimately reap the now-lease-less `running` run as orphaned
        # (CLAUDE.md §2.3 rule 2) before this test gets to look at it. That is
        # real, desired system behaviour (a lost-lease run becomes resumable
        # promptly rather than sitting unleased) -- just not what this test is
        # isolating, which is the lease-lost gate stopping node2 from running.
        executor._stop_event.set()

        node1_release.set()  # node1's own in-flight output is still written
        assert not node2_entered.wait(timeout=1), "node2 ran after the lease was lost"

        deadline = time.time() + 5
        final = persistence.load_state(run_id)
        while time.time() < deadline and final.next_node_index < 1:
            time.sleep(0.02)
            final = persistence.load_state(run_id)
        assert final.status == "running"  # run_phase returned mid-phase, not stuck or crashed
        assert final.next_node_index == 1  # only node1 completed

        events = [
            e for e in persistence.list_trace_events(run_id)
            if e["event_type"] == "run_lease_lost"
        ]
        assert events, "expected a run_lease_lost trace event"
    finally:
        node1_release.set()
        executor.stop()


# ── queue environment affinity (P3 gate review item 4) ─────────────────────


def _create_with_fingerprint(persistence, run_id: str, now_fn, fingerprint: dict):
    return runs_module.create_run(
        persistence, run_id=run_id, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None,
        skill_version=None, mode="playbook", audit_period=("2026-01-01", "2026-01-31"), objective="t",
        run_owner="alice", options={"auto_confirm_plan": True}, fingerprint=fingerprint, now=now_fn(),
    )


def _fingerprint_for_settings(fp_id: str, settings: Settings) -> dict:
    fp = _fingerprint(fp_id)
    fp["code_revision"] = settings.code_revision
    fp["runtime_config_hash"] = runtime_config_hash(settings)
    return fp


def test_executor_leaves_a_different_deployment_run_queued_and_untouched(local_persistence):
    """A run whose stored fingerprint's code_revision/runtime_config_hash do
    not match this worker's own deployment must never be claimed: no lease
    acquired, no admission-failure bookkeeping, status stays `queued` across
    several admission (and reap) ticks -- and the mid-session reaper, which
    only ever acts on `find_runs(['running'])`, structurally cannot touch a
    run that never left `queued` in the first place."""
    persistence = local_persistence
    clock = _make_clock()
    run_id = "RUN-FOREIGN-REVISION"

    own_settings = dataclasses.replace(Settings(), code_revision="rev-mine")
    foreign_settings = dataclasses.replace(Settings(), code_revision="rev-theirs")
    fp = _fingerprint_for_settings(f"FP-{run_id}", foreign_settings)
    _create_with_fingerprint(persistence, run_id, clock, fp)

    node_entered = threading.Event()

    def node(ctx, state):
        node_entered.set()
        return state

    executor = ThreadExecutor(
        persistence=persistence, settings=own_settings, worker_id="worker-mine",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint_for_settings(f"FP-{rid}", own_settings),
        clock=clock, nodes_for={"fieldwork": {"plan": [("n", node)], "execute": [], "export": []}},
        poll_interval_s=0.03,
    )
    try:
        executor.start()
        assert not node_entered.wait(timeout=1), "a different deployment's run was admitted"

        final = persistence.load_state(run_id)
        assert final.status == "queued"
        # never even attempted a lease -- expired_leases(FAR_FUTURE) lists every
        # run_id holding ANY lease row, live or expired (see executor._FAR_FUTURE).
        assert run_id not in persistence.expired_leases("9999-12-31T23:59:59.999999Z")
        assert run_id not in executor._admission_failures
    finally:
        executor.stop()


def test_two_executors_with_different_revisions_each_take_only_their_own_runs(local_persistence):
    """The scenario item 4 names directly: two executors (different deployments,
    same shared persistence -- the situation an in-place redeploy leaves for
    the moment both an old and a new container are polling) each admit only
    the queued run created under their own code revision, never the other's."""
    persistence = local_persistence
    clock = _make_clock()

    settings_a = dataclasses.replace(Settings(), code_revision="rev-A")
    settings_b = dataclasses.replace(Settings(), code_revision="rev-B")

    fp_a = _fingerprint_for_settings("FP-RUN-A", settings_a)
    fp_b = _fingerprint_for_settings("FP-RUN-B", settings_b)
    _create_with_fingerprint(persistence, "RUN-A", clock, fp_a)
    _create_with_fingerprint(persistence, "RUN-B", clock, fp_b)

    ran: list[tuple[str, str]] = []
    ran_lock = threading.Lock()

    def make_node(worker_label):
        def node(ctx, state):
            with ran_lock:
                ran.append((worker_label, state.run_id))
            return state
        return node

    executor_a = ThreadExecutor(
        persistence=persistence, settings=settings_a, worker_id="worker-A",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint_for_settings(f"FP-{rid}", settings_a),
        clock=clock, nodes_for={"fieldwork": {"plan": [("n", make_node("A"))], "execute": [], "export": []}},
        poll_interval_s=0.03,
    )
    executor_b = ThreadExecutor(
        persistence=persistence, settings=settings_b, worker_id="worker-B",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint_for_settings(f"FP-{rid}", settings_b),
        clock=clock, nodes_for={"fieldwork": {"plan": [("n", make_node("B"))], "execute": [], "export": []}},
        poll_interval_s=0.03,
    )
    try:
        executor_a.start()
        executor_b.start()

        deadline = time.time() + 10
        while time.time() < deadline:
            statuses = {rid: persistence.load_state(rid).status for rid in ("RUN-A", "RUN-B")}
            if all(s == "awaiting_signoff" for s in statuses.values()):
                break
            time.sleep(0.05)
        final_statuses = {rid: persistence.load_state(rid).status for rid in ("RUN-A", "RUN-B")}
        assert all(s == "awaiting_signoff" for s in final_statuses.values()), final_statuses

        with ran_lock:
            outcome = set(ran)
        assert ("A", "RUN-A") in outcome
        assert ("B", "RUN-B") in outcome
        assert ("A", "RUN-B") not in outcome, "worker A admitted worker B's run"
        assert ("B", "RUN-A") not in outcome, "worker B admitted worker A's run"
    finally:
        executor_a.stop()
        executor_b.stop()
