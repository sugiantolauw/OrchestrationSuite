"""scripts.check_idle_cost's logic, exercised purely against fakes --
CLAUDE.md operating instructions: never run this against the live workspace
in this session. See scripts/check_idle_cost.py's own docstring.

The corporate-workspace assessment found `system.query.history` unavailable
there, so this script (and these tests) use `system.compute.warehouse_events`
instead, with graceful "not available" handling when even that table cannot
be read (independent review item 2)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.check_idle_cost import (
    build_parser,
    check_idle_cost,
    count_warehouse_start_events,
    main,
)


class _FakeSqlClient:
    def __init__(self, rows=None, *, raises: Exception | None = None):
        self._rows = rows
        self._raises = raises
        self.statements: list[str] = []

    def execute(self, statement: str) -> list[dict]:
        self.statements.append(statement)
        if self._raises is not None:
            raise self._raises
        return self._rows


class _FakeWarehouse:
    def __init__(self, state: str):
        self.state = state


class _FakeWarehouses:
    def __init__(self, state: str):
        self._state = state

    def get(self, warehouse_id: str):
        return _FakeWarehouse(self._state)


class _FakeWorkspaceClient:
    def __init__(self, state: str):
        self.warehouses = _FakeWarehouses(state)


def test_idle_and_stopped_is_ok():
    sql_client = _FakeSqlClient([{"n": 0}])
    ws_client = _FakeWorkspaceClient("STOPPED")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_start_events=2,
    )
    assert report.ok
    assert "OK" in report.describe()


def test_a_few_start_events_within_threshold_is_still_ok():
    sql_client = _FakeSqlClient([{"n": 2}])
    ws_client = _FakeWorkspaceClient("STOPPED")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_start_events=2,
    )
    assert report.ok


def test_regression_shaped_start_event_volume_fails():
    sql_client = _FakeSqlClient([{"n": 50}])
    ws_client = _FakeWorkspaceClient("RUNNING")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_start_events=2,
    )
    assert not report.ok
    assert "FAIL" in report.describe()


def test_running_warehouse_fails_even_with_zero_start_events():
    # A warehouse that never stops (e.g. auto_stop misconfigured) is its own
    # cost problem, independent of start-event volume.
    sql_client = _FakeSqlClient([{"n": 0}])
    ws_client = _FakeWorkspaceClient("RUNNING")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_start_events=2,
    )
    assert not report.ok


def test_empty_result_set_raises_rather_than_silently_passing():
    sql_client = _FakeSqlClient([])
    with pytest.raises(RuntimeError):
        count_warehouse_start_events(sql_client, warehouse_id="wh-1", since=datetime.now(timezone.utc))


def test_query_scoped_to_the_warehouse_and_the_window():
    sql_client = _FakeSqlClient([{"n": 0}])
    ws_client = _FakeWorkspaceClient("STOPPED")
    check_idle_cost(sql_client, ws_client, client_id="sp-xyz", warehouse_id="wh-1", minutes=45, max_start_events=2)
    assert len(sql_client.statements) == 1
    assert "wh-1" in sql_client.statements[0]
    assert "system.compute.warehouse_events" in sql_client.statements[0]
    assert "STARTING" in sql_client.statements[0]


def test_permission_denied_reports_not_available_and_does_not_fail_alone():
    sql_client = _FakeSqlClient(raises=RuntimeError("PERMISSION_DENIED: user cannot read system.compute.warehouse_events"))
    ws_client = _FakeWorkspaceClient("STOPPED")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_start_events=2,
    )
    assert report.start_event_count is None
    assert "PERMISSION_DENIED" in report.start_events_unavailable_reason
    assert report.ok  # warehouse is STOPPED -- unavailable events table alone must not fail the check
    assert "NOT AVAILABLE" in report.describe()


def test_table_not_found_reports_not_available():
    sql_client = _FakeSqlClient(raises=RuntimeError("TABLE_OR_VIEW_NOT_FOUND: system.compute.warehouse_events"))
    count, reason = count_warehouse_start_events(sql_client, warehouse_id="wh-1", since=datetime.now(timezone.utc))
    assert count is None
    assert "TABLE_OR_VIEW_NOT_FOUND" in reason


def test_unavailable_events_table_plus_running_warehouse_still_fails():
    # Not-available system data must not mask a real problem (the warehouse
    # actually being awake) -- CLAUDE.md NN13.
    sql_client = _FakeSqlClient(raises=RuntimeError("PERMISSION_DENIED"))
    ws_client = _FakeWorkspaceClient("RUNNING")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_start_events=2,
    )
    assert not report.ok


def test_unrelated_sql_error_is_not_swallowed():
    sql_client = _FakeSqlClient(raises=ValueError("something else entirely broke"))
    with pytest.raises(ValueError):
        count_warehouse_start_events(sql_client, warehouse_id="wh-1", since=datetime.now(timezone.utc))


def test_legacy_max_queries_kwarg_still_works():
    sql_client = _FakeSqlClient([{"n": 10}])
    ws_client = _FakeWorkspaceClient("STOPPED")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_queries=5,
    )
    assert report.max_allowed_start_events == 5
    assert not report.ok


def test_main_exits_2_without_warehouse_id(monkeypatch):
    monkeypatch.delenv("DBX_WAREHOUSE_ID", raising=False)
    assert main([]) == 2


def test_build_parser_defaults():
    args = build_parser().parse_args([])
    assert args.minutes == 30
    assert args.max_start_events == 2
    assert args.max_queries is None
