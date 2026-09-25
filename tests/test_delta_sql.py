from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from orchestrator.adapters.persistence_delta import DeltaPersistence
from orchestrator.config import Settings
from orchestrator.errors import ConfigError, RunAlreadyExists, RunNotFound, StaleStateError
from orchestrator.state import RunState
from tests.conftest import canonical_ts

REPO_ROOT = Path(__file__).resolve().parent.parent
DELTA_DDL_DIR = REPO_ROOT / "orchestrator" / "ddl" / "delta"


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
        fingerprint_id="FP1", created_at=canonical_ts(0), last_state_change_at=canonical_ts(0),
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
    assert "status = :status" in sql_text
    assert params["expected"] == 1
    assert params["new_version"] == 2
    assert params["run_id"] == "RUN-1"
    assert params["status"] == "running"
    # no raw value interpolation: catalog/schema appear only as validated identifiers in the
    # table name, all row values travel as named parameters.
    assert ":run_id" in sql_text and ":state_json" in sql_text

    # the runs projection is updated separately, after run_state commits.
    projection_calls = [c for c in conn.calls if c[0].startswith("UPDATE cat1.sch1.runs")]
    assert len(projection_calls) == 1


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


def test_cas_retries_concurrency_error_when_version_unchanged_then_succeeds():
    # Simulates a spurious ConcurrentAppend where a re-read shows the stored version is
    # still `expected` (no one actually moved it) -- the CAS should retry rather than
    # give up immediately, per CLAUDE.md §9C non-blocking item.
    counters = {"update_attempts": 0}

    class Cur:
        def __init__(self):
            self.description = []
            self._rows = []

        def execute(self, sql_text, params=None):
            if sql_text.startswith("UPDATE cat1.sch1.run_state"):
                counters["update_attempts"] += 1
                if counters["update_attempts"] < 3:
                    raise Exception("Query failed: [ConcurrentAppend] concurrent modification detected")
                self.description = [("num_affected_rows",)]
                self._rows = [(1,)]
            elif sql_text.startswith("SELECT state_version FROM cat1.sch1.run_state"):
                self.description = [("state_version",)]
                self._rows = [(1,)]  # always reads back == expected -> warrants a retry
            else:
                self.description = []
                self._rows = []
            return self

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    p = DeltaPersistence(_settings(), connection_factory=lambda: Conn())
    result = p.save_state(_state(state_version=1))
    assert result.state_version == 2
    assert counters["update_attempts"] == 3


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


