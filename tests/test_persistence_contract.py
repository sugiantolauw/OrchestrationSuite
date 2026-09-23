from __future__ import annotations

import dataclasses

import pytest

from orchestrator.errors import (
    AttemptAlreadyClosed,
    AttemptNotFound,
    FingerprintConflict,
    RunAlreadyExists,
    RunNotFound,
    StaleStateError,
)
from orchestrator.state import RunState
from tests.conftest import canonical_ts

# Runs against every persistence backend (local_memory, local_file, delta) via the
# `persistence` fixture in conftest.py. This is the shared contract test suite referenced
# by CLAUDE.md §4.1 non-negotiable 6 and the P1A DoD ("LocalPersistence + DeltaPersistence
# both pass the same contract test").
#
# The `delta` backend shares ONE throwaway schema for the whole pytest session
# (conftest.py's `delta_schema` fixture) rather than getting a fresh one per test, so
# every id a test writes must be unique across the whole session -- the `uid` fixture
# supplies that. local_memory/local_file still get a fresh database per test and would
# never have collided on a hardcoded id, but the ids are unique everywhere now so the
# same test body genuinely exercises the same scenario on every backend.


def _fingerprint(fp_id, **overrides):
    base = dict(
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
    base.update(overrides)
    return base


def _state(run_id, *, fingerprint_id=None, **overrides):
    base = dict(
        run_id=run_id,
        run_kind="fieldwork",
        mode="playbook",
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective="test",
        run_owner="alice",
        fingerprint_id=fingerprint_id or f"FP-{run_id}",
        created_at=canonical_ts(0),
        last_state_change_at=canonical_ts(0),
        status="queued",
        engagement_id="ENG-DEFAULT",
    )
    base.update(overrides)
    return RunState(**base)


def test_create_and_load_round_trip(persistence, uid):
    run_id = f"RUN-{uid}"
    created = persistence.create_run(_state(run_id), _fingerprint(f"FP-{uid}"))
    assert created.state_version == 1
    loaded = persistence.load_state(created.run_id)
    assert loaded == created


def test_create_run_already_exists(persistence, uid):
    run_id = f"RUN-{uid}"
    persistence.create_run(_state(run_id), _fingerprint(f"FP-{uid}"))
    with pytest.raises(RunAlreadyExists):
        persistence.create_run(_state(run_id), _fingerprint(f"FP-{uid}-2"))


def test_load_state_not_found(persistence, uid):
    with pytest.raises(RunNotFound):
        persistence.load_state(f"RUN-DOES-NOT-EXIST-{uid}")


def test_save_increments_version_in_state_and_projection(persistence, uid):
    run_id = f"RUN-{uid}"
    created = persistence.create_run(_state(run_id), _fingerprint(f"FP-{uid}"))
    updated = dataclasses.replace(created, status="running")
    saved = persistence.save_state(updated)
    assert saved.state_version == 2

    reloaded = persistence.load_state(created.run_id)
    assert reloaded.state_version == 2
    assert reloaded.status == "running"

    projection = [r for r in persistence.list_runs() if r["run_id"] == created.run_id][0]
    assert projection["state_version"] == 2
    assert projection["status"] == "running"


def test_stale_save_raises_with_expected_and_actual(persistence, uid):
    run_id = f"RUN-STALE-{uid}"
    created = persistence.create_run(_state(run_id), _fingerprint(f"FP-STALE-{uid}"))
    fresh = dataclasses.replace(created, status="running")
    persistence.save_state(fresh)  # advances to version 2

    stale = dataclasses.replace(created, status="failed")  # still carries version 1
    with pytest.raises(StaleStateError) as exc:
        persistence.save_state(stale)
    assert exc.value.expected_version == 1
    assert exc.value.actual_version == 2


def test_fingerprint_dedupe(persistence, uid):
    fp_id = f"FP-DEDUPE-{uid}"
    fp = _fingerprint(fp_id)
    persistence.create_run(_state(f"RUN-A-{uid}", fingerprint_id=fp_id), fp)
    persistence.create_run(_state(f"RUN-B-{uid}", fingerprint_id=fp_id), fp)
    fetched = persistence.get_fingerprint(fp_id)
    assert fetched["fingerprint_id"] == fp_id


def test_fingerprint_conflict(persistence, uid):
    fp_id = f"FP-CONFLICT-{uid}"
    fp = _fingerprint(fp_id)
    persistence.create_run(_state(f"RUN-C-{uid}", fingerprint_id=fp_id), fp)
    conflicting = _fingerprint(fp_id, code_revision="different-rev")
    with pytest.raises(FingerprintConflict):
        persistence.create_run(_state(f"RUN-D-{uid}", fingerprint_id=fp_id), conflicting)


def test_begin_node_attempt_idempotent(persistence, uid):
    run_id = f"RUN-NODE-{uid}"
    persistence.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))
    a1 = persistence.begin_node_attempt(
        run_id=run_id, phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now=canonical_ts(1),
    )
    a2 = persistence.begin_node_attempt(
        run_id=run_id, phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now=canonical_ts(2),
    )
    assert a1["attempt_id"] == a2["attempt_id"]
    assert a1["execution_key"] == a2["execution_key"]
    attempts = [a for a in persistence.list_node_attempts(run_id) if a["node_name"] == "discover"]
    assert len(attempts) == 1


