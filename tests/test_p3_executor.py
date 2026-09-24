"""ThreadExecutor tests (CLAUDE.md build brief P3 §3): admission, the
concurrency cap, lease takeover after expiry, StaleStateError being a
normal admission skip rather than a crash, bounded admission retries with
backoff (P3 gate review item 3a), and a lost lease halting further node
output (item 3b). Uses trivial injected node functions (not the real
fieldwork nodes) -- the executor does not know or care what a node does,
only the admission/lease/pool protocol around it, so these stay fast and
independent of any Skill or data fixture.

P3 gap-audit review (2026-09-24) adds: a lost direct-start signal being
recovered by `_pending_starts` without any idle persistence calls; a
worker's own reap tick never interrupting its own live run under a slow
renewal (root cause of a live false-interrupt, RUN-57B6B4BB2B33); the
`lease_reap_grace_s` buffer in isolation; and deterministic (event- and
injected-clock-driven, no background-loop-cadence wall-clock racing)
versions of the two previously timing-flaky tests."""

from __future__ import annotations

import dataclasses
import shutil
import threading
import time
from pathlib import Path

from orchestrator import runs as runs_module
from orchestrator import service
from orchestrator.config import Settings, runtime_config_hash
from orchestrator.executor import ThreadExecutor, reap_orphaned_runs_with_leases
from orchestrator.status import transition as _transition_status
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
    """Deterministic under load (P3 gap-audit review, item 3): concurrency is
    enforced by a real threading.Semaphore(2) inside ThreadExecutor, which
    does not depend on wall-clock timing at all -- the only genuinely
    time-based waits here are for real asynchronous work (two nodes
    entering, then a third, then all three reaching awaiting_signoff), each
    bounded by a generous timeout tolerant of heavy CPU contention (proven
    at 20x under 3x parallel CPU load). The "exactly two, never a third"
    assertion is taken from a PRECISE PERSISTED-STATE SNAPSHOT at the
    instant both expected entries have fired, never from a short negative
    wait racing the background loop's own cadence -- the previous version's
    `assert not entered.acquire(timeout=1)` could only ever get weaker
    under contention (a slower admission tick just makes the negative
    easier to observe), so it added flakiness risk for no correctness
    benefit; dropping it in favour of the state snapshot keeps the
    assertion exact rather than merely making it less likely to fire."""
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
        release.wait(timeout=30)
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
        assert entered.acquire(timeout=30)
        assert entered.acquire(timeout=30)
        with lock:
            assert concurrent_count["max"] == 2

        # A precise, non-racy snapshot at the instant both expected entries
        # have fired: the third run is `queued`, the other two `running`.
        statuses = {rid: persistence.load_state(rid).status for rid in run_ids}
        assert list(statuses.values()).count("queued") == 1
        assert list(statuses.values()).count("running") == 2

        release.set()
        assert entered.acquire(timeout=30)  # the third now gets admitted

        # auto_confirm_plan is set (options in _create), and the `execute`
        # phase's node list is empty here, so each run walks straight through
        # plan -> execute -> awaiting_signoff with no gate in between.
        deadline = time.time() + 30
        while time.time() < deadline:
            if all(persistence.load_state(rid).status == "awaiting_signoff" for rid in run_ids):
                break
            time.sleep(0.05)
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


