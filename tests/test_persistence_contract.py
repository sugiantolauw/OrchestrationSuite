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


# ── batch read methods (independent review 2026-09-24 item 6) ──────────────


def test_batch_read_methods_match_the_per_id_methods(persistence, uid):
    """list_findings_for_runs / get_run_metrics_for_runs /
    list_management_actions_for_runs / get_fingerprints -- one query for
    many ids instead of one per id (orchestrator.service.list_runs' own
    N+1 query cost). Each batch result must equal calling the single-id
    method once per id, on every backend, including a run/id with nothing
    recorded (present with an empty result, never omitted)."""
    from tests.test_persistence_p2 import _finding

    run_a = f"RUN-BATCH-A-{uid}"
    run_b = f"RUN-BATCH-B-{uid}"
    run_empty = f"RUN-BATCH-EMPTY-{uid}"
    fp_a = f"FP-BATCH-A-{uid}"
    fp_b = f"FP-BATCH-B-{uid}"

    persistence.create_run(_state(run_a, fingerprint_id=fp_a), _fingerprint(fp_a))
    persistence.create_run(_state(run_b, fingerprint_id=fp_b), _fingerprint(fp_b))
    persistence.create_run(_state(run_empty, fingerprint_id=f"FP-BATCH-EMPTY-{uid}"), _fingerprint(f"FP-BATCH-EMPTY-{uid}"))

    persistence.write_findings(
        run_a, [_finding(f"F-BATCH-A1-{uid}", rule_id="R1"), _finding(f"F-BATCH-A2-{uid}", rule_id="R2")],
        engagement_id="ENG-DEFAULT", skill_id="SKILL-001", skill_version="v1", now=canonical_ts(1),
    )
    persistence.write_findings(
        run_b, [_finding(f"F-BATCH-B1-{uid}", rule_id="R1")],
        engagement_id="ENG-DEFAULT", skill_id="SKILL-001", skill_version="v1", now=canonical_ts(1),
    )

    persistence.write_run_metrics(run_a, [
        {"metric_name": "m1", "value": 1.0, "unit": "AUD", "source_ref": {}, "test_id": "T1"},
    ])
    persistence.write_run_metrics(run_b, [
        {"metric_name": "m2", "value": 2.0, "unit": "AUD", "source_ref": {}, "test_id": "T1"},
    ])

    persistence.write_management_actions(run_a, [
        {"action_id": f"MA-BATCH-A1-{uid}", "finding_id": f"F-BATCH-A1-{uid}", "engagement_id": "ENG-DEFAULT",
         "skill_id": "SKILL-001", "title": "Fix it", "status": "draft"},
    ], now=canonical_ts(1))

    run_ids = [run_a, run_b, run_empty]
    findings_batch = persistence.list_findings_for_runs(run_ids)
    metrics_batch = persistence.get_run_metrics_for_runs(run_ids)
    actions_batch = persistence.list_management_actions_for_runs(run_ids)
    fp_batch = persistence.get_fingerprints([fp_a, fp_b])

    for rid in run_ids:
        assert rid in findings_batch
        assert rid in metrics_batch
        assert rid in actions_batch
        expected_findings = {f["finding_id"] for f in persistence.list_findings(rid)}
        assert {f["finding_id"] for f in findings_batch[rid]} == expected_findings
        assert metrics_batch[rid] == persistence.get_run_metrics(rid)
        expected_actions = {a["action_id"] for a in persistence.list_management_actions(filters={"run_id": rid})}
        assert {a["action_id"] for a in actions_batch[rid]} == expected_actions

    assert findings_batch[run_empty] == []
    assert metrics_batch[run_empty] == {}
    assert actions_batch[run_empty] == []

    assert fp_batch[fp_a]["code_revision"] == persistence.get_fingerprint(fp_a)["code_revision"]
    assert fp_batch[fp_b]["code_revision"] == persistence.get_fingerprint(fp_b)["code_revision"]
    assert "FP-DOES-NOT-EXIST" not in persistence.get_fingerprints(["FP-DOES-NOT-EXIST"])


def test_batch_read_methods_with_no_ids_return_empty(persistence):
    assert persistence.list_findings_for_runs([]) == {}
    assert persistence.get_run_metrics_for_runs([]) == {}
    assert persistence.list_management_actions_for_runs([]) == {}
    assert persistence.get_fingerprints([]) == {}


# ── LLM call ledger (independent review 2026-09-24 item 3) ─────────────────


