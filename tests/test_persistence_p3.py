"""P3 run-output persistence methods (CLAUDE.md build brief P3 §1, §4),
against every persistence backend (local_memory, local_file, delta) via the
`persistence` fixture in conftest.py -- same contract as
tests/test_persistence_contract.py and tests/test_persistence_p2.py, but for
the tables those files don't cover: flagged_rows, run_metrics,
management_actions, issues (write_issues_for_findings) and run_leases. Live
against Delta with `RUN_DELTA_TESTS=1` (see conftest.py's `delta_schema`).

Each write_* here is exercised for the MERGE+prune upsert behaviour
(CLAUDE.md build brief P3 §1): a second write for the same run_id with a
changed key set updates matches, inserts new keys and prunes dropped ones --
never a DELETE-then-INSERT window where the run's rows are momentarily
absent (not directly observable from a single-threaded test, but the
row-level upsert/prune outcome is)."""

from __future__ import annotations

import numpy as np

from tests.conftest import canonical_ts

# ── flagged_rows ──────────────────────────────────────────────────────────────


def test_write_flagged_rows_upserts_and_prunes(persistence, uid):
    run_id = f"RUN-FLAGS-{uid}"
    persistence.write_flagged_rows(
        run_id,
        [
            {"source": "expense_report", "row_key": "k:1", "flag": "RF_A", "group_id": None},
            {"source": "expense_report", "row_key": "k:2", "flag": "RF_A", "group_id": "g:1"},
        ],
    )
    rows = persistence.list_flagged_rows(run_id)
    assert {(r["row_key"], r["flag"], r["group_id"]) for r in rows} == {
        ("k:1", "RF_A", None), ("k:2", "RF_A", "g:1"),
    }

    # Second write: k:1 updates (gains a group_id), k:2 is pruned (no longer
    # present), k:3 is new -- MERGE + prune, not delete-then-insert.
    persistence.write_flagged_rows(
        run_id,
        [
            {"source": "expense_report", "row_key": "k:1", "flag": "RF_A", "group_id": "g:2"},
            {"source": "expense_report", "row_key": "k:3", "flag": "RF_B", "group_id": None},
        ],
    )
    rows = persistence.list_flagged_rows(run_id)
    assert {(r["row_key"], r["flag"], r["group_id"]) for r in rows} == {
        ("k:1", "RF_A", "g:2"), ("k:3", "RF_B", None),
    }

    filtered = persistence.list_flagged_rows(run_id, flag="RF_B")
    assert [r["row_key"] for r in filtered] == ["k:3"]

    # Writing an empty list clears every row for this run_id.
    persistence.write_flagged_rows(run_id, [])
    assert persistence.list_flagged_rows(run_id) == []


# ── run_metrics ───────────────────────────────────────────────────────────────


def test_write_run_metrics_upserts_and_prunes(persistence, uid):
    run_id = f"RUN-METRICS-{uid}"
    persistence.write_run_metrics(
        run_id,
        [
            {"metric_name": "hv_count", "value": 3, "unit": "count", "source_ref": {"sources": []}, "test_id": "T1"},
            {"metric_name": "hv_amount", "value": 1500.0, "unit": "AUD", "source_ref": {"sources": []}, "test_id": "T1"},
        ],
    )
    metrics = persistence.get_run_metrics(run_id)
    assert metrics["hv_count"]["value"] == 3
    assert metrics["hv_amount"]["value"] == 1500.0

    persistence.write_run_metrics(
        run_id,
        [{"metric_name": "hv_count", "value": 5, "unit": "count", "source_ref": {"sources": []}, "test_id": "T1"}],
    )
    metrics = persistence.get_run_metrics(run_id)
    assert metrics["hv_count"]["value"] == 5
    assert "hv_amount" not in metrics  # pruned, not merely left stale