def test_idle_sweep_disabled_makes_zero_persistence_calls_after_startup(local_persistence):
    """CLAUDE.md P3 fix (2026-09-24): EXECUTOR_IDLE_POLL_INTERVAL_S now
    defaults to 0 (off) -- even a 10-minute sweep still woke a 1-minute-
    auto-stop warehouse ~6x/hour with nothing queued or running. With the
    idle sweep disabled, the loop's first tick (always immediate on
    start()) must still find no work, but must then BLOCK on _wake_event
    rather than polling on any timer -- so it must make no further
    persistence calls at all over a simulated idle period, however long."""
    persistence = local_persistence
    calls = {"n": 0}
    orig_find_runs = persistence.find_runs

    def counting_find_runs(statuses):
        calls["n"] += 1
        return orig_find_runs(statuses)

    persistence.find_runs = counting_find_runs

    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-idle-off",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=_make_clock(), nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
        poll_interval_s=0.05, idle_poll_interval_s=0.0,
    )
    try:
        executor.start()
        # Would be ~10 ticks' worth of find_runs calls if the loop never
        # blocked and instead kept polling at the (fast, 0.05s) ACTIVE cadence.
        time.sleep(0.5)
        assert executor._next_poll_interval() is None
    finally:
        executor.stop()

    # _reap_orphans + _admit_all_queued each call find_runs once per tick, so
    # 2 calls = the one tick the loop must still take immediately on
    # start(); anything beyond that means the loop never actually blocked.
    assert calls["n"] <= 2, calls["n"]


def test_queued_at_startup_runs_are_still_admitted_with_idle_sweep_disabled(local_persistence):
    """A run already `queued` before the loop starts must still be admitted
    on the loop's first (always-immediate) tick, even with the idle safety
    sweep fully disabled -- disabling the periodic sweep must never mean
    disabling the one-time startup sweep."""
    persistence = local_persistence
    run_id = "RUN-STARTUP-IDLE-OFF"
    _create(persistence, run_id, _make_clock())

    done = threading.Event()

    def quick_node(ctx, state):
        done.set()
        return state

    nodes_for = {"fieldwork": {"plan": [("quick", quick_node)], "execute": [], "export": []}}
    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-startup-idle-off",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=_make_clock(), nodes_for=nodes_for,
        poll_interval_s=0.05, idle_poll_interval_s=0.0,
    )
    try:
        executor.start()  # background-loop start, not a direct start_audit_run wake
        assert done.wait(timeout=5)
    finally:
        executor.stop()


def test_stop_unblocks_a_loop_parked_on_the_disabled_idle_wake(local_persistence):
    """stop() must not be bounded by the (disabled, effectively infinite)
    idle wait -- it sets _wake_event as well as _stop_event, so a loop
    currently blocked with nothing to do still shuts down promptly."""
    persistence = local_persistence
    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-stop-idle-off",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=_make_clock(), nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
        poll_interval_s=0.05, idle_poll_interval_s=0.0,
    )
    executor.start()
    time.sleep(0.2)  # let the loop reach its blocked idle-wait
    start = time.time()
    executor.stop()
    assert time.time() - start < 3, "stop() waited on the disabled idle sweep instead of _wake_event"


def test_direct_wake_lets_a_parked_loop_take_another_tick(local_persistence):
    """With the idle sweep disabled, the ONLY thing that should ever make the
    background loop call find_runs again after its first startup tick is a
    direct start(run_id, phase) call (the same entry point start_audit_run/
    confirm_plan/sign_off/resume_run all use) -- proving the wake is real,
    not merely that the loop happens to still be running."""
    persistence = local_persistence
    calls = {"n": 0}
    orig_find_runs = persistence.find_runs

    def counting_find_runs(statuses):
        calls["n"] += 1
        return orig_find_runs(statuses)

    persistence.find_runs = counting_find_runs

    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-wake-nudge",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=_make_clock(), nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
        poll_interval_s=0.05, idle_poll_interval_s=0.0,
    )
    try:
        executor.start()
        time.sleep(0.2)  # let the loop finish its one startup tick and park
        before = calls["n"]
        assert before <= 2

        # Isolate the wake signal itself from _try_admit's own persistence
        # calls (acquire_lease etc., already covered by other tests) --
        # start(run_id, phase) is the exact entry point start_audit_run/
        # confirm_plan/sign_off/resume_run all call.
        executor._try_admit = lambda rid: None
        executor.start("RUN-ANY", "plan")
        time.sleep(0.2)  # give the nudged loop a moment to take its extra tick

        assert calls["n"] > before, "the wake signal never reached the parked loop"
    finally:
        executor.stop()


