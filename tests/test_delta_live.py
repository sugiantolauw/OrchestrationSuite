from __future__ import annotations

import dataclasses
import multiprocessing
import threading
import time

import pytest

from orchestrator.errors import InvalidTransition, StaleStateError
from orchestrator.state import RunState
from orchestrator.timeutil import utc_now
from tests.conftest import canonical_ts

# Delta-only live concurrency checks (CLAUDE.md §9C "Concurrency model", P1A gate
# review). These skip entirely unless RUN_DELTA_TESTS=1 -- there is nothing to check
# against local_memory/local_file, which are single-connection by construction. Every
# test here opens its OWN DeltaPersistence instance(s) (own connection) per actor, since
# a shared instance would serialise through its internal connection lock and defeat the
# point of the test.


def _fingerprint(fp_id):
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


def _state(run_id, **overrides):
    base = dict(
        run_id=run_id, run_kind="fieldwork", mode="playbook", phase="plan",
        audit_period=("2026-01-01", "2026-01-31"), objective="t", run_owner="alice",
        fingerprint_id=f"FP-{run_id}", created_at=canonical_ts(0), last_state_change_at=canonical_ts(0),
        status="queued", engagement_id="ENG-DEFAULT",
    )
    base.update(overrides)
    return RunState(**base)


def test_concurrent_cas_on_different_runs_does_not_conflict(delta_settings, uid):
    from orchestrator.adapters.persistence_delta import DeltaPersistence

    p = DeltaPersistence(delta_settings)
    run_a, run_b = f"RUN-CONC-A-{uid}", f"RUN-CONC-B-{uid}"
    state_a = p.create_run(_state(run_a), _fingerprint(f"FP-{run_a}"))
    state_b = p.create_run(_state(run_b), _fingerprint(f"FP-{run_b}"))

    p_a = DeltaPersistence(delta_settings)
    p_b = DeltaPersistence(delta_settings)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(pinst, state, key):
        barrier.wait()
        saved = pinst.save_state(dataclasses.replace(state, status="running"))
        results[key] = saved.state_version

    t1 = threading.Thread(target=worker, args=(p_a, state_a, "a"))
    t2 = threading.Thread(target=worker, args=(p_b, state_b, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    # Row-level concurrency: two CAS writes to DIFFERENT rows must both succeed --
    # neither may be rejected as though they were contending for the same row.
    assert results == {"a": 2, "b": 2}
    assert p.load_state(run_a).status == "running"
    assert p.load_state(run_b).status == "running"


def test_concurrent_cas_on_same_run_exactly_one_wins(delta_settings, uid):
    from orchestrator.adapters.persistence_delta import DeltaPersistence

    p = DeltaPersistence(delta_settings)
    run_id = f"RUN-CONC-SAME-{uid}"
    state = p.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))

    p_c = DeltaPersistence(delta_settings)
    p_d = DeltaPersistence(delta_settings)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(pinst, key):
        barrier.wait()
        try:
            saved = pinst.save_state(dataclasses.replace(state, status="running"))
            results[key] = ("ok", saved.state_version)
        except StaleStateError as exc:
            results[key] = ("stale", exc.expected_version, exc.actual_version)

    t1 = threading.Thread(target=worker, args=(p_c, "c"))
    t2 = threading.Thread(target=worker, args=(p_d, "d"))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    outcomes = list(results.values())
    oks = [o for o in outcomes if o[0] == "ok"]
    stales = [o for o in outcomes if o[0] == "stale"]
    assert len(oks) == 1, results
    assert len(stales) == 1, results
    assert stales[0][1] == 1  # loser's expected_version was still 1
    assert p.load_state(run_id).state_version == 2


