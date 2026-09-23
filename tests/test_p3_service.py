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


def _build_ctx(tmp_path: Path, *, worker_id: str = "worker-a") -> service.AppContext:
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path / "data"),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "ORCH_WORKER_ID": worker_id,
        "SKILLS_DIR": str(MINI_SKILL_DIR.parent),
    }
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

        filename, content = service.get_export(ctx, run_id, "xlsx")
        assert filename == "workpaper.xlsx"
        assert len(content) > 0

        actions = service.list_management_actions(ctx, filters={"run_id": run_id})
        assert len(actions) == 2

        events = service.list_trace_events(ctx, run_id)
        assert any(e["event_type"] == "signed_off" for e in events)
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
