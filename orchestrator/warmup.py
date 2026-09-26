"""One-shot connection/cache warm-up at App start (CLAUDE.md §11 perf
follow-up, 2026-09-26): a background thread opens one connection in the
Delta pool, one connection in the UC pool (uc backend only), authenticates
the shared WorkspaceClient and primes the skill-list/catalog-listing caches
the pages already use -- so the first real page load after an App start
does not pay for any of it.

One-shot only: no timer, no loop, nothing periodic -- the same idle-cost
discipline as the executor's idle-sweep fix (CLAUDE.md §11 cost incident:
never poll the warehouse while idle). Every step is independent and
non-fatal: a failure is logged as a warning and the caller falls back to
lazy init exactly as before this existed. Never touches run status or
state, and caches nothing that would let a warmed value silently outlive a
real change (CLAUDE.md non-negotiable 14).

Only runs when `Settings.startup_warmup` is true -- off by default, so
pytest and the e2e_local subprocess fixtures never spawn it unasked."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

_LOG = logging.getLogger(__name__)


def _timed_step(name: str, fn: Callable[[], None]) -> None:
    start = time.monotonic()
    try:
        fn()
    except Exception:
        _LOG.warning("startup warm-up: %s failed (non-fatal)", name, exc_info=True)
    else:
        _LOG.info("startup warm-up: %s took %.2fs", name, time.monotonic() - start)


def _warm_delta_pool(ctx: Any) -> None:
    # A cheap, read-only call that already goes through the pooled
    # connection (readiness.check_warehouse uses the same call) -- opens
    # one pool connection without touching or caching any run's status or
    # state (CLAUDE.md non-negotiable 14).
    ctx.persistence.find_runs(["queued"])


def _warm_uc_pool(ctx: Any) -> None:
    conn = ctx.uc_pool.checkout()
    ctx.uc_pool.checkin(conn)


def _warm_uc_catalog_listing(ctx: Any) -> None:
    # Constructs and authenticates UCTableDataSource's WorkspaceClient (the
    # "shared" client the UC source-binding dropdowns use) as a side effect
    # of the same call that fills datasource_uc.py's process-wide
    # _TABLE_LISTING_CACHE -- the two costs are paid together on a real
    # page, so warming them together mirrors that.
    ctx.data_source_factory({}).list_tables()


def _warm_skill_list(ctx: Any) -> None:
    from orchestrator import service

    service.list_skills(ctx)


def run_startup_warmup(ctx: Any) -> None:
    """Runs every applicable step once, in order, on whatever thread calls
    it. Never raises -- each step's own failure is caught and logged by
    `_timed_step`."""
    if getattr(ctx, "persistence", None) is not None:
        _timed_step("delta pool connection", lambda: _warm_delta_pool(ctx))

    if getattr(ctx, "uc_pool", None) is not None:
        _timed_step("uc pool connection", lambda: _warm_uc_pool(ctx))

    if getattr(ctx, "backend", None) == "uc":
        _timed_step("workspace client auth + uc catalog listing", lambda: _warm_uc_catalog_listing(ctx))

    _timed_step("skill list", lambda: _warm_skill_list(ctx))


def maybe_start_warmup(ctx: Any) -> threading.Thread | None:
    """Starts the one-shot background warm-up thread iff
    `ctx.settings.startup_warmup` is true. Returns the thread (already
    started) for tests, or None when the setting is off -- in which case
    nothing here makes a single call."""
    if not getattr(ctx.settings, "startup_warmup", False):
        return None
    thread = threading.Thread(target=run_startup_warmup, args=(ctx,), name="startup-warmup", daemon=True)
    thread.start()
    return thread