def test_on_done_wakes_the_loop_promptly_for_a_queued_run_waiting_on_the_freed_slot(local_persistence):
    """P3 gate review: 'make sure _on_done still admits queued runs after
    the last active run finishes' -- with a single concurrency slot, a run
    still queued behind it must be admitted PROMPTLY once that run
    finishes, not only after the (deliberately slow, here) active poll
    interval next elapses. Proves _on_done's own wake signal does the work,
    not a lucky coincidence of timing."""
    persistence = local_persistence
    clock = _make_clock()
    run_a, run_c = "RUN-SLOT-A", "RUN-SLOT-C"
    _create(persistence, run_a, clock)
    _create(persistence, run_c, clock)

    a_entered = threading.Event()
    a_release = threading.Event()
    c_entered = threading.Event()

    def node_a(ctx, state):
        a_entered.set()
        a_release.wait(timeout=10)
        return dataclasses.replace(state, events=state.events + [{"node": "a"}])

    def node_c(ctx, state):
        c_entered.set()
        return dataclasses.replace(state, events=state.events + [{"node": "c"}])

    # Both runs share the same node list (nodes_for is keyed by run_kind/
    # phase, not by run_id) -- dispatch to a per-run node function by
    # run_id so run C's own entry (node_c) is what distinguishes "run C got
    # admitted" from "run A is still occupying the only slot".
    def dispatch(ctx, state):
        return (node_a if state.run_id == run_a else node_c)(ctx, state)

    nodes_for = {"fieldwork": {"plan": [("n", dispatch)], "execute": [], "export": []}}

    class _SettingsOneSlot:
        max_concurrent_runs = 1

    executor = ThreadExecutor(
        persistence=persistence, settings=_SettingsOneSlot(), worker_id="worker-slot",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=clock, nodes_for=nodes_for,
        # Deliberately slow active cadence -- if C's admission depended on
        # waiting out this interval rather than _on_done's wake, this test
        # would take ~5s instead of well under 1s.
        poll_interval_s=5.0, idle_poll_interval_s=0.0, lease_ttl_s=30,
    )
    try:
        executor.start()
        assert a_entered.wait(timeout=10)
        assert not c_entered.is_set()  # C is queued, waiting on the only slot

        start = time.time()
        a_release.set()
        assert c_entered.wait(timeout=2), "run C was not admitted promptly after run A finished"
        elapsed = time.time() - start
        assert elapsed < 2, f"run C's admission took {elapsed:.2f}s -- bounded by the active interval, not the wake"
    finally:
        a_release.set()
        executor.stop()


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
    """End-to-end through the real run_phase (via _run_one, the same code
    path _try_admit submits to the thread pool), proving no further node
    output is written after the lease is lost -- deterministic (P3
    gap-audit review, item 3): the background admission/heartbeat loops are
    never started, so nothing here depends on poll_interval_s or
    heartbeat_interval_s cadence racing against wall-clock timeouts. The
    lease-lost signal is injected at a point this test controls directly
    (_mark_lease_lost, exactly what the heartbeat thread itself would call
    on a failed renew_lease -- test_lease_renewal_failure_marks_worker_
    not_alive_for_that_run above proves that wiring in isolation), once
    node1 is confirmed running. Only genuinely asynchronous facts (node1
    entering, node2 NOT entering, the pipeline thread finishing) are waited
    on via Events/future.result, each with a generous but bounded timeout --
    never a fixed-cadence background poll."""
    persistence = local_persistence
    clock = _make_clock()
    run_id = "RUN-LEASE-LOST-E2E"
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
        clock=clock, nodes_for=nodes_for,
    )
    # Acquire the lease and submit run_phase directly -- the SAME work
    # _try_admit does, without starting the background admission/heartbeat
    # threads (so their cadence never enters this test at all).
    assert persistence.acquire_lease(run_id, executor._worker_id, ttl_s=30, now=clock())
    with executor._lock:
        executor._active_runs.add(run_id)
    future = executor._pool.submit(executor._run_one, run_id)
    try:
        assert node1_entered.wait(timeout=10)
        assert executor.worker_alive(run_id) is True

        # The exact call the heartbeat thread itself makes on a failed
        # renew_lease -- injected here, deterministically, once node1 is
        # confirmed running.
        executor._mark_lease_lost(run_id, clock())
        assert executor.worker_alive(run_id) is False

        node1_release.set()  # node1's own in-flight output is still written
        assert not node2_entered.wait(timeout=2), "node2 ran after the lease was lost"

        future.result(timeout=10)  # run_phase returns cleanly, never raises

        final = persistence.load_state(run_id)
        assert final.status == "running"  # returned mid-phase, not stuck or crashed
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


