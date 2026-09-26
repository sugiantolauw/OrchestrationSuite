"""orchestrator.warmup (CLAUDE.md §11 perf follow-up, 2026-09-26): a one-shot,
non-blocking background warm-up thread started once at App start. Exercised
here against small fakes -- no live workspace, no Dash -- so these run in
the same fast `cd app && pytest` pass as everything else in this directory.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from orchestrator.warmup import maybe_start_warmup, run_startup_warmup


class _CountingPersistence:
    def __init__(self):
        self.find_runs_calls = 0

    def find_runs(self, statuses):
        self.find_runs_calls += 1
        return []


class _CountingUCPool:
    def __init__(self):
        self.checkout_calls = 0
        self.checkin_calls = 0

    def checkout(self):
        self.checkout_calls += 1
        return "conn-1"

    def checkin(self, conn):
        self.checkin_calls += 1


class _CountingDataSource:
    def __init__(self):
        self.list_tables_calls = 0

    def list_tables(self):
        self.list_tables_calls += 1
        return []


def _make_ctx(*, backend="uc", startup_warmup=True, list_skills_fn=None):
    persistence = _CountingPersistence()
    uc_pool = _CountingUCPool()
    data_source = _CountingDataSource()
    settings = SimpleNamespace(startup_warmup=startup_warmup)
    ctx = SimpleNamespace(
        persistence=persistence, uc_pool=uc_pool, backend=backend, settings=settings,
        data_source_factory=lambda *a, **k: data_source,
    )
    return ctx, persistence, uc_pool, data_source


def test_warmup_calls_every_step_exactly_once(monkeypatch):
    ctx, persistence, uc_pool, data_source = _make_ctx()

    skill_list_calls = {"n": 0}

    def fake_list_skills(_ctx):
        skill_list_calls["n"] += 1
        return []

    monkeypatch.setattr("orchestrator.service.list_skills", fake_list_skills)

    run_startup_warmup(ctx)

    assert persistence.find_runs_calls == 1
    assert uc_pool.checkout_calls == 1
    assert uc_pool.checkin_calls == 1
    assert data_source.list_tables_calls == 1
    assert skill_list_calls["n"] == 1


def test_warmup_skips_uc_steps_on_local_backend(monkeypatch):
    ctx, persistence, uc_pool, data_source = _make_ctx(backend="local")
    ctx.uc_pool = None  # local backend never has one (AppContext.uc_pool)

    skill_list_calls = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.service.list_skills", lambda _ctx: skill_list_calls.__setitem__("n", skill_list_calls["n"] + 1)
    )

    run_startup_warmup(ctx)

    assert persistence.find_runs_calls == 1
    assert data_source.list_tables_calls == 0
    assert skill_list_calls["n"] == 1


def test_a_failing_step_is_logged_and_swallowed_and_the_rest_still_run(monkeypatch):
    ctx, persistence, uc_pool, data_source = _make_ctx()

    def _raise(*a, **k):
        raise RuntimeError("warehouse unreachable")

    persistence.find_runs = _raise

    skill_list_calls = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.service.list_skills", lambda _ctx: skill_list_calls.__setitem__("n", skill_list_calls["n"] + 1)
    )

    run_startup_warmup(ctx)  # must not raise

    assert uc_pool.checkout_calls == 1
    assert data_source.list_tables_calls == 1
    assert skill_list_calls["n"] == 1


def test_disabled_setting_means_no_calls_at_all(monkeypatch):
    ctx, persistence, uc_pool, data_source = _make_ctx(startup_warmup=False)

    skill_list_calls = {"n": 0}
    monkeypatch.setattr(
        "orchestrator.service.list_skills", lambda _ctx: skill_list_calls.__setitem__("n", skill_list_calls["n"] + 1)
    )

    thread = maybe_start_warmup(ctx)

    assert thread is None
    assert persistence.find_runs_calls == 0
    assert uc_pool.checkout_calls == 0
    assert data_source.list_tables_calls == 0
    assert skill_list_calls["n"] == 0


def test_enabled_setting_starts_exactly_one_thread_that_does_not_outlive_completion(monkeypatch):
    ctx, persistence, uc_pool, data_source = _make_ctx(startup_warmup=True)
    monkeypatch.setattr("orchestrator.service.list_skills", lambda _ctx: [])

    before = {t.ident for t in threading.enumerate()}
    thread = maybe_start_warmup(ctx)

    assert thread is not None
    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True

    thread.join(timeout=5)
    assert not thread.is_alive()

    # One-shot only: no timer, no loop -- once the thread has finished, it
    # must have left nothing else running behind it.
    after = {t.ident for t in threading.enumerate()}
    assert after - before == set()

    assert persistence.find_runs_calls == 1
    assert uc_pool.checkout_calls == 1
    assert data_source.list_tables_calls == 1


def test_idle_after_warmup_makes_zero_further_calls(monkeypatch):
    """Once the one-shot warm-up thread has finished, nothing it touched may
    be called again while the process sits idle -- the same idle-cost
    discipline as EXECUTOR_IDLE_POLL_INTERVAL_S (CLAUDE.md §11 cost
    incident: no periodic re-probing of the warehouse/UC while idle)."""
    ctx, persistence, uc_pool, data_source = _make_ctx(startup_warmup=True)
    monkeypatch.setattr("orchestrator.service.list_skills", lambda _ctx: [])

    thread = maybe_start_warmup(ctx)
    thread.join(timeout=5)
    assert not thread.is_alive()

    calls_after_warmup = (persistence.find_runs_calls, uc_pool.checkout_calls, data_source.list_tables_calls)

    time.sleep(0.3)  # simulated idle period -- nothing here should be a timer

    assert (persistence.find_runs_calls, uc_pool.checkout_calls, data_source.list_tables_calls) == calls_after_warmup
