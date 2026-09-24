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
                   outcome="succeeded", cache_hit=False, cache_key=None, total_tokens=2,
                   created_at=None):
    return {
        "call_id": call_id, "run_id": run_id, "engagement_id": "ENG-DEFAULT", "node_name": "classify",
        "execution_key": f"EK-{call_id}", "task": "classify", "seq": 1, "transport_attempt": 1,
        "endpoint_role": "model_gpt_oss", "endpoint": endpoint, "served_model_version": served_model_version,
        "source": source, "cache_hit": cache_hit, "cache_key": cache_key, "cached_from_call_id": None,
        "version_changed": False, "prompt_template_id": "t1", "prompt_template_version": "tv1",
        "prompt_sha256": "ph1", "messages_json": "[]", "params_sent_json": "{}", "params_withheld_json": "{}",
        "response_text": "hi", "reasoning_parts_stripped": 0, "finish_reason": "stop", "prompt_tokens": 1,
        "completion_tokens": 1, "total_tokens": total_tokens, "latency_ms": 10, "request_id": "req1", "outcome": outcome,
        "error_type": None, "error_status_code": None, "error_message": None,
        "pii_columns_masked_json": "[]", "pii_whitelist_json": "[]", "actor": "alice",
        "created_at": created_at or canonical_ts(0),
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


def test_find_llm_cache_scoped_to_served_model_version(persistence, uid):
    # NN8 / §3.6 steps 5-6 (independent review fix): a cache lookup must be
    # able to scope to the full (prompt_sha256, endpoint, served_model_
    # version, params_json) key, not just the first three -- otherwise a
    # replay after a provider model upgrade can return an older model's
    # response. `served_model_version=None` (the default) keeps returning
    # every cached version, unfiltered, for resolving an ambiguous version.
    prompt_sha = f"ph-ver-{uid}"
    endpoint = f"ep-ver-{uid}"
    params_json = '{"max_tokens": 10}'
    row_v1 = {
        "cache_key": f"CACHE-VER-V1-{uid}", "prompt_sha256": prompt_sha, "endpoint": endpoint,
        "served_model_version": "v1", "params_json": params_json, "response_text": "v1 response",
        "finish_reason": "stop", "usage_json": "{}", "source_call_id": f"CALL-VER-V1-{uid}",
        "created_at": canonical_ts(0),
    }
    row_v2 = {
        "cache_key": f"CACHE-VER-V2-{uid}", "prompt_sha256": prompt_sha, "endpoint": endpoint,
        "served_model_version": "v2", "params_json": params_json, "response_text": "v2 response",
        "finish_reason": "stop", "usage_json": "{}", "source_call_id": f"CALL-VER-V2-{uid}",
        "created_at": canonical_ts(1),
    }
    persistence.put_llm_cache_if_absent(row_v1)
    persistence.put_llm_cache_if_absent(row_v2)

    unfiltered = persistence.find_llm_cache(prompt_sha, endpoint, params_json)
    assert {r["served_model_version"] for r in unfiltered} == {"v1", "v2"}

    scoped_v1 = persistence.find_llm_cache(prompt_sha, endpoint, params_json, served_model_version="v1")
    assert [r["cache_key"] for r in scoped_v1] == [f"CACHE-VER-V1-{uid}"]
    assert scoped_v1[0]["response_text"] == "v1 response"

    scoped_v2 = persistence.find_llm_cache(prompt_sha, endpoint, params_json, served_model_version="v2")
    assert [r["cache_key"] for r in scoped_v2] == [f"CACHE-VER-V2-{uid}"]

    assert persistence.find_llm_cache(prompt_sha, endpoint, params_json, served_model_version="v3") == []


# ── sum_llm_call_tokens_since (L0.3, LIFECYCLE_design.md §2.6 monthly admission) ──


def test_sum_llm_call_tokens_since_sums_matching_rows_only(persistence, uid):
    # Compares two sums (with/without the "before" row) rather than an
    # absolute total, so this holds even against the shared live-delta
    # session schema (tests/conftest.py's `delta_schema`), which other tests
    # may also have written llm_calls rows into.
    run_id = f"RUN-BUDGET-{uid}"
    before_ts = "2026-01-31T23:59:59.999999Z"
    since = "2026-02-01T00:00:00.000000Z"
    persistence.record_llm_call(_llm_call_row(
        f"CALL-BEFORE-{uid}", run_id=run_id, total_tokens=1_000, created_at=before_ts,
    ))
    persistence.record_llm_call(_llm_call_row(
        f"CALL-AT-{uid}", run_id=run_id, total_tokens=100, created_at=since,
    ))
    persistence.record_llm_call(_llm_call_row(
        f"CALL-AFTER-{uid}", run_id=run_id, total_tokens=250, created_at="2026-02-15T00:00:00.000000Z",
    ))

    sum_including_before = persistence.sum_llm_call_tokens_since(before_ts)
    sum_excluding_before = persistence.sum_llm_call_tokens_since(since)
    assert sum_including_before - sum_excluding_before >= 1_000
    assert sum_excluding_before >= 350


def test_sum_llm_call_tokens_since_returns_zero_for_a_future_month(persistence, uid):
    run_id = f"RUN-BUDGET-FUTURE-{uid}"
    persistence.record_llm_call(_llm_call_row(
        f"CALL-{uid}", run_id=run_id, total_tokens=42, created_at=canonical_ts(0),
    ))
    assert persistence.sum_llm_call_tokens_since("2099-01-01T00:00:00.000000Z") == 0


# ── P6 narration (docs/specs/P6_narration_design.md §6.1 / WP N3b, migration 011) ──
#
# narratives, narrative_edits, finding_candidates, finding_themes and
# test_line_values, plus the new findings/management_actions columns. Same
# contract shape as the sections above: run against every backend via the
# `persistence` fixture, ids made unique via `uid` for the shared live-delta
# session schema.


def _narrative_row(narrative_id, *, run_id, target_id="F1", version=1, generation=0, origin="model", **overrides):
    base = dict(
        narrative_id=narrative_id, run_id=run_id, engagement_id=None, target_kind="finding",
        target_id=target_id, field="observation", version=version, generation=generation, origin=origin,
        template_text="{metric:hv_count} claims.", sources=[{"placeholder": "hv_count", "source_field": "hv_count", "unit": "count"}],
        call_ids=["CALL-1"], served_model_version="v1", violations=None,
        updated_by="narrate", updated_at=canonical_ts(0),
    )
    base.update(overrides)
    return base


def test_upsert_narrative_is_idempotent_by_narrative_id(persistence, uid):
    run_id = f"RUN-NARR-{uid}"
    narrative_id = f"NARR-{uid}"
    persistence.upsert_narrative(_narrative_row(narrative_id, run_id=run_id, template_text="first draft"))
    persistence.upsert_narrative(_narrative_row(narrative_id, run_id=run_id, template_text="revised draft", version=2))

    rows = persistence.get_narratives(run_id)
    assert len(rows) == 1  # overwritten in place, never appended
    assert rows[0]["template_text"] == "revised draft"
    assert rows[0]["version"] == 2
    assert rows[0]["sources"] == [{"placeholder": "hv_count", "source_field": "hv_count", "unit": "count"}]
    assert rows[0]["call_ids"] == ["CALL-1"]


def test_upsert_narrative_cas_rejects_a_stale_expected_version(persistence, uid):
    # P6 WP N10 (§6.4, CLAUDE.md NN14): `expected_version` turns
    # `upsert_narrative` from an unconditional overwrite into a real
    # compare-and-swap -- the same "zero rows affected = rejected" contract
    # `save_state`/`decide_candidate_cas` already enforce, now for
    # narratives.version. Runs on both backends via the `persistence`
    # fixture, per CLAUDE.md §4.1 non-negotiable 6.
    run_id = f"RUN-NARR-CAS-{uid}"
    narrative_id = f"NARR-CAS-{uid}"
    persistence.upsert_narrative(_narrative_row(narrative_id, run_id=run_id, template_text="v1", version=1))

    # A correctly-guarded write (expected_version matches the stored row)
    # applies and returns True.
    applied = persistence.upsert_narrative(
        _narrative_row(narrative_id, run_id=run_id, template_text="v2", version=2, origin="human_edit"),
        expected_version=1,
    )
    assert applied is True
    row = next(r for r in persistence.get_narratives(run_id) if r["narrative_id"] == narrative_id)
    assert row["template_text"] == "v2"
    assert row["version"] == 2
    assert row["origin"] == "human_edit"

    # A STALE write -- expected_version=1 again, but the stored row is now
    # at version 2 (another writer, e.g. a racing edit or a narrate()
    # re-execution, landed first) -- is refused: returns False and writes
    # NOTHING, never silently overwriting the version-2 row above.
    rejected = persistence.upsert_narrative(
        _narrative_row(narrative_id, run_id=run_id, template_text="v3 (stale, must be refused)", version=3),
        expected_version=1,
    )
    assert rejected is False
    still_row = next(r for r in persistence.get_narratives(run_id) if r["narrative_id"] == narrative_id)
    assert still_row["template_text"] == "v2"
    assert still_row["version"] == 2
    assert still_row["origin"] == "human_edit"


def test_upsert_narrative_without_expected_version_is_unconditional(persistence, uid):
    # The default (`expected_version=None`, every narrate-node caller) is
    # UNCHANGED by the CAS addition: an unconditional upsert that always
    # applies and returns True, regardless of the stored version.
    run_id = f"RUN-NARR-UNCOND-{uid}"
    narrative_id = f"NARR-UNCOND-{uid}"
    first = persistence.upsert_narrative(_narrative_row(narrative_id, run_id=run_id, template_text="first", version=1))
    assert first is True
    second = persistence.upsert_narrative(_narrative_row(narrative_id, run_id=run_id, template_text="second", version=5))
    assert second is True
    row = next(r for r in persistence.get_narratives(run_id) if r["narrative_id"] == narrative_id)
    assert row["template_text"] == "second"
    assert row["version"] == 5


def test_get_narratives_scoped_to_run_id(persistence, uid):
    run_a = f"RUN-NARR-A-{uid}"
    run_b = f"RUN-NARR-B-{uid}"
    persistence.upsert_narrative(_narrative_row(f"NARR-A-{uid}", run_id=run_a))
    persistence.upsert_narrative(_narrative_row(f"NARR-B-{uid}", run_id=run_b))
    rows = persistence.get_narratives(run_a)
    assert {r["narrative_id"] for r in rows} == {f"NARR-A-{uid}"}


def _narrative_edit_row(edit_id, *, narrative_id, run_id, **overrides):
    base = dict(
        edit_id=edit_id, narrative_id=narrative_id, run_id=run_id, version=1, origin="generated",
        action="generated", actor="narrate", at=canonical_ts(0), before_text=None, after_text="first draft",
        diff=None, reason=None, call_id="CALL-1",
    )
    base.update(overrides)
    return base


def test_append_narrative_edit_is_append_only_and_idempotent_by_edit_id(persistence, uid):
    run_id = f"RUN-EDIT-{uid}"
    narrative_id = f"NARR-EDIT-{uid}"
    edit_id = f"EDIT-{uid}"
    persistence.append_narrative_edit(_narrative_edit_row(edit_id, narrative_id=narrative_id, run_id=run_id))
    # A retried write of the SAME edit_id must be a no-op -- never an update,
    # this table is never rewritten (G14, CLAUDE.md §5 Tier B).
    persistence.append_narrative_edit(
        _narrative_edit_row(edit_id, narrative_id=narrative_id, run_id=run_id, actor="someone-else", after_text="tampered")
    )
    edits = persistence.list_narrative_edits(run_id)
    assert len(edits) == 1
    assert edits[0]["actor"] == "narrate"  # the FIRST write, never overwritten
    assert edits[0]["after_text"] == "first draft"


def test_append_narrative_edit_grows_with_distinct_edit_ids(persistence, uid):
    run_id = f"RUN-EDIT-GROW-{uid}"
    narrative_id = f"NARR-EDIT-GROW-{uid}"
    persistence.append_narrative_edit(
        _narrative_edit_row(f"EDIT-1-{uid}", narrative_id=narrative_id, run_id=run_id, version=1, after_text="v1")
    )
    persistence.append_narrative_edit(
        _narrative_edit_row(
            f"EDIT-2-{uid}", narrative_id=narrative_id, run_id=run_id, version=2,
            origin="human_edit", action="human_edit", actor="bob", before_text="v1", after_text="v2",
            reason="fixed a typo",
        )
    )
    edits = persistence.list_narrative_edits(run_id)
    assert [e["version"] for e in edits] == [1, 2]
    assert edits[1]["origin"] == "human_edit"
    assert edits[1]["reason"] == "fixed a typo"


def _candidate_row(candidate_id, *, run_id, generation=0, rule_id="SKILL-001.ai.abc", **overrides):
    base = dict(
        candidate_id=candidate_id, run_id=run_id, engagement_id=None, skill_id="SKILL-001",
        generation=generation, rule_id=rule_id, title="An AI-proposed finding",
        metrics_cited=["hv_count"], producing_test_ids=["T4.1"], proposed_severity="Medium",
        severity_reason="15 exceptions is above the median", rationale=None, monetary_basis="spend",
        monetary_basis_note=None, exposure_amount=1500.0, headline_eligible=True,
        headline_ineligible_reason=None, candidate_status="candidate", decided_by=None,
        decided_at=None, decision_reason=None, decided_severity=None, call_id="CALL-1",
    )
    base.update(overrides)
    return base


def test_write_candidates_upserts_by_candidate_id_never_prunes(persistence, uid):
    run_id = f"RUN-CAND-{uid}"
    c1 = f"CAND-A-{uid}"
    c2 = f"CAND-B-{uid}"
    persistence.write_candidates(run_id, [_candidate_row(c1, run_id=run_id)], now=canonical_ts(0))
    persistence.write_candidates(
        run_id, [_candidate_row(c2, run_id=run_id, title="A second candidate")], now=canonical_ts(1)
    )
    # Unlike write_flagged_rows/write_run_metrics, a second write that omits c1
    # must NOT prune it -- candidates are never deleted (§5.2).
    ids = {c["candidate_id"] for c in persistence.list_candidates(run_id)}
    assert ids == {c1, c2}


def test_write_candidates_round_trip_json_fields_and_headline_eligible(persistence, uid):
    run_id = f"RUN-CAND-RT-{uid}"
    candidate_id = f"CAND-RT-{uid}"
    persistence.write_candidates(
        run_id, [_candidate_row(candidate_id, run_id=run_id, headline_eligible=False,
                                  headline_ineligible_reason="excess allocation unsupported")],
        now=canonical_ts(0),
    )
    row = persistence.list_candidates(run_id)[0]
    assert row["metrics_cited"] == ["hv_count"]
    assert row["producing_test_ids"] == ["T4.1"]
    assert row["headline_eligible"] is False
    assert row["headline_ineligible_reason"] == "excess allocation unsupported"
    assert row["candidate_status"] == "candidate"


def test_write_candidates_reupsert_does_not_reset_a_decision_or_created_at(persistence, uid):
    run_id = f"RUN-CAND-STICKY-{uid}"
    candidate_id = f"CAND-STICKY-{uid}"
    persistence.write_candidates(run_id, [_candidate_row(candidate_id, run_id=run_id)], now=canonical_ts(0))
    assert persistence.decide_candidate_cas(
        candidate_id, decision="accepted", reason=None, decided_severity="High", actor="alice", now=canonical_ts(1)
    ) is True

    # A re-executed `narrate` node for the SAME generation (an idempotent retry,
    # CLAUDE.md §2.3 rule 1) re-upserts the SAME candidate_id with fresh prose --
    # the decision and the first-seen timestamp must survive untouched.
    persistence.write_candidates(
        run_id, [_candidate_row(candidate_id, run_id=run_id, title="Re-narrated title")], now=canonical_ts(2)
    )
    row = persistence.list_candidates(run_id)[0]
    assert row["title"] == "Re-narrated title"  # prose columns DID update
    assert row["candidate_status"] == "accepted"  # decision untouched
    assert row["decided_by"] == "alice"
    assert row["decided_severity"] == "High"
    assert row["created_at"] == canonical_ts(0)  # sticky: first-seen timestamp


def test_decide_candidate_cas_guards_against_a_double_decision(persistence, uid):
    run_id = f"RUN-CAND-CAS-{uid}"
    candidate_id = f"CAND-CAS-{uid}"
    persistence.write_candidates(run_id, [_candidate_row(candidate_id, run_id=run_id)], now=canonical_ts(0))

    first = persistence.decide_candidate_cas(
        candidate_id, decision="accepted", reason=None, decided_severity="High", actor="alice", now=canonical_ts(1)
    )
    assert first is True

    # A second decision on the SAME candidate_id (a racing double-click, or a
    # regenerate racing a decision, §5.2/§11 T-C6) must be rejected -- zero rows
    # affected, never silently overwriting the first decision.
    second = persistence.decide_candidate_cas(
        candidate_id, decision="rejected", reason="duplicate decision", decided_severity=None,
        actor="bob", now=canonical_ts(2),
    )
    assert second is False

    row = persistence.list_candidates(run_id)[0]
    assert row["candidate_status"] == "accepted"
    assert row["decided_by"] == "alice"


def test_decide_candidate_cas_on_missing_candidate_returns_false(persistence, uid):
    assert persistence.decide_candidate_cas(
        f"CAND-MISSING-{uid}", decision="accepted", reason=None, decided_severity="High",
        actor="alice", now=canonical_ts(0),
    ) is False


def test_supersede_undecided_only_touches_undecided_rows_below_the_generation(persistence, uid):
    run_id = f"RUN-CAND-SUPERSEDE-{uid}"
    still_undecided = f"CAND-UNDECIDED-{uid}"
    already_accepted = f"CAND-ACCEPTED-{uid}"
    later_generation = f"CAND-LATER-{uid}"
    persistence.write_candidates(
        run_id,
        [
            _candidate_row(still_undecided, run_id=run_id, generation=0, rule_id="SKILL-001.ai.aaa"),
            _candidate_row(already_accepted, run_id=run_id, generation=0, rule_id="SKILL-001.ai.bbb"),
        ],
        now=canonical_ts(0),
    )
    persistence.decide_candidate_cas(
        already_accepted, decision="accepted", reason=None, decided_severity="High", actor="alice", now=canonical_ts(1)
    )
    persistence.write_candidates(
        run_id, [_candidate_row(later_generation, run_id=run_id, generation=1, rule_id="SKILL-001.ai.ccc")],
        now=canonical_ts(2),
    )

    count = persistence.supersede_undecided(run_id, below_generation=1, now=canonical_ts(3))
    assert count == 1  # only still_undecided (generation 0, still 'candidate')

    by_id = {c["candidate_id"]: c for c in persistence.list_candidates(run_id)}
    assert by_id[still_undecided]["candidate_status"] == "superseded"
    assert by_id[already_accepted]["candidate_status"] == "accepted"  # a decision is frozen, never superseded
    assert by_id[later_generation]["candidate_status"] == "candidate"  # generation 1 is not below_generation=1


def _theme_row(theme_id, *, run_id, generation=0, ordinal=1, **overrides):
    base = dict(
        theme_id=theme_id, run_id=run_id, generation=generation, ordinal=ordinal,
        finding_ids=["F1", "F2"], superseded=False, created_at=canonical_ts(0),
    )
    base.update(overrides)
    return base


def test_write_themes_upserts_by_theme_id_never_prunes(persistence, uid):
    run_id = f"RUN-THEME-{uid}"
    t1 = f"{run_id}:G0:TH1"
    t2 = f"{run_id}:G0:TH2"
    persistence.write_themes(run_id, [_theme_row(t1, run_id=run_id)], now=canonical_ts(0))
    persistence.write_themes(run_id, [_theme_row(t2, run_id=run_id, ordinal=2)], now=canonical_ts(1))
    ids = {t["theme_id"] for t in persistence.list_themes(run_id)}
    assert ids == {t1, t2}  # t1 is NOT pruned by the second write


def test_write_themes_round_trip_finding_ids_and_superseded_sticky_created_at(persistence, uid):
    run_id = f"RUN-THEME-RT-{uid}"
    theme_id = f"{run_id}:G0:TH1"
    persistence.write_themes(run_id, [_theme_row(theme_id, run_id=run_id)], now=canonical_ts(0))
    persistence.write_themes(
        run_id, [_theme_row(theme_id, run_id=run_id, finding_ids=["F1"], superseded=True)], now=canonical_ts(5)
    )
    row = persistence.list_themes(run_id)[0]
    assert row["finding_ids"] == ["F1"]
    assert row["superseded"] is True
    assert row["created_at"] == canonical_ts(0)  # sticky: first-seen timestamp


def test_put_test_line_values_upserts_and_prunes(persistence, uid):
    run_id = f"RUN-TLV-{uid}"
    persistence.put_test_line_values(
        run_id,
        [
            {"test_id": "T5.2", "source": "expense_report", "row_key": "k:1", "line_key": '["expense_report","k:1"]', "spend_amount": 100.0, "excess_amount": None},
            {"test_id": "T5.2", "source": "expense_report", "row_key": "k:2", "line_key": '["expense_report","k:2"]', "spend_amount": 200.0, "excess_amount": 50.0},
        ],
    )
    rows = persistence.list_test_line_values(run_id)
    assert {(r["row_key"], r["spend_amount"], r["excess_amount"]) for r in rows} == {
        ("k:1", 100.0, None), ("k:2", 200.0, 50.0),
    }

    # Second write: k:1 updates, k:2 is pruned, k:3 is new -- overwrite semantics,
    # the same MERGE + prune shape as write_flagged_rows/write_run_metrics.
    persistence.put_test_line_values(
        run_id,
        [
            {"test_id": "T5.2", "source": "expense_report", "row_key": "k:1", "line_key": '["expense_report","k:1"]', "spend_amount": 150.0, "excess_amount": None},
            {"test_id": "T5.2", "source": "expense_report", "row_key": "k:3", "line_key": '["expense_report","k:3"]', "spend_amount": 300.0, "excess_amount": None},
        ],
    )
    rows = persistence.list_test_line_values(run_id)
    assert {(r["row_key"], r["spend_amount"]) for r in rows} == {("k:1", 150.0), ("k:3", 300.0)}

    persistence.put_test_line_values(run_id, [])
    assert persistence.list_test_line_values(run_id) == []


# ── findings/management_actions new columns (migration 011) ────────────────


def test_write_findings_round_trips_ai_proposed_origin_fields(persistence, uid):
    from tests.test_persistence_p2 import _finding

    run_id = f"RUN-AIFIND-{uid}"
    skill_id = f"SKILL-{uid}"
    candidate_id = f"CAND-{uid}"
    rule_finding = _finding(f"{run_id}:T1", rule_id=f"{skill_id}.T1")
    ai_finding = _finding(
        f"{run_id}:ai.abc123", rule_id=f"{skill_id}.ai.abc123",
        origin="ai_proposed", candidate_id=candidate_id, accepted_by="alice", accepted_at=canonical_ts(1),
    )
    written = persistence.write_findings(
        run_id, [rule_finding, ai_finding], engagement_id="ENG-DEFAULT", skill_id=skill_id,
        skill_version="1.0.0", now=canonical_ts(0),
    )
    by_id = {f["finding_id"]: f for f in written}
    # a rule finding that never set origin comes back None -- no silent default
    assert by_id[rule_finding["finding_id"]]["origin"] is None
    assert by_id[rule_finding["finding_id"]]["candidate_id"] is None
    ai_row = by_id[ai_finding["finding_id"]]
    assert ai_row["origin"] == "ai_proposed"
    assert ai_row["candidate_id"] == candidate_id
    assert ai_row["accepted_by"] == "alice"
    assert ai_row["accepted_at"] == canonical_ts(1)

    listed = {f["finding_id"]: f for f in persistence.list_findings(run_id)}
    assert listed[ai_finding["finding_id"]]["origin"] == "ai_proposed"


def test_write_management_actions_round_trips_description_origin(persistence, uid):
    run_id = f"RUN-MADESC-{uid}"
    action_id = f"MA-DESC-{uid}"
    persistence.write_management_actions(
        run_id,
        [{
            "action_id": action_id, "finding_id": f"{run_id}:T1", "title": "Fix it",
            "status": "draft", "risk": "High", "description_origin": "model",
        }],
        now=canonical_ts(0),
    )
    action = persistence.list_management_actions(filters={"run_id": run_id})[0]
    assert action["description_origin"] == "model"

    # omitted entirely -> None, never a silent default
    other_id = f"MA-DESC-OMIT-{uid}"
    persistence.write_management_actions(
        run_id,
        [{"action_id": other_id, "finding_id": f"{run_id}:T2", "title": "Fix it too", "status": "draft", "risk": "Low"}],
        now=canonical_ts(1),
    )
    other = [a for a in persistence.list_management_actions(filters={"run_id": run_id}) if a["action_id"] == other_id][0]
    assert other["description_origin"] is None