def test_create_run_checks_existing_via_run_state_merge():
    handlers = {
        "MERGE INTO cat1.sch1.run_state": lambda sql_text, params: (["num_inserted_rows"], [(0,)]),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    with pytest.raises(RunAlreadyExists):
        p.create_run(_state(status="queued", state_version=0), {"fingerprint_id": "FP1"})


def test_create_run_writes_run_state_before_fingerprint_and_runs():
    handlers = {
        "MERGE INTO cat1.sch1.run_state": lambda sql_text, params: (["num_inserted_rows"], [(1,)]),
        "SELECT * FROM cat1.sch1.run_fingerprints": lambda sql_text, params: ([], []),
        "INSERT INTO cat1.sch1.run_fingerprints": lambda sql_text, params: ([], []),
        "MERGE INTO cat1.sch1.runs": lambda sql_text, params: ([], []),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    fp = {
        "fingerprint_id": "FP1", "source_table_versions": "{}", "uploaded_file_hashes": "{}",
        "reference_data_hashes": "{}", "skill_content_hash": None, "code_revision": "rev1",
        "dependency_lock_hash": "dep1", "runtime_config_hash": "rc1", "endpoint_config": "{}",
        "prompt_template_version": "none", "created_at": canonical_ts(0),
    }
    result = p.create_run(_state(status="queued", state_version=0), fp)
    assert result.state_version == 1

    sql_calls = [c[0] for c in conn.calls]
    run_state_idx = next(i for i, s in enumerate(sql_calls) if "cat1.sch1.run_state" in s)
    fingerprint_idx = next(i for i, s in enumerate(sql_calls) if "cat1.sch1.run_fingerprints" in s)
    runs_idx = next(i for i, s in enumerate(sql_calls) if s.startswith("MERGE INTO cat1.sch1.runs"))
    assert run_state_idx < fingerprint_idx < runs_idx


def test_list_runs_builds_named_parameter_filters():
    handlers = {
        "SELECT r.*, rs.status AS rs_status FROM cat1.sch1.runs": lambda sql_text, params: (
            ["run_id", "rs_status"], [("RUN-1", "queued")],
        ),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    rows = p.list_runs({"status": "queued"})
    assert rows == [{"run_id": "RUN-1", "status": "queued"}]
    sql_text, params = conn.calls[-1]
    assert "rs.status = :status" in sql_text
    assert params["status"] == "queued"


def test_find_runs_reads_run_state_not_runs():
    handlers = {
        "SELECT run_id FROM cat1.sch1.run_state": lambda sql_text, params: (["run_id"], [("RUN-1",), ("RUN-2",)]),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    result = p.find_runs(["running", "queued"])
    assert result == ["RUN-1", "RUN-2"]
    sql_text, params = conn.calls[-1]
    assert sql_text.startswith("SELECT run_id FROM cat1.sch1.run_state")
    assert ":s0" in sql_text and ":s1" in sql_text
    assert params == {"s0": "running", "s1": "queued"}


def test_begin_node_attempt_uses_merge_and_named_params_with_phase_epoch():
    handlers = {
        "SELECT * FROM cat1.sch1.node_attempts WHERE run_id = :run_id AND node_name = :node_name "
        "AND phase = :phase AND phase_epoch = :phase_epoch AND outcome IS NULL": lambda s, p: ([], []),
        "SELECT COUNT(*)": lambda s, p: (["n"], [(0,)]),
        "MERGE INTO cat1.sch1.node_attempts": lambda s, p: ([], []),
        "SELECT * FROM cat1.sch1.node_attempts WHERE execution_key": lambda s, p: (
            ["attempt_id", "execution_key", "node_name", "attempt_number", "phase_epoch"],
            [("abc", "RUN-1:plan:1:discover:1", "discover", 1, 1)],
        ),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    attempt = p.begin_node_attempt(
        run_id="RUN-1", phase="plan", phase_epoch=1, node_index=0, node_name="discover",
        state_version_before=1, now=canonical_ts(1),
    )
    assert attempt["execution_key"] == "RUN-1:plan:1:discover:1"
    merge_calls = [c for c in conn.calls if c[0].startswith("MERGE INTO")]
    assert len(merge_calls) == 1
    assert "WHEN NOT MATCHED THEN INSERT" in merge_calls[0][0]
    assert merge_calls[0][1]["execution_key"] == "RUN-1:plan:1:discover:1"
    assert merge_calls[0][1]["phase_epoch"] == 1


def test_complete_node_attempt_already_closed_same_outcome_is_noop():
    handlers = {
        "UPDATE cat1.sch1.node_attempts": lambda sql_text, params: (["num_affected_rows"], [(0,)]),
        "SELECT outcome FROM cat1.sch1.node_attempts": lambda sql_text, params: (["outcome"], [("succeeded",)]),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    p.complete_node_attempt("RUN-1:plan:1:discover:1", outcome="succeeded", now=canonical_ts(1))  # no raise


def test_complete_node_attempt_already_closed_different_outcome_raises():
    from orchestrator.errors import AttemptAlreadyClosed

    handlers = {
        "UPDATE cat1.sch1.node_attempts": lambda sql_text, params: (["num_affected_rows"], [(0,)]),
        "SELECT outcome FROM cat1.sch1.node_attempts": lambda sql_text, params: (["outcome"], [("succeeded",)]),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    with pytest.raises(AttemptAlreadyClosed):
        p.complete_node_attempt("RUN-1:plan:1:discover:1", outcome="failed", now=canonical_ts(1))


def test_complete_node_attempt_missing_raises_attempt_not_found():
    from orchestrator.errors import AttemptNotFound

    handlers = {
        "UPDATE cat1.sch1.node_attempts": lambda sql_text, params: (["num_affected_rows"], [(0,)]),
        "SELECT outcome FROM cat1.sch1.node_attempts": lambda sql_text, params: ([], []),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    with pytest.raises(AttemptNotFound):
        p.complete_node_attempt("RUN-1:plan:1:discover:1", outcome="failed", now=canonical_ts(1))


def test_reconnects_once_on_connection_error_then_succeeds():
    factory_calls = {"n": 0}

    class Cur:
        def __init__(self, should_fail):
            self.should_fail = should_fail
            self.description = []
            self._rows = []

        def execute(self, sql_text, params=None):
            if self.should_fail:
                raise Exception("Connection reset by peer")
            self.description = [("fingerprint_id",)]
            self._rows = []
            return self

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    class Conn:
        def __init__(self, should_fail):
            self.should_fail = should_fail

        def cursor(self):
            return Cur(self.should_fail)

        def close(self):
            pass

    def factory():
        factory_calls["n"] += 1
        return Conn(should_fail=(factory_calls["n"] == 1))

    p = DeltaPersistence(_settings(), connection_factory=factory)
    with pytest.raises(RunNotFound):
        p.get_fingerprint("FP-MISSING")
    assert factory_calls["n"] == 2  # dropped the broken connection and reconnected once


def test_connection_reused_across_calls_not_reopened_every_time():
    factory_calls = {"n": 0}
    handlers = {
        "SELECT * FROM cat1.sch1.run_fingerprints": lambda sql_text, params: ([], []),
    }

    def factory():
        factory_calls["n"] += 1
        return FakeConnection(handlers)

    p = DeltaPersistence(_settings(), connection_factory=factory)
    with pytest.raises(RunNotFound):
        p.get_fingerprint("FP-1")
    with pytest.raises(RunNotFound):
        p.get_fingerprint("FP-2")
    assert factory_calls["n"] == 1  # same connection reused, not reopened per call


def test_migrate_swallows_already_exists_errors_on_alter_statements(tmp_path):
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "001_test.sql").write_text(
        "CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.foo (a STRING);\n"
        "ALTER TABLE ${catalog}.${schema}.foo ADD CONSTRAINT foo_pk CHECK (a IS NOT NULL);\n"
    )

    class Cur:
        def __init__(self):
            self.description = []
            self._rows = []

        def execute(self, sql_text, params=None):
            if sql_text.strip().startswith("ALTER TABLE"):
                raise Exception("[DELTA_CONSTRAINT_ALREADY_EXISTS] a constraint named foo_pk already exists")
            if sql_text.strip().startswith("SELECT version, checksum"):
                self.description = [("version",), ("checksum",)]
                self._rows = []
            else:
                self.description = []
                self._rows = []
            return self

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    p = DeltaPersistence(_settings(), ddl_dir=ddl_dir, connection_factory=lambda: Conn())
    applied = p.migrate()
    assert applied == ["001"]


def test_migrate_still_raises_non_already_exists_errors(tmp_path):
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "001_test.sql").write_text(
        "ALTER TABLE ${catalog}.${schema}.foo ADD CONSTRAINT foo_pk CHECK (a IS NOT NULL);\n"
    )

    class Cur:
        description = [("version",), ("checksum",)]

        def execute(self, sql_text, params=None):
            if sql_text.strip().startswith("ALTER TABLE"):
                raise Exception("some unrelated permanent failure")
            return self

        def fetchone(self):
            return None

        def fetchall(self):
            return []

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    p = DeltaPersistence(_settings(), ddl_dir=ddl_dir, connection_factory=lambda: Conn())
    with pytest.raises(Exception, match="unrelated permanent failure"):
        p.migrate()


def test_delta_migration_statements_have_no_semicolon_inside_a_string_literal():
    string_re = re.compile(r"'([^']*)'")
    for path in sorted(DELTA_DDL_DIR.glob("*.sql")):
        text = path.read_text()
        for m in string_re.finditer(text):
            assert ";" not in m.group(1), (
                f"{path.name}: semicolon inside string literal {m.group(0)!r} would "
                f"break the naive split_statements() splitter"
            )


def test_delta_migration_comment_lines_have_no_semicolon():
    # split_statements() splits on every ";", including one inside a "--"
    # comment, which turns the rest of the comment line into a bogus SQL
    # statement (found live: migration 011 failed on Delta, 2026-09-24).
    for path in sorted(DELTA_DDL_DIR.glob("*.sql")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("--"):
                assert ";" not in line, f"{path.name}:{lineno}: semicolon in a comment line"


# ── lease operations use their own connection (RUN-5E0D4353A7BB, P3 gap-audit review follow-up) ──


def _lease_handlers():
    return {
        "MERGE INTO cat1.sch1.run_leases": lambda sql_text, params: (["num_affected_rows"], [(1,)]),
        "UPDATE cat1.sch1.run_leases": lambda sql_text, params: (["num_affected_rows"], [(1,)]),
        "DELETE FROM cat1.sch1.run_leases": lambda sql_text, params: ([], []),
        "SELECT run_id FROM cat1.sch1.run_leases": lambda sql_text, params: (["run_id"], []),
    }


def test_lease_operations_use_a_connection_and_lock_separate_from_the_main_one():
    """A single DeltaPersistence instance is shared, per process, by
    ThreadExecutor and every node it runs (orchestrator/service.py's
    build_app_context). Before this fix, every method -- including
    renew_lease -- shared one `_conn`/`_conn_lock`, so the heartbeat
    thread's own renewal calls could queue behind whatever the pipeline
    thread's node-output writes were doing on that same connection. Lease
    methods must open (and lock) a connection distinct from the main one."""
    conns_created = []

    def factory():
        conns_created.append(FakeConnection(_lease_handlers()))
        return conns_created[-1]

    p = DeltaPersistence(_settings(), connection_factory=factory)

    assert p.acquire_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(1)) is True
    assert len(conns_created) == 1
    lease_conn = conns_created[0]

    # A main-connection call (e.g. list_node_attempts) must open a SECOND,
    # separate connection -- never reuse the lease connection.
    with p._cursor_ctx() as conn:
        pass
    assert len(conns_created) == 2
    main_conn = conns_created[1]

    assert main_conn is not lease_conn
    assert p._conn is main_conn
    assert p._lease_conn is lease_conn
    assert p._conn_lock is not p._lease_conn_lock

    # A further lease call reuses the SAME lease connection (lazy-open-once),
    # never the main one.
    assert p.renew_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(2)) is True
    assert len(conns_created) == 2


def test_renew_lease_is_never_blocked_by_the_main_connections_lock():
    """Structural proof of the fix: holding `_conn_lock` for a long time
    (simulating a node's own multi-minute write_flagged_rows/
    write_run_metrics batch loop on the main connection) must never delay
    renew_lease, because it never touches `_conn_lock` at all."""
    p = DeltaPersistence(_settings(), connection_factory=lambda: FakeConnection(_lease_handlers()))

    main_lock_held = threading.Event()
    release_main_lock = threading.Event()

    def hold_main_lock():
        with p._cursor_ctx():
            main_lock_held.set()
            release_main_lock.wait(timeout=10)

    holder = threading.Thread(target=hold_main_lock, daemon=True)
    holder.start()
    try:
        assert main_lock_held.wait(timeout=10)

        start = time.monotonic()
        renewed = p.renew_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(1))
        elapsed = time.monotonic() - start

        assert renewed is True
        assert elapsed < 1.0, (
            f"renew_lease took {elapsed:.2f}s while the main connection's lock was held -- "
            "it must never queue behind it"
        )
    finally:
        release_main_lock.set()
        holder.join(timeout=10)
