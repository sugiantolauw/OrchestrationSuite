"""Full-run integration tests through the service API (CLAUDE.md build brief
P3 §5): start_audit_run -> executor picks it up -> awaiting_signoff ->
sign_off -> completed with an XLSX export, and restart survival (a worker
claims a run and "dies" mid-run; its lease expires; the reaper marks the run
`interrupted`; a second worker resumes and completes it). Uses ORCH_BACKEND=
local against the small "mini" Skill fixture data this file writes itself,
so the whole suite runs in well under a second per test -- the one slow,
real-synthetic_data/ full run lives in tests/test_p3_synthetic_full_run.py."""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pytest

from orchestrator import service
from orchestrator.executor import reap_orphaned_runs_with_leases
from tests.test_p3_nodes import MINI_SKILL_DIR, _write_mini_data


def _build_ctx(tmp_path: Path, *, worker_id: str = "worker-a", env_overrides: dict | None = None) -> service.AppContext:
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path / "data"),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "ORCH_WORKER_ID": worker_id,
        "SKILLS_DIR": str(MINI_SKILL_DIR.parent),
        # Pinned rather than derived from `git rev-parse HEAD` (the fallback
        # in orchestrator.fingerprint._resolve_code_revision): several agents
        # commit to this checkout concurrently, so HEAD can legitimately move
        # between a run's creation and this test's later resume_run() call --
        # a real drift the fingerprint SHOULD catch in production, but not one
        # this test is exercising, so it is held fixed here.
        "CODE_REVISION": "test-fixed-revision",
    }
    env.update(env_overrides or {})
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    _write_mini_data(data_dir)
    return service.build_app_context(env)


def _wait_for_status(ctx, run_id, statuses, timeout=15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = service.get_run(ctx, run_id)["status"]
        if last in statuses:
            return last
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {statuses} in time (last={last})")


def test_full_local_run_to_signoff_and_export(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        skills = service.list_skills(ctx)
        assert any(s["skill_id"] == "SKILL-MINI" for s in skills)

        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        assert bindings == {"claims": "claims.csv", "register": "register.csv"}

        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="local run test",
            run_owner="tester",
        )
        status = _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"})
        run = service.get_run(ctx, run_id)
        assert status == "awaiting_signoff", run.get("status_reason")

        payload = service.get_run_payload(ctx, run_id)
        assert len(payload["findings"]) == 2
        assert payload["exposure"]["headline"] == 2300.0

        runs_list = service.list_runs(ctx)
        row = next(r for r in runs_list if r["run_id"] == run_id)
        assert row["data_mode"] == "Local test data"
        assert row["findings_count"] == 2
        assert row["potential_exposure"] == 2300.0

        frames = service.get_run_frames(ctx, run_id)
        assert set(frames) == {"claims", "register"}
        assert "RF_HV" in frames["claims"].columns
        assert int(frames["claims"]["RF_HV"].sum()) == 3

        service.sign_off(ctx, run_id, "approver")
        status = _wait_for_status(ctx, run_id, {"completed", "failed"})
        run = service.get_run(ctx, run_id)
        assert status == "completed", run.get("status_reason")
        assert run["signoff"]["approver"] == "approver"

        # run_owner="tester" above, sign-off actor="approver" -- a genuine
        # non-self sign-off (CLAUDE.md §11 self sign-off decision:
        # self_approved/sod_enforced are recorded on every sign-off, not
        # only the self-approved ones).
        assert run["signoff"]["self_approved"] is False
        assert run["signoff"]["sod_enforced"] is False

        # CLAUDE.md §4.8's runs.approved_by projection column, P2/P3 gate
        # review item 9: sign_off sets RunState.signoff but nothing wrote it
        # into the runs table's own approved_by column, so it stayed NULL on
        # every completed run.
        row = next(r for r in ctx.persistence.list_runs() if r["run_id"] == run_id)
        assert row["approved_by"] == "approver"

        runs_list_row = next(r for r in service.list_runs(ctx) if r["run_id"] == run_id)
        assert runs_list_row["approved_by"] == "approver"
        assert runs_list_row["self_approved"] is False
        assert runs_list_row["sod_enforced"] is False

        filename, content = service.get_export(ctx, run_id, "xlsx")
        assert filename == "workpaper.xlsx"
        assert len(content) > 0

        actions = service.list_management_actions(ctx, filters={"run_id": run_id})
        assert len(actions) == 2

        events = service.list_trace_events(ctx, run_id)
        signed_off_events = [e for e in events if e["event_type"] == "signed_off"]
        assert signed_off_events
        # non-self sign-off: no "self-approved" suffix on the trace message.
        assert "self-approved" not in signed_off_events[0]["message"]
    finally:
        ctx.executor.stop()


def test_run_completes_promptly_with_a_near_unreachable_idle_sweep_interval(tmp_path):
    """Found-live cost review: the admission loop's idle safety-sweep
    interval must never be what makes a run progress -- every transition
    (start_audit_run/sign_off) wakes the executor directly
    (ctx.executor.start(run_id, phase) -> _try_admit), so a run must reach
    awaiting_signoff and then completed well within a couple of seconds
    even with EXECUTOR_IDLE_POLL_INTERVAL_S set far longer than this test's
    own timeout. If admission or sign-off ever came to depend on the idle
    sweep instead of the direct wake, this test would time out."""
    ctx = _build_ctx(tmp_path, env_overrides={
        "EXECUTOR_ACTIVE_POLL_INTERVAL_S": "0.05",
        "EXECUTOR_IDLE_POLL_INTERVAL_S": "120",
    })
    assert ctx.settings.executor_idle_poll_interval_s == 120.0
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="idle-sweep test",
            run_owner="tester",
        )
        status = _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"}, timeout=5)
        run = service.get_run(ctx, run_id)
        assert status == "awaiting_signoff", run.get("status_reason")

        service.sign_off(ctx, run_id, "approver")
        status = _wait_for_status(ctx, run_id, {"completed", "failed"}, timeout=5)
        run = service.get_run(ctx, run_id)
        assert status == "completed", run.get("status_reason")
    finally:
        ctx.executor.stop()


