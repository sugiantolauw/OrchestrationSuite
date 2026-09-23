"""ThreadExecutor tests (CLAUDE.md build brief P3 §3): admission, the
concurrency cap, lease takeover after expiry, and StaleStateError being a
normal admission skip rather than a crash. Uses trivial injected node
functions (not the real fieldwork nodes) -- the executor does not know or
care what a node does, only the admission/lease/pool protocol around it, so
these stay fast and independent of any Skill or data fixture."""

from __future__ import annotations

import dataclasses
import shutil
import threading
import time
from pathlib import Path

from orchestrator import runs as runs_module
from orchestrator import service
from orchestrator.executor import ThreadExecutor
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
