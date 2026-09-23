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


def _fingerprint(fp_id="FP-1", **overrides):
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


def _state(run_id="RUN-1", **overrides):
    base = dict(
        run_id=run_id,
        run_kind="fieldwork",
        mode="playbook",
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective="test",
        run_owner="alice",
        fingerprint_id="FP-1",
        created_at=canonical_ts(0),
        last_state_change_at=canonical_ts(0),
        status="queued",
        engagement_id="ENG-DEFAULT",
    )
    base.update(overrides)
    return RunState(**base)


def test_create_and_load_round_trip(persistence):
    created = persistence.create_run(_state(), _fingerprint())
    assert created.state_version == 1
    loaded = persistence.load_state(created.run_id)
    assert loaded == created


def test_create_run_already_exists(persistence):
    persistence.create_run(_state(), _fingerprint())
    with pytest.raises(RunAlreadyExists):
        persistence.create_run(_state(), _fingerprint())


def test_load_state_not_found(persistence):
    with pytest.raises(RunNotFound):
        persistence.load_state("RUN-DOES-NOT-EXIST")


def test_save_increments_version_in_state_and_projection(persistence):
    created = persistence.create_run(_state(), _fingerprint())
    updated = dataclasses.replace(created, status="running")
    saved = persistence.save_state(updated)
    assert saved.state_version == 2

    reloaded = persistence.load_state(created.run_id)
    assert reloaded.state_version == 2
    assert reloaded.status == "running"

    projection = [r for r in persistence.list_runs() if r["run_id"] == created.run_id][0]
    assert projection["state_version"] == 2
    assert projection["status"] == "running"


def test_stale_save_raises_with_expected_and_actual(persistence):
    created = persistence.create_run(_state(run_id="RUN-STALE"), _fingerprint(fp_id="FP-STALE"))
    fresh = dataclasses.replace(created, status="running")
    persistence.save_state(fresh)  # advances to version 2

    stale = dataclasses.replace(created, status="failed")  # still carries version 1
    with pytest.raises(StaleStateError) as exc:
        persistence.save_state(stale)
    assert exc.value.expected_version == 1
    assert exc.value.actual_version == 2


def test_fingerprint_dedupe(persistence):
    fp = _fingerprint(fp_id="FP-DEDUPE")
    persistence.create_run(_state(run_id="RUN-A"), fp)
    persistence.create_run(_state(run_id="RUN-B"), fp)
    fetched = persistence.get_fingerprint("FP-DEDUPE")
    assert fetched["fingerprint_id"] == "FP-DEDUPE"


def test_fingerprint_conflict(persistence):
    fp = _fingerprint(fp_id="FP-CONFLICT")
    persistence.create_run(_state(run_id="RUN-C"), fp)
    conflicting = _fingerprint(fp_id="FP-CONFLICT", code_revision="different-rev")
    with pytest.raises(FingerprintConflict):
        persistence.create_run(_state(run_id="RUN-D"), conflicting)


def test_begin_node_attempt_idempotent(persistence):
    persistence.create_run(_state(run_id="RUN-NODE"), _fingerprint(fp_id="FP-NODE"))
    a1 = persistence.begin_node_attempt(
        run_id="RUN-NODE", phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now="t1",
    )
    a2 = persistence.begin_node_attempt(
        run_id="RUN-NODE", phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now="t2",
    )
    assert a1["attempt_id"] == a2["attempt_id"]
    assert a1["execution_key"] == a2["execution_key"]
    attempts = [a for a in persistence.list_node_attempts("RUN-NODE") if a["node_name"] == "discover"]
    assert len(attempts) == 1


def test_attempt_numbering_after_failed_attempt(persistence):
    persistence.create_run(_state(run_id="RUN-RETRY"), _fingerprint(fp_id="FP-RETRY"))
    a1 = persistence.begin_node_attempt(
        run_id="RUN-RETRY", phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now="t1",
    )
    assert a1["attempt_number"] == 1
    persistence.complete_node_attempt(a1["execution_key"], outcome="failed", now="t2", error_detail="boom")

    a2 = persistence.begin_node_attempt(
        run_id="RUN-RETRY", phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now="t3",
    )
    assert a2["attempt_number"] == 2
    assert a2["execution_key"] != a1["execution_key"]