def test_self_signoff_is_labelled_self_approved_and_sod_not_enforced(tmp_path):
    """CLAUDE.md §11 "accept all defaults, allow self sign-off for now": when
    the sign-off actor equals run_owner, sign_off must not block it (self
    sign-off stays allowed until P7) but must record self_approved=True and
    sod_enforced=False everywhere the sign-off is later read, and the trace
    event message must carry the "segregation of duties not enforced"
    suffix."""
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="self signoff test",
            run_owner="same-person",
        )
        status = _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"})
        run = service.get_run(ctx, run_id)
        assert status == "awaiting_signoff", run.get("status_reason")

        service.sign_off(ctx, run_id, "same-person")
        status = _wait_for_status(ctx, run_id, {"completed", "failed"})
        run = service.get_run(ctx, run_id)
        assert status == "completed", run.get("status_reason")

        assert run["signoff"]["approver"] == "same-person"
        assert run["signoff"]["self_approved"] is True
        assert run["signoff"]["sod_enforced"] is False

        runs_list_row = next(r for r in service.list_runs(ctx) if r["run_id"] == run_id)
        assert runs_list_row["self_approved"] is True
        assert runs_list_row["sod_enforced"] is False
        assert runs_list_row["approved_by"] == "same-person"

        events = service.list_trace_events(ctx, run_id)
        signed_off_events = [e for e in events if e["event_type"] == "signed_off"]
        assert signed_off_events
        assert "self-approved (segregation of duties not enforced)" in signed_off_events[0]["message"]
    finally:
        ctx.executor.stop()


def test_suggest_bindings_never_fuzzy(tmp_path):
    ctx = _build_ctx(tmp_path)
    bindings = service.suggest_bindings(ctx, "SKILL-MINI")
    # exact source-name matches only -- claims.csv/register.csv are the
    # contract's own declared filenames, never a substring/fuzzy guess.
    assert bindings["claims"] == "claims.csv"
    assert bindings["register"] == "register.csv"


def test_start_audit_run_rejects_missing_binding(tmp_path):
    ctx = _build_ctx(tmp_path)
    with pytest.raises(Exception):
        service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings={"claims": "claims.csv"},  # register missing
            audit_period=("2026-01-01", "2026-02-28"), objective="x", run_owner="tester",
        )


