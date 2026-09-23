from __future__ import annotations

import dataclasses
import multiprocessing
import threading

import pytest

from orchestrator import runs
from orchestrator.errors import InvalidTransition, StaleStateError
from orchestrator.pipeline import run_phase
from orchestrator.reaper import reap_orphaned_runs
from orchestrator.status import transition
from orchestrator.state import RunState


def _fingerprint(fp_id="FP-FI"):
    return dict(
        fingerprint_id=fp_id,
        source_table_versions="{}",
        uploaded_file_hashes="{}",
        skill_content_hash=None,
        code_revision="rev1",
        dependency_lock_hash="dep1",
        runtime_config_hash="rc1",
        endpoint_config="{}",
        prompt_template_version="none",
        created_at="2026-01-01T00:00:00+00:00",
    )


def _make_clock():
    counter = {"n": 0}

    def clock():
        counter["n"] += 1
        return f"t{counter['n']}"

    return clock


def _trivial_node(counts, name):
    def fn(skill, state):
        counts[name] = counts.get(name, 0) + 1
        return dataclasses.replace(state, events=state.events + [{"node": name}])

    return fn


def _nodes_for(counts, names):
    return {"fieldwork": {"plan": [(n, _trivial_node(counts, n)) for n in names], "execute": [], "export": []}}


# ── (a) crash after complete_node_attempt(succeeded) but before save_state ─────────────────


def test_write_ahead_recovery_node_not_re_executed(local_persistence):
    persistence = local_persistence
    clock = _make_clock()
    call_counts: dict[str, int] = {}
    nodes_for = _nodes_for(call_counts, ["discover", "profile", "plan"])

    state = runs.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None,
        skill_version=None, mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
        objective="t", run_owner="alice", fingerprint=_fingerprint(), now=clock(),
    )

    orig_complete = persistence.complete_node_attempt
    orig_save_state = persistence.save_state
    arm = {"raise_next_save": False}

    def patched_complete(execution_key, **kwargs):
        orig_complete(execution_key, **kwargs)
        if kwargs.get("outcome") == "succeeded":
            attempt = [a for a in persistence.list_node_attempts(state.run_id) if a["execution_key"] == execution_key][0]
            if attempt["node_name"] == "profile":
                arm["raise_next_save"] = True

    def patched_save_state(s):
        if arm["raise_next_save"]:
            arm["raise_next_save"] = False
            raise RuntimeError("simulated crash before save_state")
        return orig_save_state(s)

    persistence.complete_node_attempt = patched_complete
    persistence.save_state = patched_save_state
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock)
    finally:
        persistence.complete_node_attempt = orig_complete
        persistence.save_state = orig_save_state

    loaded = persistence.load_state(state.run_id)
    assert loaded.status == "running"
    assert loaded.next_node_index == 1  # profile's completion was never persisted

    reap_orphaned_runs(persistence, now=clock())
    resumed = runs.resume(persistence, state.run_id, actor="alice", now=clock(), current_fingerprint=_fingerprint())
    assert resumed.status == "queued"

    final = run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock)
    assert final.status == "awaiting_confirmation"
    assert call_counts["discover"] == 1
    assert call_counts["profile"] == 1
    assert call_counts["plan"] == 1

    attempts = {a["node_name"]: a for a in persistence.list_node_attempts(state.run_id)}
    assert all(a["outcome"] == "succeeded" for a in attempts.values())
    assert attempts["profile"]["attempt_number"] == 1


# ── (b) duplicate CAS: two saves with the same expected version ────────────────────────────


