from __future__ import annotations

import pytest

from orchestrator.adapters.persistence_delta import DeltaPersistence
from orchestrator.config import Settings
from orchestrator.errors import ConfigError, RunAlreadyExists, StaleStateError
from orchestrator.state import RunState


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self._rows = []
        self._pos = 0

    def execute(self, sql_text, params=None):
        self.conn.calls.append((sql_text, dict(params or {})))
        handler = self.conn.handlers.get(self._match(sql_text))
        if handler is not None:
            cols, rows = handler(sql_text, params or {})
            self.description = [(c,) for c in cols]
            self._rows = list(rows)
        else:
            self.description = []
            self._rows = []
        self._pos = 0
        return self

    def _match(self, sql_text):
        for key in self.conn.handlers:
            if key in sql_text:
                return key
        return None

    def fetchone(self):
        if self._pos < len(self._rows):
            row = self._rows[self._pos]
            self._pos += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


def _settings():
    return Settings(catalog="cat1", schema="sch1", warehouse_http_path="/sql/1", host="https://x.cloud.databricks.com")


def _state(**overrides):
    base = dict(
        run_id="RUN-1", run_kind="fieldwork", mode="playbook", phase="plan",
        audit_period=("2026-01-01", "2026-01-31"), objective="t", run_owner="alice",
        fingerprint_id="FP1", created_at="t0", last_state_change_at="t0",
        status="running", state_version=1, engagement_id="E1",
    )
    base.update(overrides)
    return RunState(**base)


def test_construction_requires_settings():
    with pytest.raises(ConfigError):
        DeltaPersistence(Settings())


def test_identifier_validation_rejects_injection_in_catalog():
    with pytest.raises(ConfigError):
        Settings(catalog="x; DROP TABLE foo;--", schema="sch1", warehouse_http_path="/sql/1", host="https://x")


def test_save_state_uses_named_parameters_and_cas_where_clause():
    handlers = {
        "UPDATE cat1.sch1.run_state": lambda sql_text, params: (["num_affected_rows"], [(1,)]),
        "UPDATE cat1.sch1.runs": lambda sql_text, params: ([], []),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    result = p.save_state(_state(state_version=1))
    assert result.state_version == 2

    update_calls = [c for c in conn.calls if c[0].startswith("UPDATE cat1.sch1.run_state")]
    assert len(update_calls) == 1
    sql_text, params = update_calls[0]
    assert "WHERE run_id = :run_id AND state_version = :expected" in sql_text
    assert params["expected"] == 1
    assert params["new_version"] == 2
    assert params["run_id"] == "RUN-1"
    # no raw value interpolation: catalog/schema appear only as validated identifiers in the
    # table name, all row values travel as named parameters.
    assert ":run_id" in sql_text and ":state_json" in sql_text


def test_save_state_zero_affected_rows_raises_stale_state_error():
    handlers = {
        "UPDATE cat1.sch1.run_state": lambda sql_text, params: (["num_affected_rows"], [(0,)]),
        "SELECT state_version FROM cat1.sch1.run_state": lambda sql_text, params: (["state_version"], [(9,)]),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    with pytest.raises(StaleStateError) as exc:
        p.save_state(_state(state_version=1))
    assert exc.value.expected_version == 1
    assert exc.value.actual_version == 9


@pytest.mark.parametrize(
    "marker",
    ["ConcurrentAppend", "ConcurrentDelete", "ConcurrentTransaction", "DELTA_CONCURRENT_APPEND"],
)
def test_concurrency_exceptions_mapped_to_stale_state_error(marker):
    class ThrowingConn:
        def __init__(self):
            self.calls = []

        def cursor(self):
            return ThrowingCursor(self)

        def close(self):
            pass

    class ThrowingCursor:
        def __init__(self, conn):
            self.conn = conn
            self.description = [("state_version",)]

        def execute(self, sql_text, params=None):
            self.conn.calls.append((sql_text, params))
            if sql_text.startswith("UPDATE cat1.sch1.run_state"):
                raise Exception(f"Query failed: [{marker}] concurrent modification detected")
            return self

        def fetchone(self):
            return (11,)

        def fetchall(self):
            return []

        def close(self):
            pass

    conn = ThrowingConn()
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    with pytest.raises(StaleStateError) as exc:
        p.save_state(_state(state_version=1))
    assert exc.value.actual_version == 11


def test_non_concurrency_exception_propagates():
    class ThrowingConn:
        def cursor(self):
            return self

        def execute(self, sql_text, params=None):
            raise Exception("some other unrelated warehouse error")

        def close(self):
            pass

    p = DeltaPersistence(_settings(), connection_factory=lambda: ThrowingConn())
    with pytest.raises(Exception, match="unrelated warehouse error"):
        p.save_state(_state(state_version=1))


def test_create_run_checks_existing_before_insert():
    handlers = {
        "SELECT run_id FROM cat1.sch1.runs": lambda sql_text, params: (["run_id"], [("RUN-1",)]),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    with pytest.raises(RunAlreadyExists):
        p.create_run(_state(status="queued", state_version=0), {"fingerprint_id": "FP1"})


def test_list_runs_builds_named_parameter_filters():
    handlers = {
        "SELECT * FROM cat1.sch1.runs": lambda sql_text, params: (["run_id", "status"], [("RUN-1", "queued")]),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    rows = p.list_runs({"status": "queued"})
    assert rows == [{"run_id": "RUN-1", "status": "queued"}]
    sql_text, params = conn.calls[-1]
    assert "status = :status" in sql_text
    assert params["status"] == "queued"


def test_find_runs_uses_named_placeholders_for_each_status():
    handlers = {
        "SELECT run_id FROM cat1.sch1.runs": lambda sql_text, params: (["run_id"], [("RUN-1",), ("RUN-2",)]),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    result = p.find_runs(["running", "queued"])
    assert result == ["RUN-1", "RUN-2"]
    sql_text, params = conn.calls[-1]
    assert ":s0" in sql_text and ":s1" in sql_text
    assert params == {"s0": "running", "s1": "queued"}


def test_begin_node_attempt_uses_merge_and_named_params():
    handlers = {
        "SELECT * FROM cat1.sch1.node_attempts WHERE run_id = :run_id AND node_name = :node_name AND outcome IS NULL": lambda s, p: ([], []),
        "SELECT COUNT(*)": lambda s, p: (["n"], [(0,)]),
        "MERGE INTO cat1.sch1.node_attempts": lambda s, p: ([], []),
        "SELECT * FROM cat1.sch1.node_attempts WHERE execution_key": lambda s, p: (
            ["attempt_id", "execution_key", "node_name", "attempt_number"],
            [("abc", "RUN-1:discover:1", "discover", 1)],
        ),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    attempt = p.begin_node_attempt(
        run_id="RUN-1", phase="plan", node_index=0, node_name="discover", state_version_before=1, now="t1"
    )
    assert attempt["execution_key"] == "RUN-1:discover:1"
    merge_calls = [c for c in conn.calls if c[0].startswith("MERGE INTO")]
    assert len(merge_calls) == 1
    assert "WHEN NOT MATCHED THEN INSERT" in merge_calls[0][0]
    assert merge_calls[0][1]["execution_key"] == "RUN-1:discover:1"