# ── lost start signal recovered without idle warehouse polling (P3 gap-audit
#    review item 1) ──────────────────────────────────────────────────────────


def test_pending_starts_keeps_the_loop_busy_until_resolved_with_zero_idle_sql(local_persistence):
    """Deterministic unit test (no threads, no wall clock) of the
    `_pending_starts` bookkeeping itself: `_next_poll_interval()` must report
    the ACTIVE cadence while a start(run_id, phase) attempt is outstanding,
    and fall back to idle (None, meaning zero further persistence calls)
    once it resolves -- proving the self-heal mechanism does not itself
    become a new source of idle polling."""
    persistence = local_persistence
    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-pending-unit",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=_make_clock(), nodes_for={"fieldwork": {"plan": [], "execute": [], "export": []}},
        poll_interval_s=0.05, idle_poll_interval_s=0.0,
    )
    try:
        assert executor._next_poll_interval() is None  # nothing outstanding -- idle

        with executor._lock:
            executor._pending_starts.add("RUN-PENDING")
        assert executor._next_poll_interval() == 0.05  # busy: something is still unresolved

        with executor._lock:
            executor._pending_starts.discard("RUN-PENDING")
        assert executor._next_poll_interval() is None  # resolved -- idle again
    finally:
        executor.stop()


def test_lost_direct_admission_signal_is_recovered_without_idle_sql(local_persistence):
    """CLAUDE.md build brief P3 gap-audit review, root cause of
    RUN-D452E2A3832A sitting `queued` until an App restart: a direct
    start(run_id, phase) call whose own synchronous _try_admit does not
    succeed (here: a transient acquire_lease failure) must still be admitted
    promptly by the SAME running executor -- proven with the real
    (unmodified) wake_event, so this is the realistic path, not a
    manufactured "no wake could ever work" scenario. The run is recorded in
    `_pending_starts` for the whole outstanding window (independent
    evidence the new mechanism, not a lucky coincidence, is what is
    tracking it) and cleared once resolved. Confirms zero persistence calls
    while genuinely idle beforehand, exactly like the existing
    idle-sweep-disabled test."""
    persistence = local_persistence
    run_id = "RUN-LOST-SIGNAL"

    calls = {"n": 0}
    orig_find_runs = persistence.find_runs

    def counting_find_runs(statuses):
        calls["n"] += 1
        return orig_find_runs(statuses)

    persistence.find_runs = counting_find_runs

    done = threading.Event()

    def quick_node(ctx, state):
        done.set()
        return state

    nodes_for = {"fieldwork": {"plan": [("quick", quick_node)], "execute": [], "export": []}}
    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-lost-signal",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=_make_clock(), nodes_for=nodes_for,
        poll_interval_s=0.05, idle_poll_interval_s=0.0,
    )
    orig_acquire = persistence.acquire_lease
    try:
        # The background loop is running and genuinely idle first -- NO run
        # exists yet at all -- zero persistence calls beyond its one
        # guaranteed startup tick, exactly like the existing
        # idle-sweep-disabled test.
        executor.start()
        time.sleep(0.2)
        assert calls["n"] <= 2, "the loop polled while genuinely idle before the run existed"

        # Now the run is created (a real start_audit_run would write this row
        # and then call executor.start(run_id, phase) -- the row must exist
        # first, exactly as it does here), and the FIRST synchronous
        # admission attempt inside start(run_id, phase) fails transiently.
        _create(persistence, run_id, _make_clock())
        outcomes = iter([False])
        persistence.acquire_lease = lambda rid, wid, *, ttl_s, now: next(outcomes, True)

        executor.start(run_id, "plan")  # direct call: synchronous _try_admit fails on this one attempt

        # _pending_starts is what is tracking this run as outstanding right
        # after the failed attempt -- not merely inferred from the eventual
        # outcome.
        with executor._lock:
            assert run_id in executor._pending_starts, "the failed attempt was not tracked as pending"

        assert done.wait(timeout=5), "the run was never admitted after the transient failure"
        with executor._lock:
            assert run_id not in executor._pending_starts, "resolved run left dangling in _pending_starts"
    finally:
        persistence.acquire_lease = orig_acquire
        executor.stop()


