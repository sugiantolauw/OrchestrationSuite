"""Full-run integration tests through the service API (CLAUDE.md build brief
P3 §5): start_audit_run -> executor picks it up -> awaiting_signoff ->
sign_off -> completed with an XLSX export, and restart survival (a worker
claims a run and "dies" mid-run; its lease expires; the reaper marks the run
`interrupted`; a second worker resumes and completes it). Uses ORCH_BACKEND=
local against the small "mini" Skill fixture data this file writes itself,
so the whole suite runs in well under a second per test -- the one slow,
real-synthetic_data/ full run lives in tests/test_p3_synthetic_full_run.py."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pandas as pd
import pytest

from orchestrator import service
from orchestrator.executor import reap_orphaned_runs_with_leases
from tests.test_p3_nodes import MINI_SKILL_DIR, _write_mini_data


@contextlib.contextmanager
def _count_sql_statements():
    """P3/P4 perf gap review 2026-09-25: counts every SQL statement sqlite3
    actually executes, across every connection LocalPersistence opens during
    this block, via `sqlite3.Connection.set_trace_callback` -- one entry per
    top-level statement, the same granularity as one warehouse round trip in
    DeltaPersistence. LocalPersistence's file-mode `_connect()` opens a NEW
    connection per call (CLAUDE.md build brief P1A/P3: no in-memory sharing
    outside tests), so this patches `sqlite3.connect` at the
    orchestrator.adapters.persistence_local module level -- the one place
    every such connection is actually created -- to attach the callback to
    each one, rather than trying to reach into a connection this test never
    holds a reference to."""
    import orchestrator.adapters.persistence_local as pl

    count = [0]
    real_connect = pl.sqlite3.connect

    def _traced_connect(*a, **k):
        conn = real_connect(*a, **k)
        conn.set_trace_callback(lambda _stmt: count.__setitem__(0, count[0] + 1))
        return conn

    pl.sqlite3.connect = _traced_connect
    try:
        yield count
    finally:
        pl.sqlite3.connect = real_connect


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


# ── §11 "Paused runs across a code deploy" / independent review 2026-09-24
# gap #11: a run pins the code revision it started on; a redeploy must not
# strand it forever, but a run paused before execute completed must not
# silently run under different code either. ───────────────────────────────


def _copy_mini_skills(tmp_path: Path) -> Path:
    """A private copy of tests/fixtures/skills (never the shared fixture
    itself -- these tests mutate a Skill file on disk to change its content
    hash mid-test, and must not leave that dirty for every OTHER test in the
    session)."""
    import shutil

    dest = tmp_path / "skills"
    shutil.copytree(MINI_SKILL_DIR.parent, dest)
    return dest


def test_run_continues_its_export_on_a_new_code_revision_after_signoff(tmp_path):
    """The one narrow exception (CLAUDE.md §11): a run signed off under one
    code revision may complete its export under a later one, since sign-off
    means execute already fixed this run's numbers. Both `computed_code_
    revision` (the fingerprint's own, immutable value) and `export_code_
    revision` (recorded, never silent) are surfaced on get_run."""
    skills_dir = _copy_mini_skills(tmp_path)

    ctx_a = _build_ctx(
        tmp_path, worker_id="worker-a",
        env_overrides={"CODE_REVISION": "rev-old", "SKILLS_DIR": str(skills_dir)},
    )
    ctx_a.executor.start()
    try:
        bindings = service.suggest_bindings(ctx_a, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx_a, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="revision test", run_owner="tester",
        )
        status = _wait_for_status(ctx_a, run_id, {"awaiting_signoff", "failed"})
        run = service.get_run(ctx_a, run_id)
        assert status == "awaiting_signoff", run.get("status_reason")
    finally:
        ctx_a.executor.stop()

    # Sign off directly against persistence, never through service.sign_off
    # -- that would also call ctx_a.executor.start(), waking the (deliberately
    # stopped) old-revision worker back up and racing this test's own
    # new-revision worker B for the export phase.
    from orchestrator import runs as runs_module

    runs_module.sign_off(ctx_a.persistence, run_id, actor="approver", now=ctx_a.clock())
    assert ctx_a.persistence.load_state(run_id).phase == "export"

    # A fresh deployment: same persistence, a NEW code revision -- exactly
    # what a redeploy after sign-off leaves behind.
    ctx_b = _build_ctx(
        tmp_path, worker_id="worker-b",
        env_overrides={"CODE_REVISION": "rev-new", "SKILLS_DIR": str(skills_dir)},
    )
    ctx_b.executor.start()
    try:
        status = _wait_for_status(ctx_b, run_id, {"completed", "failed"})
        run = service.get_run(ctx_b, run_id)
        assert status == "completed", run.get("status_reason")
        assert run["computed_code_revision"] == "rev-old"
        assert run["export_code_revision"] == "rev-new"

        row = ctx_b.persistence.get_run_row(run_id)
        assert row["export_code_revision"] == "rev-new"
    finally:
        ctx_b.executor.stop()


def test_signed_off_run_still_fails_if_more_than_code_revision_changed(tmp_path):
    """The relaxation is code_revision ONLY: a run whose Skill content ALSO
    changed between sign-off and the export attempt must still fail loudly,
    never silently export under a materially different setup."""
    skills_dir = _copy_mini_skills(tmp_path)

    ctx_a = _build_ctx(
        tmp_path, worker_id="worker-a",
        env_overrides={"CODE_REVISION": "rev-old", "SKILLS_DIR": str(skills_dir)},
    )
    ctx_a.executor.start()
    try:
        bindings = service.suggest_bindings(ctx_a, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx_a, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="revision test", run_owner="tester",
        )
        status = _wait_for_status(ctx_a, run_id, {"awaiting_signoff", "failed"})
        assert status == "awaiting_signoff"
    finally:
        ctx_a.executor.stop()

    from orchestrator import runs as runs_module

    runs_module.sign_off(ctx_a.persistence, run_id, actor="approver", now=ctx_a.clock())

    # A real change to the Skill's content, not just code_revision -- a
    # thresholds.yaml edit between sign-off and export.
    (skills_dir / "mini" / "thresholds.yaml").write_text(
        (skills_dir / "mini" / "thresholds.yaml").read_text() + "\n# edited after sign-off\n"
    )

    ctx_b = _build_ctx(
        tmp_path, worker_id="worker-b",
        env_overrides={"CODE_REVISION": "rev-new", "SKILLS_DIR": str(skills_dir)},
    )
    ctx_b.executor.start()
    try:
        status = _wait_for_status(ctx_b, run_id, {"completed", "failed"})
        assert status == "failed"
        run = service.get_run(ctx_b, run_id)
        assert "skill_content_hash" in (run.get("status_reason") or "")
    finally:
        ctx_b.executor.stop()


def test_confirm_refused_and_restart_service_function_for_a_stale_pre_execute_run(tmp_path):
    """A run paused before its tests ran (plan confirmation) on an older code
    revision cannot proceed (CLAUDE.md §11): confirm_plan refuses with
    RunCodeRevisionStale rather than silently confirming into the new setup,
    and restart_stale_run is the "one click" resolution -- a fresh run with
    the same parameters, with the old one marked superseded, never deleted."""
    from orchestrator.errors import RunCodeRevisionStale

    skills_dir = _copy_mini_skills(tmp_path)

    ctx_a = _build_ctx(
        tmp_path, worker_id="worker-a",
        env_overrides={"CODE_REVISION": "rev-old", "SKILLS_DIR": str(skills_dir)},
    )
    # Suppress immediate admission (this run must stay `awaiting_confirmation`,
    # never proceed into execute under worker A).
    ctx_a.executor = None
    bindings = service.suggest_bindings(ctx_a, "SKILL-MINI")
    run_id = service.start_audit_run(
        ctx_a, skill_id="SKILL-MINI", bindings=bindings,
        audit_period=("2026-01-01", "2026-02-28"), objective="stale confirm test",
        run_owner="tester", review_plan_first=True,
    )

    # Run discover/profile/plan by hand (no execute phase yet) so the run
    # reaches `awaiting_confirmation` -- the same state a real Playbook run
    # with review_plan_first=True would reach once its own worker got to it.
    from orchestrator.pipeline import run_phase

    fingerprint_a = service.build_run_fingerprint(ctx_a, ctx_a.persistence.load_state(run_id))
    node_ctx = service.build_node_context(ctx_a, ctx_a.persistence.load_state(run_id))
    run_phase(
        ctx_a.persistence, run_id, nodes_for=service.NODES_FOR, skill=node_ctx, clock=ctx_a.clock,
        current_fingerprint=fingerprint_a,
    )
    state_a = ctx_a.persistence.load_state(run_id)
    assert state_a.status == "awaiting_confirmation", state_a.status_reason

    # A fresh deployment: same persistence, a NEW code revision -- confirm
    # is attempted under the new code before the plan was ever confirmed.
    ctx_b = _build_ctx(
        tmp_path, worker_id="worker-b",
        env_overrides={"CODE_REVISION": "rev-new", "SKILLS_DIR": str(skills_dir)},
    )
    ctx_b.executor = None
    with pytest.raises(RunCodeRevisionStale):
        service.confirm_plan(ctx_b, run_id, "operator")
    # Refused, never silently confirmed.
    assert ctx_b.persistence.load_state(run_id).status == "awaiting_confirmation"

    new_run_id = service.restart_stale_run(ctx_b, run_id, "operator")
    assert new_run_id != run_id

    old_row = ctx_b.persistence.get_run_row(run_id)
    assert old_row["superseded_by"] == new_run_id
    # Never deleted (CLAUDE.md §9A Q2) -- the old run's own state is untouched.
    assert ctx_b.persistence.load_state(run_id).status == "awaiting_confirmation"

    new_state = ctx_b.persistence.load_state(new_run_id)
    assert new_state.skill_id == "SKILL-MINI"
    assert new_state.objective == "stale confirm test"
    assert new_state.run_owner == "tester"


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


def test_run_completes_promptly_with_default_settings_idle_sweep_off(tmp_path):
    """CLAUDE.md P3 fix (2026-09-24): EXECUTOR_IDLE_POLL_INTERVAL_S now
    defaults to 0 (the periodic idle sweep is off entirely) -- a run must
    still reach awaiting_signoff and then completed promptly using NOTHING
    but default Settings (no env override at all), because every transition
    wakes the executor directly rather than depending on any sweep."""
    ctx = _build_ctx(tmp_path)
    assert ctx.settings.executor_idle_poll_interval_s == 0.0
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="idle-sweep-off default test",
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


def test_get_run_and_list_runs_report_run_state_status_not_a_lagging_runs_projection(tmp_path):
    """P3/P4 perf gap review 2026-09-25 (BUG-STATUS-1): a run page was seen
    live showing 'Queued -- waiting for an available run slot' while
    node_attempts showed a node actively executing for the same run_id.
    service.get_run and service.list_runs both already source `status` from
    `run_state` (CLAUDE.md §9C/B4: 'the runs projection is a convenience for
    cheap listing/filtering ... repair_projections() catches up') --
    LocalPersistence.list_runs and DeltaPersistence.list_runs both JOIN
    run_state and report ITS status column, never the runs table's own. This
    locks that in: directly corrupts ONLY the `runs` projection's status
    column (bypassing save_state, simulating exactly the lag repair_
    projections exists to catch up on) and asserts both functions still
    report the true, authoritative run_state.status -- never the stale
    projection value."""
    ctx = _build_ctx(tmp_path)
    ctx.executor = None
    bindings = service.suggest_bindings(ctx, "SKILL-MINI")
    run_id = service.start_audit_run(
        ctx, skill_id="SKILL-MINI", bindings=bindings,
        audit_period=("2026-01-01", "2026-02-28"), objective="status projection regression",
        run_owner="tester",
    )
    from orchestrator.status import transition

    state = ctx.persistence.load_state(run_id)
    ctx.persistence.save_state(transition(state, "running", now=ctx.clock()))

    conn = ctx.persistence._connect()
    try:
        conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", ("queued", run_id))
        conn.commit()
    finally:
        ctx.persistence._release(conn)

    run = service.get_run(ctx, run_id)
    assert run["status"] == "running", "get_run must read run_state.status, never the runs projection"

    listed = next(r for r in service.list_runs(ctx) if r["run_id"] == run_id)
    assert listed["status"] == "Running", "list_runs must read run_state.status, never the runs projection"


# ── cross_run_totals (independent review 2026-09-24 gap #6) ─────────────────
#
# Pure function over the shape service.list_runs() already returns, so these
# are plain dict fixtures -- no executor, no persistence.

def _run(
    run_id, *, status="Completed", skill_id="SKILL-001", engagement_id="ENG-A",
    audit_period="2025-01-01 – 2025-03-31", run_timestamp="2026-01-01T00:00:00Z",
    high_risk_count=0, potential_exposure=None, superseded_by=None,
):
    return {
        "run_id": run_id, "status": status, "skill_id": skill_id, "engagement_id": engagement_id,
        "audit_period": audit_period, "run_timestamp": run_timestamp,
        "high_risk_count": high_risk_count, "potential_exposure": potential_exposure,
        "superseded_by": superseded_by,
    }


def test_cross_run_totals_excludes_rerun_and_failed_runs():
    # B1c: /runs' "High-risk findings" used to sum high_risk_count over
    # EVERY run, including a failed attempt and every re-run of the same
    # skill/period alongside its predecessor.
    runs = [
        _run("RUN-OLD", high_risk_count=3, potential_exposure=1000),
        _run("RUN-NEW", high_risk_count=5, potential_exposure=2000, run_timestamp="2026-02-01T00:00:00Z"),
        _run("RUN-FAILED", status="Failed", high_risk_count=9, potential_exposure=9999,
             audit_period="2025-04-01 – 2025-06-30", run_timestamp="2026-04-01T00:00:00Z"),
        _run("RUN-QUEUED", status="Queued", high_risk_count=7,
             audit_period="2025-07-01 – 2025-09-30", run_timestamp="2026-07-01T00:00:00Z"),
    ]
    totals = service.cross_run_totals(runs)
    assert totals["high_risk_findings_total"] == 5, "only RUN-NEW (the latest re-run) counts"
    assert totals["total_exposure"] == 2000


def test_cross_run_totals_counts_awaiting_signoff_as_eligible():
    # A run whose tests already ran and whose numbers are already fixed
    # (only export is outstanding) is a trustworthy figure, not a re-run in
    # progress.
    runs = [_run("RUN-1", status="Awaiting Signoff", high_risk_count=2, potential_exposure=500)]
    totals = service.cross_run_totals(runs)
    assert totals == {"high_risk_findings_total": 2, "total_exposure": 500}


def test_cross_run_totals_excludes_superseded_run():
    runs = [_run("RUN-SUPERSEDED", high_risk_count=4, potential_exposure=4000, superseded_by="RUN-NEW")]
    totals = service.cross_run_totals(runs)
    assert totals == {"high_risk_findings_total": 0, "total_exposure": None}


def test_cross_run_totals_sums_disjoint_periods():
    runs = [
        _run("RUN-1", audit_period="2025-01-01 – 2025-03-31", potential_exposure=8000),
        _run("RUN-2", audit_period="2025-04-01 – 2025-06-30", potential_exposure=3000,
             run_timestamp="2026-04-01T00:00:00Z"),
    ]
    totals = service.cross_run_totals(runs)
    assert totals["total_exposure"] == 11000


def test_cross_run_totals_never_sums_overlapping_periods():
    # gap #6 (B4b): two DIFFERENT audit periods that still overlap in time
    # (Jan-Mar and Feb-Apr) are the same underlying spend tested twice under
    # two windows -- summing them would double-count March. The most recent
    # run's own figure stands in, never a sum.
    runs = [
        _run("RUN-1", audit_period="2025-01-01 – 2025-03-31", potential_exposure=8000,
             run_timestamp="2026-01-01T00:00:00Z"),
        _run("RUN-2", audit_period="2025-02-01 – 2025-04-30", potential_exposure=3000,
             run_timestamp="2026-04-01T00:00:00Z"),
    ]
    totals = service.cross_run_totals(runs)
    assert totals["total_exposure"] == 3000, "the latest run's own figure, never a sum across overlapping periods"


def test_cross_run_totals_overlap_across_skills_also_never_summed():
    # The overlap rule is stated over periods, not scoped to one Skill --
    # two Skills' runs over overlapping windows are still never summed.
    runs = [
        _run("RUN-A", skill_id="SKILL-001", audit_period="2025-01-01 – 2025-06-30", potential_exposure=5000,
             run_timestamp="2026-01-01T00:00:00Z"),
        _run("RUN-B", skill_id="SKILL-002", audit_period="2025-03-01 – 2025-09-30", potential_exposure=6000,
             run_timestamp="2026-06-01T00:00:00Z"),
    ]
    totals = service.cross_run_totals(runs)
    assert totals["total_exposure"] == 6000


def test_cross_run_totals_no_eligible_runs_returns_none_never_zero():
    totals = service.cross_run_totals([_run("RUN-1", status="Running", potential_exposure=1000)])
    assert totals == {"high_risk_findings_total": 0, "total_exposure": None}


def test_cross_run_totals_run_with_no_exposure_metric_is_excluded_not_zero():
    # B4 (NN14): a run whose finding set has no run_exposure_headline
    # metric (potential_exposure=None) never gets treated as $0 -- it drops
    # out of the sum entirely rather than dragging the total down.
    runs = [
        _run("RUN-1", audit_period="2025-01-01 – 2025-03-31", potential_exposure=None),
        _run("RUN-2", audit_period="2025-04-01 – 2025-06-30", potential_exposure=7000,
             run_timestamp="2026-04-01T00:00:00Z"),
    ]
    totals = service.cross_run_totals(runs)
    assert totals["total_exposure"] == 7000


# ── update_management_action (independent review 2026-09-24 gap #3) ─────────

def test_update_management_action_persists_and_is_reflected_by_list(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="local run test",
            run_owner="tester",
        )
        _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"})
        service.sign_off(ctx, run_id, "approver")
        _wait_for_status(ctx, run_id, {"completed", "failed"})

        actions = service.list_management_actions(ctx, filters={"run_id": run_id})
        assert actions, "the mini Skill's findings should have drafted management actions"
        action_id = actions[0]["action_id"]

        updated = service.update_management_action(
            ctx, action_id, owner="Alex Chen", status="agreed", target_date="2026-03-01",
            response="Agreed with management.", actor="reviewer@example.com",
        )
        assert updated["owner"] == "Alex Chen"
        assert updated["status"] == "Agreed"  # title-cased, same as list_management_actions' own status
        assert updated["target_date"] == "2026-03-01"
        assert updated["response"] == "Agreed with management."
        assert updated["updated_by"] == "reviewer@example.com"

        # a completely fresh list call, exactly what a page reload makes --
        # not the return value of update_management_action itself.
        reread = next(a for a in service.list_management_actions(ctx, filters={"run_id": run_id})
                      if a["action_id"] == action_id)
        assert reread["owner"] == "Alex Chen"
        assert reread["status"] == "Agreed"
        assert reread["target_date"] == "2026-03-01"
        assert reread["description"] == "Agreed with management."
        assert reread["updated_by"] == "reviewer@example.com"
    finally:
        ctx.executor.stop()


def test_update_management_action_rejects_an_unrecognised_status(tmp_path):
    ctx = _build_ctx(tmp_path)
    with pytest.raises(ValueError):
        service.update_management_action(
            ctx, "MA-DOES-NOT-EXIST", owner=None, status="not_a_real_status", target_date=None,
            response=None, actor="reviewer@example.com",
        )


# ── SQL statement-count regression guards (P3/P4 perf gap review 2026-09-25)
#
# LocalPersistence for counts (bounded on statement COUNT, not wall time --
# portable across machines); the live-warehouse timings these bounds were
# calibrated against are reported separately, never asserted here. Bounds
# are generous headroom over what this file's own fixtures measure today,
# not the exact count -- the point is catching a reintroduced per-row/
# per-source/per-skill query LOOP (an O(n) shape), not pinning an exact
# statement total that would make this brittle to an unrelated, harmless
# one-query change elsewhere.

def test_start_run_statement_count_has_no_per_source_or_per_skill_n_plus_one(tmp_path):
    """BUG-PERF-1: start_audit_run's own synchronous work (before it hands
    off to the executor and returns) used to scale with the number of
    contract sources it resolves versions for AND, via list_skills inside
    register_skill's risk/control seeding path, with the number of skill
    directories on disk -- 8 sources x cold DESCRIBE HISTORY + an
    unconditional register_skill (record_skill_version + upsert_risks +
    upsert_controls, one SELECT+UPDATE/INSERT round trip PER risk/control,
    every single run) measured live at ~87-89s end to end, ~80s of it inside
    register_skill alone for a Skill whose content had not changed since the
    last run. `ctx.executor = None` isolates exactly this synchronous
    portion -- no node ever runs here."""
    ctx = _build_ctx(tmp_path)
    ctx.executor = None
    bindings = service.suggest_bindings(ctx, "SKILL-MINI")

    with _count_sql_statements() as first_count:
        service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="stmt count 1", run_owner="tester",
        )
    assert first_count[0] <= 60, (
        f"start_audit_run issued {first_count[0]} SQL statements on a Skill's first "
        "registration this process -- check for a reintroduced N+1"
    )

    # A second run of the SAME Skill (same skill_id/version/content_hash)
    # must be cheaper: register_skill's risk/control seeding is guaranteed
    # unchanged content and must not be repeated (_ensure_skill_registered's
    # in-process cache) -- this is the concrete regression guard for that fix.
    with _count_sql_statements() as second_count:
        service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="stmt count 2", run_owner="tester",
        )
    assert second_count[0] < first_count[0], (
        f"a second start_audit_run for an already-registered Skill issued "
        f"{second_count[0]} statements, not fewer than the first run's {first_count[0]} -- "
        "register_skill's risk/control seeding is being repeated for unchanged content"
    )
    assert second_count[0] <= 30, (
        f"start_audit_run issued {second_count[0]} SQL statements on an already-registered "
        "Skill -- check for a reintroduced N+1"
    )


def test_landing_page_skill_listing_statement_count_bounded(tmp_path):
    """BUG-PERF-1 (landing page): list_skills used to call
    ctx.persistence.list_runs(filters={"skill_id": skill_id}) once PER SKILL
    DIRECTORY (measured live at 17.06s for a single skill), each as
    expensive as the single unfiltered call this now makes ONCE regardless
    of skill count. tests/fixtures/skills has 2 skill directories (mini,
    mini_candidates) -- this must cost one list_runs() call, not two."""
    ctx = _build_ctx(tmp_path)
    ctx.executor = None
    with _count_sql_statements() as count:
        skills = service.list_skills(ctx)
    assert len(skills) >= 2, "fixture must offer at least 2 skill directories for this to be a real N+1 guard"
    assert count[0] <= 16, f"list_skills issued {count[0]} SQL statements for {len(skills)} skills"


def test_runs_page_statement_count_bounded_regardless_of_run_count(tmp_path):
    """BUG-PERF-1 (/runs page, ~9-35s live): service.list_runs already
    batches per-run findings/actions/metrics/fingerprint lookups (independent
    review 2026-09-24 item 6) -- this guards the one remaining path that used
    to scale with run count via list_skills' own former per-skill N+1 (fixed
    above): 5 runs must cost the same handful of statements as 1."""
    ctx = _build_ctx(tmp_path)
    ctx.executor = None
    bindings = service.suggest_bindings(ctx, "SKILL-MINI")
    for i in range(5):
        service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective=f"runs page count {i}", run_owner="tester",
        )

    with _count_sql_statements() as count:
        rows = service.list_runs(ctx)
    assert len(rows) >= 5
    assert count[0] <= 30, f"list_runs issued {count[0]} SQL statements for {len(rows)} runs"


def test_sign_off_statement_count_bounded(tmp_path):
    """BUG-PERF-2: sign-off's own write must not cost more than a small,
    fixed number of statements regardless of narration/candidate volume --
    the visible several-second lag reported live was the run PAGE re-loading
    RunState a second time after the write already had it in hand
    (app/src/run_status.py's now-fixed `_refresh_from_state`), not sign_off
    itself, but this still guards sign_off's own write path against a
    reintroduced per-row loop."""
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="sign off stmt count", run_owner="tester",
        )
        _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"})
        status = service.get_run(ctx, run_id)["status"]
        assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")

        with _count_sql_statements() as count:
            service.sign_off(ctx, run_id, "approver")
        assert count[0] <= 100, f"sign_off issued {count[0]} SQL statements"
    finally:
        ctx.executor.stop()