def test_duplicate_cas_second_call_rejected(persistence):
    created = persistence.create_run(
        RunState(
            run_id="RUN-DUPCAS", run_kind="fieldwork", mode="playbook", phase="plan",
            audit_period=("2026-01-01", "2026-01-31"), objective="t", run_owner="alice",
            fingerprint_id="FP-DUPCAS", created_at="t0", last_state_change_at="t0",
            status="queued", engagement_id="ENG-DEFAULT",
        ),
        _fingerprint("FP-DUPCAS"),
    )
    running = dataclasses.replace(created, status="running")
    persistence.save_state(running)  # first call succeeds (version 1 -> 2)

    with pytest.raises(StaleStateError):
        persistence.save_state(running)  # second call with the same stale expected version


# ── (c) two resume requests racing on the same interrupted run ─────────────────────────────


def test_concurrent_resume_only_one_succeeds_threads(local_persistence):
    persistence = local_persistence
    clock = _make_clock()
    created = persistence.create_run(
        RunState(
            run_id="RUN-RACE-T", run_kind="fieldwork", mode="playbook", phase="plan",
            audit_period=("2026-01-01", "2026-01-31"), objective="t", run_owner="alice",
            fingerprint_id="FP-RACE-T", created_at="t0", last_state_change_at="t0",
            status="queued", engagement_id="ENG-DEFAULT",
        ),
        _fingerprint("FP-RACE-T"),
    )
    running = transition(created, "running", now="t1")
    persistence.save_state(running)
    interrupted = transition(persistence.load_state("RUN-RACE-T"), "interrupted", now="t2")
    persistence.save_state(interrupted)

    barrier = threading.Barrier(2)
    results = []

    def attempt_resume():
        barrier.wait()
        try:
            r = runs.resume(persistence, "RUN-RACE-T", actor="alice", now="t3", current_fingerprint=_fingerprint("FP-RACE-T"))
            results.append(("ok", r.status))
        except (StaleStateError, InvalidTransition):
            # Both are the accepted "lost the race" outcomes (CLAUDE.md §9C scenario c):
            # a CAS rejection, or an InvalidTransition after the loser re-loads state that
            # the winner already advanced to 'queued'.
            results.append(("lost", None))

    threads = [threading.Thread(target=attempt_resume) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r[0] == "ok"]
    losers = [r for r in results if r[0] == "lost"]
    assert len(oks) == 1
    assert len(losers) == 1

    resumed_events = [e for e in persistence.list_trace_events("RUN-RACE-T") if e["event_type"] == "resumed"]
    assert len(resumed_events) == 1

    final = persistence.load_state("RUN-RACE-T")
    assert final.status == "queued"


def _child_resume(db_path, ddl_dir, run_id, fp_dict, barrier_file, result_file):
    import os
    import time

    from orchestrator.adapters.persistence_local import LocalPersistence
    from orchestrator import runs as runs_mod
    from orchestrator.errors import InvalidTransition as IT
    from orchestrator.errors import StaleStateError as SSE

    p = LocalPersistence(db_path, ddl_dir=ddl_dir)

    with open(barrier_file, "a") as f:
        f.write("ready\n")
    while True:
        with open(barrier_file) as f:
            if len(f.readlines()) >= 2:
                break
        time.sleep(0.01)

    try:
        r = runs_mod.resume(p, run_id, actor="proc", now=f"t-{os.getpid()}", current_fingerprint=fp_dict)
        outcome = f"ok:{r.status}"
    except (SSE, IT):
        # Both are the accepted "lost the race" outcomes (CLAUDE.md §9C scenario c).
        outcome = "lost"
    except Exception as exc:  # pragma: no cover - diagnostic aid only
        outcome = f"error:{exc!r}"

    with open(result_file, "a") as f:
        f.write(outcome + "\n")