def test_lost_signal_recovery_also_works_when_the_background_loop_was_never_started(local_persistence):
    """The other half of the same fix: start(run_id, phase) must itself
    ensure the background admission/heartbeat loop is running -- previously,
    if the no-arg App-start call had never been made (or the loop had died),
    a run_id call's own wake_event.set() woke nobody, ever."""
    persistence = local_persistence
    run_id = "RUN-NO-LOOP-YET"
    _create(persistence, run_id, _make_clock())

    done = threading.Event()

    def quick_node(ctx, state):
        done.set()
        return state

    nodes_for = {"fieldwork": {"plan": [("quick", quick_node)], "execute": [], "export": []}}
    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-no-loop-yet",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=_make_clock(), nodes_for=nodes_for,
        poll_interval_s=0.05, idle_poll_interval_s=0.0,
    )
    assert executor._admission_thread is None  # the no-arg start() was never called
    try:
        executor.start(run_id, "plan")
        assert done.wait(timeout=5)
        assert executor._admission_thread is not None and executor._admission_thread.is_alive()
    finally:
        executor.stop()


# ── grace period + self-exclusion protect a live lease under latency (P3
#    gap-audit review item 2, root cause of RUN-57B6B4BB2B33) ─────────────────


def test_reap_orphaned_runs_with_leases_grace_period_tolerates_a_recently_expired_lease(local_persistence):
    """Unit-level test of `grace_s` alone, no concurrency: a lease that
    crossed raw expiry only recently is NOT reaped; the same lease once it
    is past expiry by more than the grace window IS reaped. Isolates
    grace_s from exclude_run_ids (covered separately below)."""
    persistence = local_persistence
    run_id = "RUN-GRACE"
    state = _create(persistence, run_id, lambda: "2026-01-01T00:00:00.000000Z")
    running_state = _transition_status(state, "running", now="2026-01-01T00:00:00.000000Z")
    persistence.save_state(running_state)
    persistence.acquire_lease(run_id, "peer-worker", ttl_s=10, now="2026-01-01T00:00:00.000000Z")
    # lease_expires_at == 2026-01-01T00:00:10

    just_past_raw_expiry = "2026-01-01T00:00:12.000000Z"  # 2s past raw expiry
    reaped = reap_orphaned_runs_with_leases(persistence, now=just_past_raw_expiry, grace_s=30.0)
    assert reaped == []
    assert persistence.load_state(run_id).status == "running"

    well_past_grace = "2026-01-01T00:00:45.000000Z"  # 35s past raw expiry, > 30s grace
    reaped = reap_orphaned_runs_with_leases(persistence, now=well_past_grace, grace_s=30.0)
    assert reaped == [run_id]
    assert persistence.load_state(run_id).status == "interrupted"