def test_attempt_numbering_after_failed_attempt(persistence, uid):
    run_id = f"RUN-RETRY-{uid}"
    persistence.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))
    a1 = persistence.begin_node_attempt(
        run_id=run_id, phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now=canonical_ts(1),
    )
    assert a1["attempt_number"] == 1
    persistence.complete_node_attempt(a1["execution_key"], outcome="failed", now=canonical_ts(2), error_detail="boom")

    a2 = persistence.begin_node_attempt(
        run_id=run_id, phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now=canonical_ts(3),
    )
    assert a2["attempt_number"] == 2
    assert a2["execution_key"] != a1["execution_key"]


def test_complete_node_attempt_never_overwrites_succeeded(persistence, uid):
    run_id = f"RUN-OVERWRITE-{uid}"
    persistence.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))
    a1 = persistence.begin_node_attempt(
        run_id=run_id, phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now=canonical_ts(1),
    )
    persistence.complete_node_attempt(
        a1["execution_key"], outcome="succeeded", now=canonical_ts(2),
        state_version_after=2, result_state_json="{\"marker\": 1}",
    )
    # closing again with a DIFFERENT outcome is a genuine contract violation, not a
    # silent no-op (CLAUDE.md §9C non-blocking item).
    with pytest.raises(AttemptAlreadyClosed):
        persistence.complete_node_attempt(
            a1["execution_key"], outcome="failed", now=canonical_ts(3), error_detail="should not apply"
        )
    rows = [a for a in persistence.list_node_attempts(run_id) if a["node_name"] == "discover"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "succeeded"
    assert rows[0]["result_state_json"] == "{\"marker\": 1}"


def test_complete_node_attempt_same_outcome_is_idempotent_noop(persistence, uid):
    run_id = f"RUN-IDEMPOTENT-{uid}"
    persistence.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))
    a1 = persistence.begin_node_attempt(
        run_id=run_id, phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now=canonical_ts(1),
    )
    persistence.complete_node_attempt(
        a1["execution_key"], outcome="succeeded", now=canonical_ts(2),
        state_version_after=2, result_state_json="{\"marker\": 1}",
    )
    persistence.complete_node_attempt(  # same outcome again -- no-op, no raise
        a1["execution_key"], outcome="succeeded", now=canonical_ts(3),
        state_version_after=2, result_state_json="{\"marker\": 1}",
    )
    rows = [a for a in persistence.list_node_attempts(run_id) if a["node_name"] == "discover"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "succeeded"


def test_complete_node_attempt_missing_raises_attempt_not_found(persistence, uid):
    with pytest.raises(AttemptNotFound):
        persistence.complete_node_attempt(f"RUN-NOPE-{uid}:plan:1:discover:1", outcome="failed", now=canonical_ts(1))


def test_trace_events_idempotent_and_ui_shape(persistence, uid):
    run_id = f"RUN-TRACE-{uid}"
    persistence.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))
    # event_time must be the canonical timestamp format (CLAUDE.md §9C timeutil.py):
    # Delta stores it as a real TIMESTAMP and hands back a datetime on read, which
    # DeltaPersistence normalises back to this exact canonical string -- an
    # arbitrary-precision literal would round-trip on sqlite (plain TEXT storage) but
    # not through a real TIMESTAMP column, which only round-trips what it can parse.
    event_time = canonical_ts(5)
    event = {
        "event_id": f"EVT-FIXED-{uid}",
        "run_id": run_id,
        "event_type": "node_started",
        "event_time": event_time,
        "stage": "discover",
        "status": "running",
        "message": "discover started",
        "duration_s": None,
        "actor": "pipeline",
    }
    persistence.append_trace_event(event)
    persistence.append_trace_event(dict(event, message="ignored, duplicate event_id"))

    events = persistence.list_trace_events(run_id)
    assert len(events) == 1
    e = events[0]
    for key in ("event_id", "run_id", "timestamp", "stage", "status", "message", "duration_s"):
        assert key in e
    assert e["message"] == "discover started"
    assert e["timestamp"] == event_time


def test_list_runs_and_find_runs(persistence, uid):
    run_1, run_2 = f"RUN-LIST-1-{uid}", f"RUN-LIST-2-{uid}"
    persistence.create_run(_state(run_1, status="queued"), _fingerprint(f"FP-{run_1}"))
    persistence.create_run(_state(run_2, status="queued"), _fingerprint(f"FP-{run_2}"))
    running = dataclasses.replace(persistence.load_state(run_2), status="running")
    persistence.save_state(running)

    all_runs = {r["run_id"] for r in persistence.list_runs()}
    assert {run_1, run_2} <= all_runs

    queued_only = {r["run_id"] for r in persistence.list_runs({"status": "queued"})}
    assert run_1 in queued_only
    assert run_2 not in queued_only

    running_ids = persistence.find_runs(["running"])
    assert run_2 in running_ids
    assert run_1 not in running_ids