def test_start_audit_run_writes_data_assets_in_the_same_insert_as_the_run(tmp_path):
    """Regression (found live, against a real deployed App): data_assets
    used to be written in a SEPARATE save_state call after create_run,
    leaving a window where the row was already `queued` -- visible to any
    ALREADY-RUNNING executor's admission loop (find_runs(["queued"])),
    polling independently of this process -- but data_assets was still
    empty. That other executor's own first CAS transition (queued ->
    running) would then win the race, and this service's own follow-up
    save_state call lost with StaleStateError. data_assets is now part of
    create_run's own initial insert, so state_version stays 1 and
    data_assets is never empty for a row any executor can see."""
    ctx = _build_ctx(tmp_path)
    ctx.executor = None  # isolate the create_run write itself
    bindings = service.suggest_bindings(ctx, "SKILL-MINI")
    run_id = service.start_audit_run(
        ctx, skill_id="SKILL-MINI", bindings=bindings,
        audit_period=("2026-01-01", "2026-02-28"), objective="race regression", run_owner="tester",
    )
    state = ctx.persistence.load_state(run_id)
    assert state.status == "queued"
    assert state.state_version == 1, "data_assets must not require a second write after creation"
    assert state.data_assets
    assert {b["source"] for b in state.data_assets} == set(bindings)


# ── restart survival: worker A dies mid-run, worker B resumes and finishes ──


def test_restart_survival_across_worker_death(tmp_path):
    ctx_a = _build_ctx(tmp_path, worker_id="worker-a")
    bindings = service.suggest_bindings(ctx_a, "SKILL-MINI")
    # Suppress start_audit_run's immediate-admission call (§4: "if
    # ctx.executor is set also call executor.start for immediacy") -- this
    # test fabricates worker A's crash by hand and must not race a real
    # background execution of the same run.
    ctx_a.executor = None

    run_id = service.start_audit_run(
        ctx_a, skill_id="SKILL-MINI", bindings=bindings,
        audit_period=("2026-01-01", "2026-02-28"), objective="restart test", run_owner="tester",
    )

    # Worker A claims the lease and starts, then "dies" (we never call
    # executor.start()'s background loop -- instead we admit it once by hand
    # via a short-TTL lease, then simply never renew or release it, exactly
    # as a crashed process would leave it).
    now = ctx_a.clock()
    acquired = ctx_a.persistence.acquire_lease(run_id, "worker-a", ttl_s=1, now=now)
    assert acquired
    # Move the run to `running` without actually executing any node -- models
    # a crash immediately after admission claimed the lease and the pipeline
    # loop transitioned queued -> running.
    from orchestrator.status import transition

    state = ctx_a.persistence.load_state(run_id)
    running = transition(state, "running", now=ctx_a.clock())
    ctx_a.persistence.save_state(running)

    # A live run (a different, healthy, long-TTL lease) started BEFORE the
    # short-TTL one expires, so a single `later` timestamp can distinguish
    # "expired" from "still live" for both at once.
    other_run_id = service.start_audit_run(
        ctx_a, skill_id="SKILL-MINI", bindings=bindings,
        audit_period=("2026-01-01", "2026-02-28"), objective="healthy run", run_owner="tester",
    )
    ctx_a.persistence.acquire_lease(other_run_id, "worker-a", ttl_s=999999, now=ctx_a.clock())
    other_state = ctx_a.persistence.load_state(other_run_id)
    ctx_a.persistence.save_state(transition(other_state, "running", now=ctx_a.clock()))

    # Time passes -- the ttl_s=1 lease expires; the ttl_s=999999 one does not.
    time.sleep(1.2)
    later = ctx_a.clock()
    assert ctx_a.persistence.expired_leases(later) == [run_id]

    reaped = reap_orphaned_runs_with_leases(ctx_a.persistence, now=later)
    assert run_id in reaped
    assert ctx_a.persistence.load_state(run_id).status == "interrupted"
    assert other_run_id not in reaped
    assert ctx_a.persistence.load_state(other_run_id).status == "running"

    # Worker B resumes the interrupted run and completes it.
    ctx_b = service.AppContext(
        settings=ctx_a.settings, persistence=ctx_a.persistence, skills_dir=ctx_a.skills_dir,
        data_source_factory=ctx_a.data_source_factory, export_storage=ctx_a.export_storage,
        clock=ctx_a.clock, backend=ctx_a.backend, local_data_root=ctx_a.local_data_root,
    )
    from orchestrator.executor import ThreadExecutor

    ctx_b.executor = ThreadExecutor(
        persistence=ctx_b.persistence, settings=ctx_b.settings, worker_id="worker-b",
        ctx_factory=lambda rid: service.build_node_context(ctx_b, ctx_b.persistence.load_state(rid)),
        fingerprint_factory=lambda rid: service.build_run_fingerprint(ctx_b, ctx_b.persistence.load_state(rid)),
        clock=ctx_b.clock, poll_interval_s=0.1, lease_ttl_s=30,
    )
    ctx_b.executor.start()
    try:
        service.resume_run(ctx_b, run_id, "operator")
        status = _wait_for_status(ctx_b, run_id, {"awaiting_signoff", "failed"})
        run = service.get_run(ctx_b, run_id)
        assert status == "awaiting_signoff", run.get("status_reason")
    finally:
        ctx_b.executor.stop()


