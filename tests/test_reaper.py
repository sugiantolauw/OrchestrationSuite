from __future__ import annotations

import dataclasses

from orchestrator.reaper import reap_orphaned_runs
from orchestrator.status import transition


def _fingerprint(fp_id):
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


def _state(run_id, status="queued", **overrides):
    from orchestrator.state import RunState

    base = dict(
        run_id=run_id, run_kind="fieldwork", mode="playbook", phase="plan",
        audit_period=("2026-01-01", "2026-01-31"), objective="t", run_owner="alice",
        fingerprint_id=f"FP-{run_id}", created_at="t0", last_state_change_at="t0",
        status=status, engagement_id="ENG-DEFAULT",
    )
    base.update(overrides)
    return RunState(**base)


def test_reaper_marks_running_as_interrupted(persistence):
    created = persistence.create_run(_state("RUN-R1"), _fingerprint("FP-RUN-R1"))
    running = transition(created, "running", now="t1")
    persistence.save_state(running)

    reaped = reap_orphaned_runs(persistence, now="t2")
    assert "RUN-R1" in reaped

    final = persistence.load_state("RUN-R1")
    assert final.status == "interrupted"
    assert "orphaned" in (final.status_reason or "").lower()


def test_reaper_leaves_non_running_untouched(persistence):
    persistence.create_run(_state("RUN-Q1", status="queued"), _fingerprint("FP-RUN-Q1"))

    created2 = persistence.create_run(_state("RUN-C1"), _fingerprint("FP-RUN-C1"))
    running2 = transition(created2, "running", now="t1", phase="plan")
    saved2 = persistence.save_state(running2)
    confirmed2 = transition(saved2, "awaiting_confirmation", now="t2")
    persistence.save_state(confirmed2)

    created3 = persistence.create_run(_state("RUN-DONE", phase="export"), _fingerprint("FP-RUN-DONE"))
    running3 = transition(created3, "running", now="t1")
    saved3 = persistence.save_state(running3)
    completed3 = transition(saved3, "completed", now="t2")
    persistence.save_state(completed3)

    before_runs = {r["run_id"]: r for r in persistence.list_runs()}

    reaped = reap_orphaned_runs(persistence, now="t9")
    assert reaped == []

    after_runs = {r["run_id"]: r for r in persistence.list_runs()}
    assert before_runs.keys() == after_runs.keys()
    for run_id in before_runs:
        assert before_runs[run_id]["status"] == after_runs[run_id]["status"]


def test_reaper_never_deletes_rows(persistence):
    persistence.create_run(_state("RUN-KEEP"), _fingerprint("FP-RUN-KEEP"))
    running = transition(persistence.load_state("RUN-KEEP"), "running", now="t1")
    persistence.save_state(running)

    before_count = len(persistence.list_runs())
    reap_orphaned_runs(persistence, now="t2")
    after_count = len(persistence.list_runs())
    assert before_count == after_count


def test_reaper_closes_open_node_attempts(persistence):
    created = persistence.create_run(_state("RUN-OPEN"), _fingerprint("FP-RUN-OPEN"))
    running = transition(created, "running", now="t1")
    persistence.save_state(running)
    persistence.begin_node_attempt(
        run_id="RUN-OPEN", phase="plan", node_index=0, node_name="discover",
        state_version_before=2, now="t2",
    )

    reap_orphaned_runs(persistence, now="t3")

    attempts = persistence.list_node_attempts("RUN-OPEN")
    assert attempts[0]["outcome"] == "interrupted"


def test_reaper_skips_stale_state_error(local_persistence):
    persistence = local_persistence
    created = persistence.create_run(_state("RUN-RACE"), _fingerprint("FP-RUN-RACE"))
    running = transition(created, "running", now="t1")
    persistence.save_state(running)

    orig_save_state = persistence.save_state
    call_count = {"n": 0}

    def patched_save_state(state):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # simulate someone else moving the run in between load and save
            from orchestrator.errors import StaleStateError

            raise StaleStateError("RUN-RACE", state.state_version, state.state_version + 5)
        return orig_save_state(state)

    persistence.save_state = patched_save_state
    try:
        reaped = reap_orphaned_runs(persistence, now="t2")
    finally:
        persistence.save_state = orig_save_state

    assert reaped == []