def _llm_call_row(call_id, *, run_id, endpoint="ep1", served_model_version="v1", source="live",
                   outcome="succeeded", cache_hit=False, cache_key=None):
    return {
        "call_id": call_id, "run_id": run_id, "engagement_id": "ENG-DEFAULT", "node_name": "classify",
        "execution_key": f"EK-{call_id}", "task": "classify", "seq": 1, "transport_attempt": 1,
        "endpoint_role": "model_gpt_oss", "endpoint": endpoint, "served_model_version": served_model_version,
        "source": source, "cache_hit": cache_hit, "cache_key": cache_key, "cached_from_call_id": None,
        "version_changed": False, "prompt_template_id": "t1", "prompt_template_version": "tv1",
        "prompt_sha256": "ph1", "messages_json": "[]", "params_sent_json": "{}", "params_withheld_json": "{}",
        "response_text": "hi", "reasoning_parts_stripped": 0, "finish_reason": "stop", "prompt_tokens": 1,
        "completion_tokens": 1, "total_tokens": 2, "latency_ms": 10, "request_id": "req1", "outcome": outcome,
        "error_type": None, "error_status_code": None, "error_message": None,
        "pii_columns_masked_json": "[]", "pii_whitelist_json": "[]", "actor": "alice",
        "created_at": canonical_ts(0),
    }


def test_record_llm_call_and_list_llm_calls(persistence, uid):
    run_id = f"RUN-LLM-{uid}"
    persistence.record_llm_call(_llm_call_row(f"CALL-A-{uid}", run_id=run_id))
    persistence.record_llm_call(_llm_call_row(f"CALL-B-{uid}", run_id=run_id))
    rows = persistence.list_llm_calls(run_id)
    assert {r["call_id"] for r in rows} == {f"CALL-A-{uid}", f"CALL-B-{uid}"}
    assert rows[0]["run_id"] == run_id


def test_record_llm_call_is_idempotent_by_call_id(persistence, uid):
    run_id = f"RUN-LLM-IDEMPOTENT-{uid}"
    call_id = f"CALL-IDEM-{uid}"
    persistence.record_llm_call(_llm_call_row(call_id, run_id=run_id, outcome="failed_transport"))
    persistence.record_llm_call(_llm_call_row(call_id, run_id=run_id, outcome="succeeded"))
    rows = persistence.list_llm_calls(run_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "succeeded"


def test_last_live_version_only_considers_live_succeeded_rows(persistence, uid):
    endpoint = f"ep-{uid}"
    run_id = f"RUN-LLM-VER-{uid}"
    assert persistence.last_live_version(endpoint) is None
    persistence.record_llm_call(_llm_call_row(
        f"CALL-FAIL-{uid}", run_id=run_id, endpoint=endpoint, served_model_version="v1",
        source="live", outcome="failed_transport",
    ))
    assert persistence.last_live_version(endpoint) is None  # not succeeded -- doesn't count
    persistence.record_llm_call(_llm_call_row(
        f"CALL-OK-{uid}", run_id=run_id, endpoint=endpoint, served_model_version="v2",
        source="live", outcome="succeeded",
    ))
    assert persistence.last_live_version(endpoint) == "v2"


def test_llm_cache_put_if_absent_and_lookup(persistence, uid):
    cache_key = f"CACHE-{uid}"
    row = {
        "cache_key": cache_key, "prompt_sha256": f"ph-{uid}", "endpoint": f"ep-{uid}",
        "served_model_version": "v1", "params_json": "{}", "response_text": "hi",
        "finish_reason": "stop", "usage_json": "{}", "source_call_id": f"CALL-{uid}",
        "created_at": canonical_ts(0),
    }
    assert persistence.get_llm_cache(cache_key) is None
    assert persistence.put_llm_cache_if_absent(row) is True
    assert persistence.put_llm_cache_if_absent(row) is False  # never updates -- already present

    fetched = persistence.get_llm_cache(cache_key)
    assert fetched["response_text"] == "hi"

    matches = persistence.find_llm_cache(row["prompt_sha256"], row["endpoint"], row["params_json"])
    assert [m["cache_key"] for m in matches] == [cache_key]


def test_find_llm_cache_scoped_to_exact_prompt_endpoint_and_params(persistence, uid):
    prompt_sha = f"ph-scope-{uid}"
    endpoint = f"ep-scope-{uid}"
    row = {
        "cache_key": f"CACHE-SCOPE-{uid}", "prompt_sha256": prompt_sha, "endpoint": endpoint,
        "served_model_version": "v1", "params_json": '{"max_tokens": 10}', "response_text": "hi",
        "finish_reason": "stop", "usage_json": "{}", "source_call_id": f"CALL-SCOPE-{uid}",
        "created_at": canonical_ts(0),
    }
    persistence.put_llm_cache_if_absent(row)
    assert persistence.find_llm_cache(prompt_sha, endpoint, '{"max_tokens": 20}') == []
    assert persistence.find_llm_cache(prompt_sha, "a-different-endpoint", row["params_json"]) == []
    assert len(persistence.find_llm_cache(prompt_sha, endpoint, row["params_json"])) == 1