def test_close_open_attempts(persistence, uid):
    run_id = f"RUN-CLOSE-{uid}"
    persistence.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))
    persistence.begin_node_attempt(
        run_id=run_id, phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now=canonical_ts(1),
    )
    persistence.close_open_attempts(run_id, outcome="interrupted", now=canonical_ts(2), error_detail="orphaned")
    rows = [a for a in persistence.list_node_attempts(run_id) if a["node_name"] == "discover"]
    assert rows[0]["outcome"] == "interrupted"


def test_migrate_is_idempotent(persistence):
    # persistence fixture already ran migrate() once during setup; calling again must be a no-op.
    result = persistence.migrate()
    assert result == []


def test_repair_projections_catches_up_after_projection_write_failure(persistence, uid):
    # Simulates the swallowed-projection-write failure CLAUDE.md §9C/B4 describes:
    # run_state (the system of record) commits, but the `runs` projection update never
    # lands. repair_projections() must bring the projection back in line with
    # run_state on every backend, including live Delta.
    run_id = f"RUN-REPAIR-{uid}"
    created = persistence.create_run(_state(run_id), _fingerprint(f"FP-{run_id}"))

    orig_retry = persistence._update_runs_projection_with_retry
    persistence._update_runs_projection_with_retry = lambda state: None
    try:
        updated = dataclasses.replace(created, status="running")
        saved = persistence.save_state(updated)
    finally:
        persistence._update_runs_projection_with_retry = orig_retry
    assert saved.state_version == 2

    # list_runs() always reports `status` live from run_state (the system of record,
    # CLAUDE.md §9C/B4), so the lag this simulates is only visible in the `runs`
    # projection's own state_version column, not in status.
    lagging = [r for r in persistence.list_runs() if r["run_id"] == run_id][0]
    assert lagging["state_version"] == 1  # projection never got the update
    assert lagging["status"] == "running"  # read live from run_state, unaffected by the lag

    repaired = persistence.repair_projections()
    assert repaired >= 1

    fixed = [r for r in persistence.list_runs() if r["run_id"] == run_id][0]
    assert fixed["state_version"] == 2
    assert fixed["status"] == "running"


def test_migrate_detects_tampered_checksum(tmp_path):
    import shutil

    from orchestrator.adapters.persistence_local import LocalPersistence
    from orchestrator.errors import MigrationError

    db_path = str(tmp_path / "tamper.db")
    real_ddl_dir = LocalPersistence(":memory:").ddl_dir
    p1 = LocalPersistence(db_path, ddl_dir=real_ddl_dir)
    p1.migrate()

    tampered_dir = tmp_path / "tampered_ddl"
    shutil.copytree(real_ddl_dir, tampered_dir)
    migration_file = tampered_dir / "001_p1a_ledger.sql"
    migration_file.write_text(migration_file.read_text() + "\n-- tampered\n")

    p2 = LocalPersistence(db_path, ddl_dir=tampered_dir)
    with pytest.raises(MigrationError):
        p2.migrate()

    # the real ddl directory on disk must be untouched
    assert "tampered" not in (real_ddl_dir / "001_p1a_ledger.sql").read_text()


def test_append_only_and_no_delete_enforced_locally():
    from orchestrator.adapters.persistence_local import LocalPersistence

    p = LocalPersistence(":memory:")
    p.migrate()
    created = p.create_run(_state("RUN-APPEND-ONLY"), _fingerprint("FP-APPEND-ONLY"))

    conn = p._connect()
    with pytest.raises(Exception):
        conn.execute("DELETE FROM runs WHERE run_id = ?", (created.run_id,))

    p.append_trace_event(
        {
            "event_id": "EVT-AO-1", "run_id": created.run_id, "event_type": "x",
            "event_time": canonical_ts(1), "stage": "s", "status": "ok", "message": "m", "actor": "a",
        }
    )
    with pytest.raises(Exception):
        conn.execute("UPDATE trace_events SET message = 'changed' WHERE event_id = 'EVT-AO-1'")
    with pytest.raises(Exception):
        conn.execute("DELETE FROM trace_events WHERE event_id = 'EVT-AO-1'")

    fp_row = p.get_fingerprint("FP-APPEND-ONLY")
    assert fp_row is not None
    with pytest.raises(Exception):
        conn.execute("UPDATE run_fingerprints SET code_revision = 'x' WHERE fingerprint_id = 'FP-APPEND-ONLY'")
    with pytest.raises(Exception):
        conn.execute("DELETE FROM run_fingerprints WHERE fingerprint_id = 'FP-APPEND-ONLY'")
