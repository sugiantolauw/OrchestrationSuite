"""In-process registry and bounded worker pool for asynchronous run
creation.

CLAUDE.md §11 "Run start opens the run page at once" (2026-09-25): the
web tier's "Start audit analysis" callback (src/run_setup.py) pre-generates
a run_id, submits the real adapters.start_audit_run(...) call to THIS
module's own small, dedicated ThreadPoolExecutor (max 2 workers) -- never
orchestrator's pipeline executor, which only ever starts once the `runs`
row that call is about to create already exists (CLAUDE.md §2.1: "the web
tier never executes a node") -- and navigates to /run/<run_id> at once.

Until that row exists, /run/<id> (src/run_status.py) reads this registry to
render the page's existing "Queued" status state, or its existing error
panel on failure (NN14: loud, never silent). Nothing here talks to Delta or
a workspace -- it is pure in-process bookkeeping, so it costs nothing while
idle (CLAUDE.md §11 cost incident).

Bounded and self-pruning (no leak): at most _MAX_ENTRIES tracked at once
(oldest evicted first), and any entry older than _ENTRY_TTL_S is pruned
opportunistically whenever the registry is touched. A registration that
succeeds is dropped immediately (the `runs` row is the source of truth from
then on); a failed one is kept, bounded, so a still-open /run/<id> tab can
show the reason.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

_MAX_ENTRIES = 200
_ENTRY_TTL_S = 3600  # generous enough for a failure to stay visible across a
# reload; small enough this can never grow unbounded over a long-running App
# process.

_lock = threading.Lock()
# run_id -> {"status": "pending" | "failed", "error": str | None,
#            "created_at": float, "dedupe_key": str | None}
_entries: "OrderedDict[str, dict]" = OrderedDict()
# dedupe_key -> run_id, present only while that run_id's own entry is still "pending".
_dedupe_index: dict[str, str] = {}

# A dedicated pool, deliberately NOT orchestrator.executor.ThreadExecutor
# (CLAUDE.md §2.1/§2.3): this pool only ever runs the one Delta-writing call
# that creates the `runs` row; the pipeline itself starts afterwards, inside
# THAT call, on the real executor.
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="run-start")


def _drop_locked(run_id: str) -> None:
    entry = _entries.pop(run_id, None)
    if entry and entry.get("dedupe_key") is not None and _dedupe_index.get(entry["dedupe_key"]) == run_id:
        del _dedupe_index[entry["dedupe_key"]]


def _prune_locked(now: float) -> None:
    stale = [rid for rid, e in _entries.items() if now - e["created_at"] > _ENTRY_TTL_S]
    for rid in stale:
        _drop_locked(rid)
    while len(_entries) > _MAX_ENTRIES:
        oldest_id = next(iter(_entries))
        _drop_locked(oldest_id)


def find_pending(dedupe_key: str) -> str | None:
    """A still-pending run_id already registered for this exact set of
    start parameters, if any. A genuine double-click / resubmit (the same
    auditor, the same form state, before the first submission's background
    write has finished) reuses that run_id instead of starting a second
    run."""
    with _lock:
        _prune_locked(time.time())
        return _dedupe_index.get(dedupe_key)


def start(run_id: str, dedupe_key: str | None, fn: Callable[[], None]) -> None:
    """Registers `run_id` as pending, then runs `fn` -- a zero-argument
    closure wrapping the real adapters.start_audit_run(..., run_id=run_id,
    ...) call -- on this module's own bounded pool. `fn` must not raise
    anything the caller needs back synchronously: any exception it raises
    is caught here, recorded against `run_id` (NN14 -- never swallowed
    silently) and left for /run/<run_id> to show."""
    now = time.time()
    with _lock:
        _prune_locked(now)
        _entries[run_id] = {"status": "pending", "error": None, "created_at": now, "dedupe_key": dedupe_key}
        _entries.move_to_end(run_id)
        if dedupe_key is not None:
            _dedupe_index[dedupe_key] = run_id

    def _run() -> None:
        try:
            fn()
        except Exception as exc:  # NN14: recorded, never swallowed
            with _lock:
                entry = _entries.get(run_id)
                if entry is not None:
                    entry["status"] = "failed"
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                if dedupe_key is not None and _dedupe_index.get(dedupe_key) == run_id:
                    del _dedupe_index[dedupe_key]
        else:
            with _lock:
                # The `runs` row now exists -- it is the source of truth
                # from here on, so this pending entry is no longer needed.
                _drop_locked(run_id)

    _pool.submit(_run)


def status(run_id: str) -> tuple[str, str | None] | None:
    """`("pending", None)` | `("failed", error_message)` | `None` (no
    entry -- never registered, already completed and dropped, or pruned)."""
    with _lock:
        entry = _entries.get(run_id)
        if entry is None:
            return None
        return entry["status"], entry.get("error")


def _reset_for_tests() -> None:
    """Test-only: clears all registry state between tests. Never called
    from app code -- each test otherwise inherits whatever a prior test's
    background jobs left behind in this process-wide registry."""
    with _lock:
        _entries.clear()
        _dedupe_index.clear()


def _wait_until_settled(run_id: str, timeout: float = 5.0) -> tuple[str, str | None] | None:
    """Test-only: blocks on this in-process registry (never Delta) until
    `run_id`'s background job has finished -- its entry is gone (succeeded)
    or "failed" -- or `timeout` elapses. Lets tests assert on the outcome of
    a submitted job deterministically instead of a fixed sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = status(run_id)
        if s is None or s[0] != "pending":
            return s
        time.sleep(0.01)
    return status(run_id)
