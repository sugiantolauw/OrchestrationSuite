"""scripts.check_idle_cost's logic, exercised purely against fakes --
CLAUDE.md operating instructions: never run this against the live workspace
in this session. See scripts/check_idle_cost.py's own docstring."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.check_idle_cost import check_idle_cost, count_recent_queries


class _FakeSqlClient:
    def __init__(self, rows):
        self._rows = rows
        self.statements: list[str] = []

    def execute(self, statement: str) -> list[dict]:
        self.statements.append(statement)
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
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_queries=5,
    )
    assert report.ok
    assert "OK" in report.describe()


def test_a_few_queries_within_threshold_is_still_ok():
    sql_client = _FakeSqlClient([{"n": 3}])
    ws_client = _FakeWorkspaceClient("STOPPED")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_queries=5,
    )
    assert report.ok


def test_regression_shaped_query_volume_fails():
    # The 2026-09-23 incident: ~3,000 queries/hour while idle.
    sql_client = _FakeSqlClient([{"n": 1500}])
    ws_client = _FakeWorkspaceClient("RUNNING")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_queries=5,
    )
    assert not report.ok
    assert "FAIL" in report.describe()


def test_running_warehouse_fails_even_with_zero_queries():
    # A warehouse that never stops (e.g. auto_stop misconfigured) is its own
    # cost problem, independent of query volume.
    sql_client = _FakeSqlClient([{"n": 0}])
    ws_client = _FakeWorkspaceClient("RUNNING")
    report = check_idle_cost(
        sql_client, ws_client, client_id="sp-1", warehouse_id="wh-1", minutes=30, max_queries=5,
    )
    assert not report.ok


def test_empty_result_set_raises_rather_than_silently_passing():
    sql_client = _FakeSqlClient([])
    with pytest.raises(RuntimeError):
        count_recent_queries(sql_client, client_id="sp-1", since=datetime.now(timezone.utc))


def test_query_scoped_to_the_apps_service_principal_and_the_window():
    sql_client = _FakeSqlClient([{"n": 0}])
    ws_client = _FakeWorkspaceClient("STOPPED")
    check_idle_cost(sql_client, ws_client, client_id="sp-xyz", warehouse_id="wh-1", minutes=45, max_queries=5)
    assert len(sql_client.statements) == 1
    assert "sp-xyz" in sql_client.statements[0]
    assert "system.query.history" in sql_client.statements[0]