def test_write_run_metrics_amounts_round_trip_exactly_no_float32_loss(persistence, uid):
    """CLAUDE.md P2/P3 gate review item 1: live Delta values were observed equal
    to float(np.float32(x)) for monetary metrics -- e.g. 467063.71875 instead of
    467063.73. Every value here is a real double that is NOT exactly representable
    as float32, so any float32 rounding on the write path (implicit driver
    inference, a FLOAT-typed bind, or a stray numpy float32 upstream) would make
    at least one of these fail `== ` on read-back. Runs against every backend via
    the `persistence` fixture; against `delta` it is the live proof (RUN_DELTA_TESTS=1)
    that persistence_delta.py's explicit DoubleParameter binding (_execute_typed)
    round-trips bit-exactly through the real SQL warehouse, not a synthetic probe."""
    run_id = f"RUN-PRECISION-{uid}"
    cases = {
        "claims_approved_amount": 467063.73,
        "float_add_precision": 0.1 + 0.2,
        "tiny_amount": 1e-7,
        "large_amount": 123456789.01,
    }
    persistence.write_run_metrics(
        run_id,
        [
            {"metric_name": name, "value": value, "unit": "AUD", "source_ref": {"sources": []}, "test_id": "T1"}
            for name, value in cases.items()
        ],
    )
    metrics = persistence.get_run_metrics(run_id)
    for name, expected in cases.items():
        got = metrics[name]["value"]
        float32_rounded = float(np.float32(expected))
        assert expected != float32_rounded, f"{name}: fixture value must not already be float32-exact"
        assert got == expected, f"{name}: wrote {expected!r}, read back {got!r} (float32 precision loss)"


# ── management_actions ───────────────────────────────────────────────────────


def test_write_management_actions_upserts_and_prunes(persistence, uid):
    run_id = f"RUN-ACTIONS-{uid}"
    now = canonical_ts(1)
    persistence.write_management_actions(
        run_id,
        [
            {"action_id": f"MA-{uid}-1", "finding_id": f"{run_id}:T1", "title": "Fix A", "status": "draft", "risk": "High"},
            {"action_id": f"MA-{uid}-2", "finding_id": f"{run_id}:T2", "title": "Fix B", "status": "draft", "risk": "Low"},
        ],
        now=now,
    )
    actions = persistence.list_management_actions(filters={"run_id": run_id})
    assert {a["action_id"] for a in actions} == {f"MA-{uid}-1", f"MA-{uid}-2"}

    later = canonical_ts(2)
    persistence.write_management_actions(
        run_id,
        [{"action_id": f"MA-{uid}-1", "finding_id": f"{run_id}:T1", "title": "Fix A (updated)", "status": "in_progress", "risk": "High"}],
        now=later,
    )
    actions = persistence.list_management_actions(filters={"run_id": run_id})
    assert {a["action_id"] for a in actions} == {f"MA-{uid}-1"}
    assert actions[0]["title"] == "Fix A (updated)"
    assert actions[0]["status"] == "in_progress"


# ── issues ────────────────────────────────────────────────────────────────────


def test_write_issues_for_findings_creates_one_per_finding_and_is_idempotent(persistence, uid):
    run_id = f"RUN-ISSUES-{uid}"
    finding = {
        "finding_id": f"{run_id}:T1",
        "rule_id": "SKILL-001.T1",
        "title": "Missing receipts",
        "observation": "Some claims are missing receipts.",
        "severity": "High",
    }
    created = persistence.write_issues_for_findings(run_id, [finding], engagement_id="ENG-DEFAULT", now=canonical_ts(1))
    assert created == [{"issue_id": f"ISS-{run_id}:T1", "finding_id": f"{run_id}:T1"}]

    # A second call for the SAME finding is a no-op (never a duplicate issue) --
    # idempotent per CLAUDE.md §2.3 rule 1.
    created_again = persistence.write_issues_for_findings(run_id, [finding], engagement_id="ENG-DEFAULT", now=canonical_ts(2))
    assert created_again == []


# ── run_leases ────────────────────────────────────────────────────────────────