def test_concurrent_duplicate_begin_node_attempt_leaves_exactly_one_row(delta_settings, uid):
    from orchestrator.adapters.persistence_delta import DeltaPersistence

    p = DeltaPersistence(delta_settings)
    run_id = f"RUN-DUPMERGE-{uid}"
    p.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))

    p_a = DeltaPersistence(delta_settings)
    p_b = DeltaPersistence(delta_settings)
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def do_begin(pinst, key):
        barrier.wait()
        try:
            a = pinst.begin_node_attempt(
                run_id=run_id, phase="plan", node_index=0, node_name="discover",
                state_version_before=1, now=utc_now(),
            )
            outcomes[key] = ("ok", a["execution_key"])
        except Exception as exc:  # noqa: BLE001 - the assertion below is what matters
            outcomes[key] = ("error", repr(exc))

    t1 = threading.Thread(target=do_begin, args=(p_a, "a"))
    t2 = threading.Thread(target=do_begin, args=(p_b, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    # Whether or not the losing MERGE raised, the table must never end up with two
    # rows for the same execution_key -- that would be silent duplicate execution
    # tracking, exactly the defect CLAUDE.md §9C/P1A DoD calls out.
    rows = [a for a in p.list_node_attempts(run_id) if a["node_name"] == "discover"]
    assert len(rows) == 1, outcomes
    ok_outcomes = [o for o in outcomes.values() if o[0] == "ok"]
    assert len(ok_outcomes) >= 1, outcomes


def test_concurrent_duplicate_trace_event_leaves_exactly_one_row(delta_settings, uid):
    from orchestrator.adapters.persistence_delta import DeltaPersistence

    p = DeltaPersistence(delta_settings)
    run_id = f"RUN-DUPEVENT-{uid}"
    p.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))

    p_a = DeltaPersistence(delta_settings)
    p_b = DeltaPersistence(delta_settings)
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}
    event_id = f"EVT-DUP-{uid}"

    def do_event(pinst, key):
        barrier.wait()
        try:
            pinst.append_trace_event(
                {
                    "event_id": event_id, "run_id": run_id, "event_type": "x",
                    "event_time": utc_now(), "stage": "s", "status": "ok",
                    "message": f"from {key}", "actor": "a",
                }
            )
            outcomes[key] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcomes[key] = f"error: {exc!r}"

    t1 = threading.Thread(target=do_event, args=(p_a, "a"))
    t2 = threading.Thread(target=do_event, args=(p_b, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    rows = [e for e in p.list_trace_events(run_id) if e["event_id"] == event_id]
    assert len(rows) == 1, outcomes
    assert "ok" in outcomes.values(), outcomes


def test_append_only_enforced_on_delta(delta_settings, uid):
    from orchestrator.adapters.persistence_delta import DeltaPersistence

    p = DeltaPersistence(delta_settings)
    run_id = f"RUN-AO-{uid}"
    p.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))
    event_id = f"EVT-AO-{uid}"
    p.append_trace_event(
        {
            "event_id": event_id, "run_id": run_id, "event_type": "x",
            "event_time": utc_now(), "stage": "s", "status": "ok", "message": "m", "actor": "a",
        }
    )

    table = p._table("trace_events")
    with p._cursor_ctx() as conn:
        with pytest.raises(Exception, match="APPEND_ONLY"):
            p._execute(conn, f"UPDATE {table} SET message = 'changed' WHERE event_id = :id", {"id": event_id})
        with pytest.raises(Exception, match="APPEND_ONLY"):
            p._execute(conn, f"DELETE FROM {table} WHERE event_id = :id", {"id": event_id})

    fp_table = p._table("run_fingerprints")
    with p._cursor_ctx() as conn:
        with pytest.raises(Exception, match="APPEND_ONLY"):
            p._execute(conn, f"UPDATE {fp_table} SET code_revision = 'x' WHERE fingerprint_id = :id", {"id": f"FP-{run_id}"})