def test_reap_grace_never_delays_a_run_that_was_never_leased_at_all(local_persistence):
    """grace_s must apply only to leases that WERE acquired and look
    expired -- a run left `running` with NO lease row at all (a
    pre-P3-shaped orphan) is a structurally different, unambiguous orphan
    and is reaped immediately regardless of grace."""
    persistence = local_persistence
    run_id = "RUN-NEVER-LEASED"
    state = _create(persistence, run_id, lambda: "2026-01-01T00:00:00.000000Z")
    running_state = _transition_status(state, "running", now="2026-01-01T00:00:00.000000Z")
    persistence.save_state(running_state)

    reaped = reap_orphaned_runs_with_leases(persistence, now="2026-01-01T00:00:01.000000Z", grace_s=300.0)
    assert reaped == [run_id]
    assert persistence.load_state(run_id).status == "interrupted"


def test_reap_never_interrupts_a_live_run_whose_own_renewal_is_slow(local_persistence):
    """Root cause of a live false-interrupt under warehouse contention
    (RUN-57B6B4BB2B33, CLAUDE.md build brief P3 gap-audit review): a
    worker's OWN admission-loop reap tick must never mark its own
    actively-running task's run `interrupted`, even when that SAME worker's
    heartbeat renewal for it is running slower than the lease TTL. Injects a
    slow renew_lease (blocks well past the TTL) and drives real reap ticks
    concurrently, with lease_reap_grace_s explicitly disabled -- proving the
    exclude-own-active-runs fix alone (not the grace buffer) is what
    protects the live run."""
    persistence = local_persistence
    run_id = "RUN-SLOW-RENEWAL"
    _create(persistence, run_id, lambda: canonical_ts(0))

    node_entered = threading.Event()
    node_release = threading.Event()

    def blocking_node(ctx, state):
        node_entered.set()
        node_release.wait(timeout=10)
        return dataclasses.replace(state, events=state.events + [{"node": "n"}])

    nodes_for = {"fieldwork": {"plan": [("n", blocking_node)], "execute": [], "export": []}}

    renewal_started = threading.Event()
    renewal_may_return = threading.Event()
    orig_renew = persistence.renew_lease

    def slow_renew_lease(rid, wid, *, ttl_s, now):
        renewal_started.set()
        renewal_may_return.wait(timeout=10)  # simulate warehouse contention outlasting the TTL
        return orig_renew(rid, wid, ttl_s=ttl_s, now=now)

    persistence.renew_lease = slow_renew_lease

    # A REAL wall clock (not _make_clock()'s fast-forwarding logical clock):
    # lease_expires_at is compared against real elapsed time by the admission
    # loop's own reap ticks while the renewal above is deliberately blocked.
    executor = ThreadExecutor(
        persistence=persistence, settings=_Settings(), worker_id="worker-slow-renew",
        ctx_factory=lambda rid: object(),
        fingerprint_factory=lambda rid: _fingerprint(f"FP-{rid}"),
        clock=utc_now, nodes_for=nodes_for,
        poll_interval_s=0.05, heartbeat_interval_s=0.1, lease_ttl_s=1,
        lease_reap_grace_s=0.0,  # no extra tolerance -- isolates the exclude-own-active fix
    )
    try:
        executor.start()
        assert node_entered.wait(timeout=10)
        assert renewal_started.wait(timeout=10)

        # The lease has now genuinely crossed its 1s TTL (renewal is still
        # blocked) -- give several reap ticks (poll_interval_s=0.05) the
        # chance to wrongly act on that before releasing the renewal.
        time.sleep(0.5)
        assert persistence.load_state(run_id).status == "running", (
            "the worker's own reap tick interrupted its own live run under a slow renewal"
        )

        renewal_may_return.set()
        node_release.set()
        deadline = time.time() + 10
        status = persistence.load_state(run_id).status
        while time.time() < deadline and status == "running":
            time.sleep(0.05)
            status = persistence.load_state(run_id).status
        assert status == "awaiting_signoff", status
    finally:
        renewal_may_return.set()
        node_release.set()
        executor.stop()