def test_concurrent_resume_only_one_succeeds_processes(tmp_path):
    from orchestrator.adapters.persistence_local import LocalPersistence

    db_path = str(tmp_path / "race.db")
    p = LocalPersistence(db_path)
    p.migrate()
    fp = _fingerprint("FP-RACE-P")
    created = p.create_run(
        RunState(
            run_id="RUN-RACE-P", run_kind="fieldwork", mode="playbook", phase="plan",
            audit_period=("2026-01-01", "2026-01-31"), objective="t", run_owner="alice",
            fingerprint_id="FP-RACE-P", created_at="t0", last_state_change_at="t0",
            status="queued", engagement_id="ENG-DEFAULT",
        ),
        fp,
    )
    running = transition(created, "running", now="t1")
    p.save_state(running)
    interrupted = transition(p.load_state("RUN-RACE-P"), "interrupted", now="t2")
    p.save_state(interrupted)

    barrier_file = str(tmp_path / "barrier.txt")
    result_file = str(tmp_path / "results.txt")
    open(barrier_file, "w").close()
    open(result_file, "w").close()

    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_child_resume, args=(db_path, str(p.ddl_dir), "RUN-RACE-P", fp, barrier_file, result_file))
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)

    with open(result_file) as f:
        outcomes = [line.strip() for line in f if line.strip()]

    assert len(outcomes) == 2
    ok_outcomes = [o for o in outcomes if o.startswith("ok:")]
    lost_outcomes = [o for o in outcomes if o == "lost"]
    assert len(ok_outcomes) == 1, outcomes
    assert len(lost_outcomes) == 1, outcomes

    final = p.load_state("RUN-RACE-P")
    assert final.status == "queued"


# ── (d) executor dies mid-node: attempt left with outcome NULL ─────────────────────────────


class _SimulatedProcessDeath(BaseException):
    pass


def test_executor_dies_mid_node_only_that_node_reexecutes(local_persistence):
    persistence = local_persistence
    clock = _make_clock()
    call_counts: dict[str, int] = {}

    def discover_fn(skill, state):
        call_counts["discover"] = call_counts.get("discover", 0) + 1
        return dataclasses.replace(state, events=state.events + [{"node": "discover"}])

    def profile_fn(skill, state):
        call_counts["profile"] = call_counts.get("profile", 0) + 1
        if call_counts["profile"] == 1:
            raise _SimulatedProcessDeath("process died mid-node")
        return dataclasses.replace(state, events=state.events + [{"node": "profile"}])

    def plan_fn(skill, state):
        call_counts["plan"] = call_counts.get("plan", 0) + 1
        return dataclasses.replace(state, events=state.events + [{"node": "plan"}])

    nodes_for = {"fieldwork": {"plan": [("discover", discover_fn), ("profile", profile_fn), ("plan", plan_fn)], "execute": [], "export": []}}

    state = runs.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None,
        skill_version=None, mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
        objective="t", run_owner="alice", fingerprint=_fingerprint("FP-DIE"), now=clock(),
    )

    with pytest.raises(_SimulatedProcessDeath):
        run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock)

    attempts = {a["node_name"]: a for a in persistence.list_node_attempts(state.run_id)}
    assert attempts["discover"]["outcome"] == "succeeded"
    assert attempts["profile"]["outcome"] is None  # left open, simulating a dead process

    reap_orphaned_runs(persistence, now=clock())
    attempts_after_reap = {a["node_name"]: a for a in persistence.list_node_attempts(state.run_id)}
    assert attempts_after_reap["profile"]["outcome"] == "interrupted"

    resumed = runs.resume(persistence, state.run_id, actor="alice", now=clock(), current_fingerprint=_fingerprint("FP-DIE"))
    assert resumed.status == "queued"

    final = run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock)
    assert final.status == "awaiting_confirmation"

    assert call_counts["discover"] == 1  # earlier node not re-run
    assert call_counts["profile"] == 2  # this node re-executed once
    assert call_counts["plan"] == 1

    final_attempts = [a for a in persistence.list_node_attempts(state.run_id) if a["node_name"] == "profile"]
    assert len(final_attempts) == 2
    assert final_attempts[0]["attempt_number"] == 1
    assert final_attempts[0]["outcome"] == "interrupted"
    assert final_attempts[1]["attempt_number"] == 2
    assert final_attempts[1]["outcome"] == "succeeded"
