"""Unit tests for src.pending_runs -- the in-process registry + bounded
worker pool behind CLAUDE.md §11 "Run start opens the run page at once"
(2026-09-25). app/tests/conftest.py's autouse clean_pending_runs fixture
resets this module's process-wide state before and after every test."""

from __future__ import annotations

import threading
import time

from src import pending_runs


def test_start_registers_pending_then_settles_to_gone_on_success():
    started = threading.Event()
    release = threading.Event()

    def fn():
        started.set()
        release.wait(timeout=5)

    pending_runs.start("RUN-AAA111111111", "key-1", fn)
    started.wait(timeout=5)
    assert pending_runs.status("RUN-AAA111111111") == ("pending", None)

    release.set()
    settled = pending_runs._wait_until_settled("RUN-AAA111111111")
    # Success drops the entry entirely -- the `runs` row is the source of
    # truth from then on (no leak: nothing left to prune later).
    assert settled is None
    assert pending_runs.status("RUN-AAA111111111") is None


def test_start_records_a_failure_and_keeps_it_until_pruned():
    def fn():
        raise ValueError("boom -- contract violation")

    pending_runs.start("RUN-BBB222222222", "key-2", fn)
    settled = pending_runs._wait_until_settled("RUN-BBB222222222")
    assert settled == ("failed", "ValueError: boom -- contract violation")
    # Still there on a second read -- a still-open /run/<id> tab must keep
    # seeing the same failure across repeated polls, not just once.
    assert pending_runs.status("RUN-BBB222222222") == ("failed", "ValueError: boom -- contract violation")


def test_find_pending_dedupes_a_still_pending_submission():
    release = threading.Event()

    def fn():
        release.wait(timeout=5)

    pending_runs.start("RUN-CCC333333333", "same-key", fn)
    assert pending_runs.find_pending("same-key") == "RUN-CCC333333333"
    release.set()
    pending_runs._wait_until_settled("RUN-CCC333333333")


def test_find_pending_is_cleared_once_the_job_settles():
    def fn():
        pass

    pending_runs.start("RUN-DDD444444444", "clears-key", fn)
    pending_runs._wait_until_settled("RUN-DDD444444444")
    # The job succeeded and its entry is gone -- a later click with the
    # same parameters must be free to start a genuinely new run, not be
    # stuck deduping against a submission that already finished.
    assert pending_runs.find_pending("clears-key") is None


def test_find_pending_is_cleared_after_a_failure_too():
    def fn():
        raise RuntimeError("nope")

    pending_runs.start("RUN-EEE555555555", "fails-key", fn)
    pending_runs._wait_until_settled("RUN-EEE555555555")
    # The pending entry itself is retained (previous test) but the dedupe
    # index is not -- a retry after a failure must be able to start fresh.
    assert pending_runs.find_pending("fails-key") is None


def test_registry_is_bounded_and_evicts_oldest_first():
    for i in range(pending_runs._MAX_ENTRIES + 5):
        run_id = f"RUN-BOUND{i:08d}"
        with pending_runs._lock:
            pending_runs._entries[run_id] = {
                "status": "failed", "error": "x", "created_at": time.time(), "dedupe_key": None,
            }
    # Registering one more entry triggers pruning back down to the bound.
    def fn():
        pass

    pending_runs.start("RUN-BOUND-NEW0001", "bound-key", fn)
    pending_runs._wait_until_settled("RUN-BOUND-NEW0001")
    with pending_runs._lock:
        assert len(pending_runs._entries) <= pending_runs._MAX_ENTRIES
    # The very first ones inserted (oldest) are the ones evicted.
    assert pending_runs.status("RUN-BOUND00000000") is None


def test_registry_prunes_entries_older_than_the_ttl():
    stale_time = time.time() - pending_runs._ENTRY_TTL_S - 10
    with pending_runs._lock:
        pending_runs._entries["RUN-STALE00000001"] = {
            "status": "failed", "error": "old", "created_at": stale_time, "dedupe_key": "stale-key",
        }
        pending_runs._dedupe_index["stale-key"] = "RUN-STALE00000001"

    assert pending_runs.status("RUN-STALE00000001") is not None  # still there before a touch

    def fn():
        pass

    # Any touch of the registry opportunistically prunes stale entries.
    pending_runs.start("RUN-FRESH00000001", "fresh-key", fn)
    pending_runs._wait_until_settled("RUN-FRESH00000001")
    assert pending_runs.status("RUN-STALE00000001") is None
    assert pending_runs.find_pending("stale-key") is None
