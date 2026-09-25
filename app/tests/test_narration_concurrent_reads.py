"""P3/P4 perf gap review 2026-09-25, concurrent-reads follow-up: CLAUDE.md
§2.1's "nothing over a couple of seconds ... runs in a callback" -- a real
render at the sign-off gate was measured at ~3.3-3.6s, dominated by 5
independent, sequential persistence reads (findings/run_metrics/
candidates/themes/narratives) that adapters._read_narration_sources now
runs concurrently via a small ThreadPoolExecutor for a real (Delta) backend,
while keeping LocalPersistence's exact sequential behaviour (see that
function's own docstring for why).

These tests exercise `_read_narration_sources` directly with a fake
persistence -- no real backend, no live warehouse needed -- to prove the
properties a live measurement can only sample, not guarantee: genuine
concurrency (not just "fast enough on this run"), deterministic assembly
order, loud failure propagation, and the pool-size bound. The statement-COUNT
regression test ("still 6 statements") lives in test_run_status_narration.py
(test_render_issues_a_small_constant_number_of_statements) against a real
LocalPersistence run -- unaffected by this change, and re-run here only
implicitly by being in the same test session.
"""

from __future__ import annotations

import threading
import time

import pytest

from src.platform import adapters


class _FakeSettings:
    def __init__(self, max_connections=6):
        self.max_connections = max_connections
        self.narration_enabled = True


class _FakeCtx:
    def __init__(self, persistence, *, max_connections=6):
        self.persistence = persistence
        self.settings = _FakeSettings(max_connections=max_connections)
        # Deliberately "uc" regardless of what persistence actually is --
        # detection in _read_narration_sources is by the persistence
        # instance's own type, never this label (see
        # test_local_persistence_runs_sequentially_despite_backend_label
        # below for the case that matters).
        self.backend = "uc"


class _SleepyPersistence:
    """5 independent reads, each sleeping `sleep_s` to simulate real
    per-statement latency, with a call log for order/count assertions."""

    def __init__(self, *, sleep_s=0.1, fail_on=None):
        self.sleep_s = sleep_s
        self.fail_on = fail_on
        self.calls: list[str] = []
        self._log_lock = threading.Lock()

    def _record(self, name: str) -> None:
        with self._log_lock:
            self.calls.append(name)

    def _read(self, name: str, run_id: str):
        self._record(name)
        if self.fail_on == name:
            raise RuntimeError(f"{name} failed")
        time.sleep(self.sleep_s)
        return [{"tag": name, "run_id": run_id}]

    def list_findings(self, run_id):
        return self._read("list_findings", run_id)

    def get_run_metrics(self, run_id):
        return self._read("get_run_metrics", run_id)

    def list_candidates(self, run_id):
        return self._read("list_candidates", run_id)

    def list_themes(self, run_id):
        return self._read("list_themes", run_id)

    def get_narratives(self, run_id):
        return self._read("get_narratives", run_id)


def test_five_reads_run_concurrently_not_sequentially():
    """Wall time for 5 reads, each sleeping sleep_s, must be close to ONE
    sleep, not five -- proves genuine concurrency, not just "fast enough"."""
    p = _SleepyPersistence(sleep_s=0.2)
    ctx = _FakeCtx(p)

    start = time.monotonic()
    results = adapters._read_narration_sources(ctx, p, "RUN-1")
    elapsed = time.monotonic() - start

    assert len(results) == 5
    assert elapsed < 0.2 * 3, f"5 reads took {elapsed:.3f}s -- looks sequential (~5x), not concurrent (~1x)"
    assert set(p.calls) == {
        "list_findings", "get_run_metrics", "list_candidates", "list_themes", "get_narratives",
    }


def test_results_assembled_in_fixed_order_regardless_of_completion_order():
    """Deterministic assembly: findings, metrics, candidates, themes,
    narratives -- in that order -- never whichever future finished first."""
    p = _SleepyPersistence(sleep_s=0.05)
    ctx = _FakeCtx(p)

    results = adapters._read_narration_sources(ctx, p, "RUN-1")
    tags = [r[0]["tag"] for r in results]
    assert tags == ["list_findings", "get_run_metrics", "list_candidates", "list_themes", "get_narratives"]


def test_a_failed_read_propagates_loudly_not_a_partial_result():
    """A failure in any one read surfaces exactly as a sequential call
    would have -- the caller's exception, never a swallowed/partial page."""
    p = _SleepyPersistence(sleep_s=0.05, fail_on="list_themes")
    ctx = _FakeCtx(p)

    with pytest.raises(RuntimeError, match="list_themes failed"):
        adapters._read_narration_sources(ctx, p, "RUN-1")


def test_thread_count_never_exceeds_the_configured_connection_pool_size():
    """A pool configured smaller than the 5 reads must never see more
    concurrent reads in flight than the pool could hand connections to at
    once (CLAUDE.md §2.3 rule 3 / the bounded-pool fix)."""
    current = {"n": 0}
    max_seen = {"n": 0}
    lock = threading.Lock()

    class _CountingPersistence(_SleepyPersistence):
        def _read(self, name, run_id):
            with lock:
                current["n"] += 1
                max_seen["n"] = max(max_seen["n"], current["n"])
            try:
                return super()._read(name, run_id)
            finally:
                with lock:
                    current["n"] -= 1

    p = _CountingPersistence(sleep_s=0.1)
    ctx = _FakeCtx(p, max_connections=2)

    adapters._read_narration_sources(ctx, p, "RUN-1")
    assert max_seen["n"] <= 2, f"observed {max_seen['n']} reads in flight at once with max_connections=2"


def test_local_persistence_runs_sequentially_despite_backend_label():
    """Detection is by the persistence instance's own type, not
    `ctx.backend` -- real test fixtures (app/tests/test_run_status_narration.py's
    `real_run`, the one behind the "still 6 statements" regression test)
    build an AppContext around a real LocalPersistence without setting
    `backend`, which then defaults to "uc" (orchestrator/service.py's
    AppContext.backend). If detection trusted that label, this exact
    fixture would parallelise reads against sqlite -- the risk this test
    guards against."""
    from orchestrator.adapters.persistence_local import LocalPersistence

    persistence = LocalPersistence(":memory:")
    persistence.migrate()

    class _CtxWrongBackendLabel:
        backend = "uc"  # deliberately wrong

        class settings:
            max_connections = 6

    ctx = _CtxWrongBackendLabel()
    ctx.persistence = persistence

    results = adapters._read_narration_sources(ctx, persistence, "RUN-MISSING")
    assert len(results) == 5
    assert all(r == [] or r == {} for r in results)  # no such run -- every read empty, none errors


def test_local_persistence_never_uses_a_thread_pool(monkeypatch):
    """Structural proof: ThreadPoolExecutor must never even be constructed
    for a LocalPersistence instance."""
    import concurrent.futures as cf

    from orchestrator.adapters.persistence_local import LocalPersistence

    persistence = LocalPersistence(":memory:")
    persistence.migrate()
    ctx = _FakeCtx(persistence)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("ThreadPoolExecutor must never be constructed for LocalPersistence")

    monkeypatch.setattr(cf, "ThreadPoolExecutor", _forbidden)
    adapters._read_narration_sources(ctx, persistence, "RUN-MISSING")