def test_complete_node_attempt_never_overwrites_succeeded(persistence):
    persistence.create_run(_state(run_id="RUN-OVERWRITE"), _fingerprint(fp_id="FP-OVERWRITE"))
    a1 = persistence.begin_node_attempt(
        run_id="RUN-OVERWRITE", phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now="t1",
    )
    persistence.complete_node_attempt(
        a1["execution_key"], outcome="succeeded", now="t2",
        state_version_after=2, result_state_json="{\"marker\": 1}",
    )
    # closing again with a DIFFERENT outcome is a genuine contract violation, not a
    # silent no-op (CLAUDE.md §9C non-blocking item).
    with pytest.raises(AttemptAlreadyClosed):
        persistence.complete_node_attempt(
            a1["execution_key"], outcome="failed", now="t3", error_detail="should not apply"
        )
    rows = [a for a in persistence.list_node_attempts("RUN-OVERWRITE") if a["node_name"] == "discover"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "succeeded"
    assert rows[0]["result_state_json"] == "{\"marker\": 1}"


def test_complete_node_attempt_same_outcome_is_idempotent_noop(persistence):
    persistence.create_run(_state(run_id="RUN-IDEMPOTENT"), _fingerprint(fp_id="FP-IDEMPOTENT"))
    a1 = persistence.begin_node_attempt(
        run_id="RUN-IDEMPOTENT", phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now="t1",
    )
    persistence.complete_node_attempt(
        a1["execution_key"], outcome="succeeded", now="t2",
        state_version_after=2, result_state_json="{\"marker\": 1}",
    )
    persistence.complete_node_attempt(  # same outcome again -- no-op, no raise
        a1["execution_key"], outcome="succeeded", now="t3",
        state_version_after=2, result_state_json="{\"marker\": 1}",
    )
    rows = [a for a in persistence.list_node_attempts("RUN-IDEMPOTENT") if a["node_name"] == "discover"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "succeeded"


def test_complete_node_attempt_missing_raises_attempt_not_found(persistence):
    with pytest.raises(AttemptNotFound):
        persistence.complete_node_attempt("RUN-NOPE:plan:1:discover:1", outcome="failed", now="t1")


def test_trace_events_idempotent_and_ui_shape(persistence):
    persistence.create_run(_state(run_id="RUN-TRACE"), _fingerprint(fp_id="FP-TRACE"))
    event = {
        "event_id": "EVT-FIXED-1",
        "run_id": "RUN-TRACE",
        "event_type": "node_started",
        "event_time": "2026-01-01T00:00:05Z",
        "stage": "discover",
        "status": "running",
        "message": "discover started",
        "duration_s": None,
        "actor": "pipeline",
    }
    persistence.append_trace_event(event)
    persistence.append_trace_event(dict(event, message="ignored, duplicate event_id"))

    events = persistence.list_trace_events("RUN-TRACE")
    assert len(events) == 1
    e = events[0]
    for key in ("event_id", "run_id", "timestamp", "stage", "status", "message", "duration_s"):
        assert key in e
    assert e["message"] == "discover started"
    assert e["timestamp"] == "2026-01-01T00:00:05Z"


def test_list_runs_and_find_runs(persistence):
    persistence.create_run(_state(run_id="RUN-LIST-1", status="queued"), _fingerprint(fp_id="FP-LIST-1"))
    persistence.create_run(_state(run_id="RUN-LIST-2", status="queued"), _fingerprint(fp_id="FP-LIST-2"))
    running = dataclasses.replace(persistence.load_state("RUN-LIST-2"), status="running")
    persistence.save_state(running)

    all_runs = {r["run_id"] for r in persistence.list_runs()}
    assert {"RUN-LIST-1", "RUN-LIST-2"} <= all_runs

    queued_only = {r["run_id"] for r in persistence.list_runs({"status": "queued"})}
    assert "RUN-LIST-1" in queued_only
    assert "RUN-LIST-2" not in queued_only

    running_ids = persistence.find_runs(["running"])
    assert "RUN-LIST-2" in running_ids
    assert "RUN-LIST-1" not in running_ids


def test_close_open_attempts(persistence):
    persistence.create_run(_state(run_id="RUN-CLOSE"), _fingerprint(fp_id="FP-CLOSE"))
    a1 = persistence.begin_node_attempt(
        run_id="RUN-CLOSE", phase="plan", node_index=0, node_name="discover",
        state_version_before=1, now="t1",
    )
    persistence.close_open_attempts("RUN-CLOSE", outcome="interrupted", now="t2", error_detail="orphaned")
    rows = [a for a in persistence.list_node_attempts("RUN-CLOSE") if a["node_name"] == "discover"]
    assert rows[0]["outcome"] == "interrupted"


def test_migrate_is_idempotent(persistence):
    # persistence fixture already ran migrate() once during setup; calling again must be a no-op.
    result = persistence.migrate()
    assert result == []


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
    created = p.create_run(_state(run_id="RUN-APPEND-ONLY"), _fingerprint(fp_id="FP-APPEND-ONLY"))

    conn = p._connect()
    with pytest.raises(Exception):
        conn.execute("DELETE FROM runs WHERE run_id = ?", (created.run_id,))

    p.append_trace_event(
        {
            "event_id": "EVT-AO-1", "run_id": created.run_id, "event_type": "x",
            "event_time": "t1", "stage": "s", "status": "ok", "message": "m", "actor": "a",
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