def test_lease_acquire_renew_release_and_expiry(persistence, uid):
    run_id = f"RUN-LEASE-{uid}"
    t0 = "2026-01-01T00:00:00.000000Z"
    assert persistence.acquire_lease(run_id, "worker-a", ttl_s=60, now=t0) is True
    # A second worker cannot acquire a live lease.
    assert persistence.acquire_lease(run_id, "worker-b", ttl_s=60, now=t0) is False

    t1 = "2026-01-01T00:00:30.000000Z"
    assert persistence.renew_lease(run_id, "worker-a", ttl_s=60, now=t1) is True
    # Renewing as the wrong worker is a no-op (no rows affected).
    assert persistence.renew_lease(run_id, "worker-b", ttl_s=60, now=t1) is False

    far_future = "9999-12-31T23:59:59.999999Z"
    assert run_id not in persistence.expired_leases(t1)
    assert run_id in persistence.expired_leases(far_future)

    persistence.release_lease(run_id, "worker-a")
    assert run_id not in persistence.expired_leases(far_future)

    # After release, a different worker can acquire it fresh.
    assert persistence.acquire_lease(run_id, "worker-b", ttl_s=60, now=t1) is True


def test_lease_taken_over_once_expired(persistence, uid):
    run_id = f"RUN-LEASE-EXPIRE-{uid}"
    t0 = "2026-01-01T00:00:00.000000Z"
    assert persistence.acquire_lease(run_id, "worker-a", ttl_s=1, now=t0) is True

    well_past = "2026-01-01T00:05:00.000000Z"
    assert persistence.acquire_lease(run_id, "worker-b", ttl_s=60, now=well_past) is True
    assert persistence.acquire_lease(run_id, "worker-a", ttl_s=60, now=well_past) is False


def test_lease_acquire_concurrent_on_a_fresh_run_id_only_one_wins(persistence, uid):
    """Item 6 (CLAUDE.md P2/P3 gate review): acquire_lease's old
    read-then-insert was non-atomic. On Delta specifically, which has no
    ENFORCED primary key, two workers racing to acquire a lease for a run_id
    with NO existing row could both see "no row" and both INSERT, leaving
    two owners of the same run -- exactly the failure mode a lease exists to
    prevent. Fired genuinely concurrently (a barrier, not sequential calls)
    against every backend `persistence` covers, including real Delta under
    RUN_DELTA_TESTS=1 (conftest.py's delta_schema, a throwaway schema)."""
    import threading

    run_id = f"RUN-LEASE-RACE-{uid}"
    now = "2026-01-01T00:00:00.000000Z"
    n_workers = 6
    barrier = threading.Barrier(n_workers)
    results: list[bool] = []
    lock = threading.Lock()

    def attempt(worker_id: str):
        barrier.wait()
        won = persistence.acquire_lease(run_id, worker_id, ttl_s=60, now=now)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=attempt, args=(f"worker-{i}",)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1, f"expected exactly one winner, got {results}"


# ── exports ───────────────────────────────────────────────────────────────────


def test_record_and_list_exports(persistence, uid):
    run_id = f"RUN-EXPORT-{uid}"
    now = canonical_ts(1)
    persistence.record_export(run_id, "xlsx", path=f"exports/{run_id}/workpaper.xlsx", sha256="a" * 64, created_by="tester", now=now)
    exports = persistence.list_exports(run_id)
    assert len(exports) == 1
    assert exports[0]["kind"] == "xlsx"
    assert exports[0]["sha256"] == "a" * 64

    # Re-recording the same kind (a re-executed export node) replaces it --
    # exports_pk is (run_id, kind), the same idempotent-per-run-id contract
    # as every other P3 write here.
    persistence.record_export(run_id, "xlsx", path=f"exports/{run_id}/workpaper-v2.xlsx", sha256="b" * 64, created_by="tester", now=canonical_ts(2))
    exports = persistence.list_exports(run_id)
    assert len(exports) == 1
    assert exports[0]["sha256"] == "b" * 64
