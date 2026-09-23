"""app.py's own App-start wiring (CLAUDE.md §2.3 rule 2, §4.1) -- renders
against the REAL orchestrator.service + LocalPersistence (overriding this
directory's autouse fake_backend fixture) so a wiring regression is caught
here, not just in the fake backend, which has no persistence object at all
for this to call."""

from __future__ import annotations

from pathlib import Path

from conftest import load_app_entry
from src.platform import adapters


def test_app_start_repairs_projections_before_reaping(monkeypatch, tmp_path):
    """Item 5 (CLAUDE.md §4.1, P2/P3 gate review): repair_projections() must
    run at App start, before the reaper. Its docstring
    (orchestrator/adapters/protocols.py) always said "Called at App start
    alongside the reaper" -- nothing in app.py ever actually called it, so a
    `runs` row left stale by a failed projection update after a save (CLAUDE.md
    §9C/B4) stayed stale forever. Both calls are wrapped here to record the
    order they fire in."""
    from orchestrator import service as real_service
    import orchestrator.executor as executor_module

    repo_root = Path(__file__).resolve().parents[2]
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(repo_root / "skills"),
    }
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))

    ctx = adapters.get_context()
    order: list[str] = []

    original_repair = ctx.persistence.repair_projections

    def _tracked_repair():
        order.append("repair_projections")
        return original_repair()

    monkeypatch.setattr(ctx.persistence, "repair_projections", _tracked_repair)

    original_reap = executor_module.reap_orphaned_runs_with_leases

    def _tracked_reap(*a, **k):
        order.append("reap")
        return original_reap(*a, **k)

    monkeypatch.setattr(executor_module, "reap_orphaned_runs_with_leases", _tracked_reap)

    entry = load_app_entry()
    try:
        entry._start_executor_once()
        # ThreadExecutor.start() also runs its own periodic reap on a
        # background admission thread (orchestrator/executor.py), so "reap"
        # may appear more than once and its exact count/timing is not what
        # this test is about -- only that repair_projections() runs, and
        # runs strictly before the FIRST reap, matching item 5's ordering
        # requirement.
        assert "repair_projections" in order, order
        assert "reap" in order, order
        assert order.index("repair_projections") < order.index("reap"), order
        assert order.count("repair_projections") == 1, order
    finally:
        ctx.executor.stop()
        adapters._ctx = None
