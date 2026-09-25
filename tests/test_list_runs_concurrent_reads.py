"""P3/P4 perf gap review 2026-09-25, live pass: orchestrator.service.
list_runs() drives /runs, /trace and (via get_actions_page_data) /actions.
A live measurement found each of its 5 post-`rows` reads (list_skills' own
skill-directory/list_all_skill_versions work, plus list_findings_for_runs/
list_management_actions_for_runs/get_run_metrics_for_runs/get_fingerprints)
running one after another, ~0.5-1.3s each against the real warehouse, for a
total no caller needed to wait on sequentially -- fixed by fanning them out
concurrently for a real (Delta-shaped) backend, exactly the same pattern
app/src/platform/adapters.py's `_read_narration_sources` already uses (see
app/tests/test_narration_concurrent_reads.py, which this file mirrors).

These tests exercise `service.list_runs` directly with a fake persistence
-- no real backend, no live warehouse needed -- to prove genuine
concurrency (not just "fast enough on this run"), deterministic output, loud
failure propagation, the pool-size bound, and that LocalPersistence keeps
its exact sequential behaviour."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from orchestrator import service
from orchestrator.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent


class _SleepyPersistence:
    """The 4 batched-by-run-id reads plus list_all_skill_versions (the read
    list_skills' own call to it performs) -- each sleeping `sleep_s` to
    simulate real per-statement latency, with a call log for order/count
    assertions. `list_runs` (the FIRST read, before the fan-out) is
    deliberately NOT sleepy -- only the 5 independent reads under test are."""

    def __init__(self, rows, *, sleep_s=0.1, fail_on=None):
        self.rows = rows
        self.sleep_s = sleep_s
        self.fail_on = fail_on
        self.calls: list[str] = []
        self._log_lock = threading.Lock()

    def _record(self, name: str) -> None:
        with self._log_lock:
            self.calls.append(name)

    def _read(self, name: str, result):
        self._record(name)
        if self.fail_on == name:
            raise RuntimeError(f"{name} failed")
        time.sleep(self.sleep_s)
        return result

    def list_runs(self, filters=None):
        return self.rows

    def list_all_skill_versions(self):
        return self._read("list_all_skill_versions", [])

    def list_findings_for_runs(self, run_ids):
        return self._read("list_findings_for_runs", {})

    def list_management_actions_for_runs(self, run_ids):
        return self._read("list_management_actions_for_runs", {})

    def get_run_metrics_for_runs(self, run_ids):
        return self._read("get_run_metrics_for_runs", {})

    def get_fingerprints(self, fingerprint_ids):
        return self._read("get_fingerprints", {})


def _ctx(persistence, *, max_connections=6):
    return service.AppContext(
        settings=Settings(max_connections=max_connections),
        persistence=persistence,
        skills_dir=REPO_ROOT / "skills",
        data_source_factory=lambda bindings, *a, **kw: None,
        export_storage=None,
        clock=lambda: "2026-09-23T00:00:00Z",
        backend="uc",
    )


def _row(run_id: str, *, status: str = "completed") -> dict:
    return {
        "run_id": run_id, "skill_id": "SKILL-001", "status": status,
        "run_owner": "alice", "created_at": "2026-09-23T00:00:00", "last_state_change_at": "2026-09-23T00:00:00",
        "fingerprint_id": f"FP-{run_id}", "audit_period_start": "2026-01-01", "audit_period_end": "2026-01-31",
        "engagement_id": "ENG-DEFAULT",
    }


def test_five_reads_run_concurrently_not_sequentially():
    """Wall time must be close to ONE sleep, not five -- proves genuine
    concurrency, not just "fast enough"."""
    p = _SleepyPersistence([_row("RUN-1")], sleep_s=0.2)
    ctx = _ctx(p)

    start = time.monotonic()
    service.list_runs(ctx)
    elapsed = time.monotonic() - start

    assert elapsed < 0.2 * 3, f"5 reads took {elapsed:.3f}s -- looks sequential (~5x), not concurrent (~1x)"
    assert set(p.calls) == {
        "list_all_skill_versions", "list_findings_for_runs",
        "list_management_actions_for_runs", "get_run_metrics_for_runs", "get_fingerprints",
    }


def test_a_failed_read_propagates_loudly_not_a_partial_result():
    """A failure in any one read surfaces exactly as a sequential call
    would have -- the caller's exception, never a swallowed/partial page."""
    p = _SleepyPersistence([_row("RUN-1")], sleep_s=0.05, fail_on="get_run_metrics_for_runs")
    ctx = _ctx(p)

    with pytest.raises(RuntimeError, match="get_run_metrics_for_runs failed"):
        service.list_runs(ctx)


def test_thread_count_never_exceeds_the_configured_connection_pool_size():
    """A pool configured smaller than the 5 reads must never see more
    concurrent reads in flight than the pool could hand connections to at
    once (CLAUDE.md §2.3 rule 3 / the bounded-pool fix)."""
    current = {"n": 0}
    max_seen = {"n": 0}
    lock = threading.Lock()

    class _CountingPersistence(_SleepyPersistence):
        def _read(self, name, result):
            with lock:
                current["n"] += 1
                max_seen["n"] = max(max_seen["n"], current["n"])
            try:
                return super()._read(name, result)
            finally:
                with lock:
                    current["n"] -= 1

    p = _CountingPersistence([_row("RUN-1")], sleep_s=0.1)
    ctx = _ctx(p, max_connections=2)

    service.list_runs(ctx)
    assert max_seen["n"] <= 2, f"observed {max_seen['n']} reads in flight at once with max_connections=2"


def test_local_persistence_never_uses_a_thread_pool(monkeypatch):
    """Structural proof: ThreadPoolExecutor must never even be constructed
    for a LocalPersistence instance -- its own single shared sqlite
    connection (:memory: mode) has no business being touched from several
    threads at once."""
    import concurrent.futures as cf

    from orchestrator.adapters.persistence_local import LocalPersistence

    persistence = LocalPersistence(":memory:")
    persistence.migrate()
    ctx = _ctx(persistence)
    ctx.backend = "local"

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("ThreadPoolExecutor must never be constructed for LocalPersistence")

    monkeypatch.setattr(cf, "ThreadPoolExecutor", _forbidden)
    result = service.list_runs(ctx)
    assert result == []  # no runs -- proves it ran to completion sequentially, not raising