def _child_resume_delta(schema, run_id, fp_dict, barrier_file, result_file):
    import os
    import time as _time

    os.environ["DBX_SCHEMA"] = schema
    from orchestrator import runs as runs_mod
    from orchestrator.adapters.persistence_delta import DeltaPersistence
    from orchestrator.config import load_settings as _load_settings
    from orchestrator.errors import InvalidTransition as IT
    from orchestrator.errors import StaleStateError as SSE
    from tests.conftest import canonical_ts as _canonical_ts

    p = DeltaPersistence(_load_settings())

    with open(barrier_file, "a") as f:
        f.write("ready\n")
    while True:
        with open(barrier_file) as f:
            if len(f.readlines()) >= 2:
                break
        _time.sleep(0.05)

    try:
        r = runs_mod.resume(p, run_id, actor="proc", now=_canonical_ts(os.getpid() % 3600), current_fingerprint=fp_dict)
        outcome = f"ok:{r.status}"
    except (SSE, IT):
        outcome = "lost"
    except Exception as exc:  # pragma: no cover - diagnostic aid only
        outcome = f"error:{exc!r}"

    with open(result_file, "a") as f:
        f.write(outcome + "\n")


def test_concurrent_resume_only_one_succeeds_cross_process_delta(delta_settings, uid, tmp_path):
    from orchestrator.adapters.persistence_delta import DeltaPersistence
    from orchestrator.status import transition

    p = DeltaPersistence(delta_settings)
    run_id = f"RUN-RACE-DELTA-{uid}"
    fp = _fingerprint(f"FP-{run_id}")
    created = p.create_run(_state(run_id), fp)
    running = transition(created, "running", now=canonical_ts(1))
    p.save_state(running)
    interrupted = transition(p.load_state(run_id), "interrupted", now=canonical_ts(2))
    p.save_state(interrupted)

    barrier_file = str(tmp_path / "barrier.txt")
    result_file = str(tmp_path / "results.txt")
    open(barrier_file, "w").close()
    open(result_file, "w").close()

    schema = delta_settings.schema
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_child_resume_delta, args=(schema, run_id, fp, barrier_file, result_file))
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)

    with open(result_file) as f:
        outcomes = [line.strip() for line in f if line.strip()]

    assert len(outcomes) == 2, outcomes
    ok_outcomes = [o for o in outcomes if o.startswith("ok:")]
    lost_outcomes = [o for o in outcomes if o == "lost"]
    assert len(ok_outcomes) == 1, outcomes
    assert len(lost_outcomes) == 1, outcomes

    final = p.load_state(run_id)
    assert final.status == "queued"


def test_measured_latencies(delta_settings, uid, capsys):
    # Informational, not a perf gate (CLAUDE.md doesn't set a P1A latency budget) --
    # records median latency of the core write paths against the live warehouse for
    # the report. Runs with -s to see the printed numbers.
    from orchestrator.adapters.persistence_delta import DeltaPersistence

    p = DeltaPersistence(delta_settings)
    n = 5
    timings: dict[str, list[float]] = {"create_run": [], "save_state": [], "begin_node_attempt": [], "append_trace_event": []}

    for i in range(n):
        run_id = f"RUN-LAT-{uid}-{i}"
        t0 = time.perf_counter()
        state = p.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))
        timings["create_run"].append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        state = p.save_state(dataclasses.replace(state, status="running"))
        timings["save_state"].append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        attempt = p.begin_node_attempt(
            run_id=run_id, phase="plan", node_index=0, node_name="discover",
            state_version_before=state.state_version, now=utc_now(),
        )
        timings["begin_node_attempt"].append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        p.append_trace_event(
            {
                "event_id": f"EVT-LAT-{uid}-{i}", "run_id": run_id, "event_type": "x",
                "event_time": utc_now(), "stage": "s", "status": "ok", "message": "m", "actor": "a",
            }
        )
        timings["append_trace_event"].append(time.perf_counter() - t0)

    def median(values):
        s = sorted(values)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    print("\nDelta live latency (median of n=%d, seconds):" % n)
    for op, values in timings.items():
        print(f"  {op}: {median(values):.3f}s  (min={min(values):.3f}s max={max(values):.3f}s)")

    for op, values in timings.items():
        assert median(values) < 30, f"{op} median latency {median(values):.1f}s looks hung"
