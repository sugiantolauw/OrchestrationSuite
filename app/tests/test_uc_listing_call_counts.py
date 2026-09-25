"""P3/P4 perf gap review 2026-09-25 (live pass): guards against a page
render triggering more than one real Unity Catalog listing round trip, and
against orchestrator.service.list_data_asset_cards' new concurrent
row-count/classification fan-out (the /home fix for the ~40s cold /
~8s warm live measurement, dominated by 4 governed-table cards' row-count
and classification lookups running one after another) firing a table's
lookups more than once or losing/misattributing a result.

A fake `data_source_factory` stands in for UCTableDataSource -- this is a
service-level test, not a live one, so nothing here talks to a real
workspace; the counters below are the same shape a real WorkspaceClient
call count would be."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from orchestrator import service
from orchestrator.config import Settings
from orchestrator.adapters.persistence_local import LocalPersistence
from orchestrator.timeutil import utc_now

_REPO_ROOT = Path(__file__).resolve().parents[2]

_FAKE_TABLES = [
    {"fqn": "cat.sch.expense_report", "catalog": "cat", "schema": "sch", "table": "expense_report",
     "comment": "T&E claims", "columns": ["Employee ID"]},
    {"fqn": "cat.sch.booking_detail", "catalog": "cat", "schema": "sch", "table": "booking_detail",
     "comment": "Bookings", "columns": ["Booking Type"]},
    {"fqn": "cat.sch.attendee_validity", "catalog": "cat", "schema": "sch", "table": "attendee_validity",
     "comment": "Attendees", "columns": ["Employee ID"]},
    {"fqn": "cat.sch.approval_aging", "catalog": "cat", "schema": "sch", "table": "approval_aging",
     "comment": "Approvals", "columns": ["Employee ID"]},
]


class _FakeUCSource:
    """One shared instance per `list_data_asset_cards` call (the same
    lifetime `ctx.data_source_factory({})` gives a real UCTableDataSource) --
    `calls` records every method invocation, with a lock since the fix under
    test calls `get_row_count`/`get_classification` from several threads."""

    def __init__(self, calls: list, lock: threading.Lock):
        self._calls = calls
        self._lock = lock

    def list_tables(self):
        with self._lock:
            self._calls.append(("list_tables", None))
        return [dict(t) for t in _FAKE_TABLES]

    def get_row_count(self, fqn: str) -> int:
        with self._lock:
            self._calls.append(("get_row_count", fqn))
        return {"cat.sch.expense_report": 1000, "cat.sch.booking_detail": 200,
                "cat.sch.attendee_validity": 50, "cat.sch.approval_aging": 30}[fqn]

    def get_classification(self, fqn: str):
        with self._lock:
            self._calls.append(("get_classification", fqn))
        return None

    def close(self) -> None:
        pass


@pytest.fixture
def uc_ctx(tmp_path):
    calls: list = []
    lock = threading.Lock()
    source = _FakeUCSource(calls, lock)

    persistence = LocalPersistence(str(tmp_path / "orch.db"))
    persistence.migrate()

    ctx = service.AppContext(
        settings=Settings(max_connections=6),
        persistence=persistence,
        skills_dir=_REPO_ROOT / "skills",
        data_source_factory=lambda bindings, *a, **kw: source,
        export_storage=None,
        clock=utc_now,
        backend="uc",
    )
    return ctx, calls


def test_list_data_asset_cards_lists_the_catalog_exactly_once(uc_ctx):
    ctx, calls = uc_ctx

    cards = service.list_data_asset_cards(ctx, "", limit=4)

    listing_calls = [c for c in calls if c[0] == "list_tables"]
    assert len(listing_calls) == 1, f"expected exactly one list_tables() call, got {listing_calls}"
    assert len(cards) == 4


def test_list_data_asset_cards_looks_up_each_table_exactly_once(uc_ctx):
    """The row-count/classification fan-out (P3/P4 perf gap review
    2026-09-25) runs one table's lookups per thread, concurrently -- proves
    that parallelising it never turns into a double lookup for any one
    table, and every card still gets the right table's own row count back
    (not another table's, e.g. from a shared/reused variable across
    threads)."""
    ctx, calls = uc_ctx

    cards = service.list_data_asset_cards(ctx, "", limit=4)

    row_count_calls = [c[1] for c in calls if c[0] == "get_row_count"]
    classification_calls = [c[1] for c in calls if c[0] == "get_classification"]
    assert sorted(row_count_calls) == sorted(t["fqn"] for t in _FAKE_TABLES)
    assert sorted(classification_calls) == sorted(t["fqn"] for t in _FAKE_TABLES)

    by_name = {c["name"]: c for c in cards}
    assert by_name["cat.sch.expense_report"]["rows"] == 1000
    assert by_name["cat.sch.booking_detail"]["rows"] == 200
    assert by_name["cat.sch.attendee_validity"]["rows"] == 50
    assert by_name["cat.sch.approval_aging"]["rows"] == 30


def test_list_data_asset_cards_with_no_limit_never_looks_up_rows(uc_ctx):
    """CLAUDE.md §5 UI item 3: row count/classification are real per-table
    queries, only ever fetched for the cards a caller is actually about to
    render -- omitting `limit` (a caller filtering further itself) must
    trigger neither."""
    ctx, calls = uc_ctx

    service.list_data_asset_cards(ctx, "")

    assert not [c for c in calls if c[0] in ("get_row_count", "get_classification")]