# ── queue environment affinity: the UI-facing note (P3 gate review item 4) ──


def test_get_run_and_list_runs_surface_a_queue_note_for_a_different_deployment(tmp_path):
    """A run created under a different deployment's code_revision (an
    in-place redeploy leaving a stale queued row, or a shared dev database)
    is never admitted by THIS deployment's executor (tests/test_p3_executor.py
    covers that side) -- this covers the read path: service.get_run and
    service.list_runs both surface WHY it is not progressing."""
    ctx = _build_ctx(tmp_path)  # CODE_REVISION="test-fixed-revision"
    ctx.executor = None  # isolate the read-path note from any executor's own admission gate
    from orchestrator import runs as runs_module

    fp = dict(
        fingerprint_id="FP-FOREIGN-NOTE",
        source_table_versions="{}", uploaded_file_hashes="{}", reference_data_hashes="{}",
        skill_content_hash=None, code_revision="rev-a-different-deployment",
        dependency_lock_hash="dep1", runtime_config_hash="rc-other",
        endpoint_config="{}", prompt_template_version="none", created_at=ctx.clock(),
    )
    run_id = "RUN-FOREIGN-NOTE"
    runs_module.create_run(
        ctx.persistence, run_id=run_id, run_kind="fieldwork", engagement_id="ENG-DEFAULT",
        skill_id=None, skill_version=None, mode="playbook",
        audit_period=("2026-01-01", "2026-01-31"), objective="t", run_owner="alice",
        options={}, fingerprint=fp, now=ctx.clock(),
    )

    run = service.get_run(ctx, run_id)
    assert run["status"] == "queued"
    assert run["queue_note"] is not None
    assert "different deployment" in run["queue_note"]
    assert "rev-a-differ" in run["queue_note"]  # truncated to 12 chars, service._queue_affinity_note

    rows = service.list_runs(ctx)
    row = next(r for r in rows if r["run_id"] == run_id)
    assert row["queue_note"] == run["queue_note"]


def test_queue_note_is_none_for_a_run_created_under_this_same_deployment(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor = None  # isolate the read path -- do not let it actually get admitted
    bindings = service.suggest_bindings(ctx, "SKILL-MINI")
    run_id = service.start_audit_run(
        ctx, skill_id="SKILL-MINI", bindings=bindings,
        audit_period=("2026-01-01", "2026-02-28"), objective="own deployment", run_owner="tester",
    )
    run = service.get_run(ctx, run_id)
    assert run["status"] == "queued"
    assert run["queue_note"] is None

    rows = service.list_runs(ctx)
    row = next(r for r in rows if r["run_id"] == run_id)
    assert row["queue_note"] is None


def test_queue_note_is_none_once_the_run_is_no_longer_queued(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor = None
    from orchestrator import runs as runs_module
    from orchestrator.status import transition

    fp = dict(
        fingerprint_id="FP-FOREIGN-BUT-RUNNING",
        source_table_versions="{}", uploaded_file_hashes="{}", reference_data_hashes="{}",
        skill_content_hash=None, code_revision="rev-a-different-deployment",
        dependency_lock_hash="dep1", runtime_config_hash="rc-other",
        endpoint_config="{}", prompt_template_version="none", created_at=ctx.clock(),
    )
    run_id = "RUN-FOREIGN-RUNNING"
    state = runs_module.create_run(
        ctx.persistence, run_id=run_id, run_kind="fieldwork", engagement_id="ENG-DEFAULT",
        skill_id=None, skill_version=None, mode="playbook",
        audit_period=("2026-01-01", "2026-01-31"), objective="t", run_owner="alice",
        options={}, fingerprint=fp, now=ctx.clock(),
    )
    # Moved on somehow (e.g. admitted before this deployment existed) -- the
    # note is specifically about a run stuck `queued`, not any run whose
    # fingerprint happens to differ.
    ctx.persistence.save_state(transition(state, "running", now=ctx.clock()))

    run = service.get_run(ctx, run_id)
    assert run["queue_note"] is None
