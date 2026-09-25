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
        # `params` is a plain dict for _execute's calls, but a list of typed
        # Parameter objects (each carrying .name/.value) for _execute_typed's
        # -- normalise both into the same plain dict shape callers/handlers
        # below actually work with.
        if params is None:
            named = {}
        elif isinstance(params, dict):
            named = dict(params)
        else:
            named = {p.name: p.value for p in params}
        self.conn.calls.append((sql_text, named))
        handler = self.conn.handlers.get(self._match(sql_text))
        if handler is not None:
            cols, rows = handler(sql_text, named)
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


# ── write_issues_for_findings: batched, not one SELECT+INSERT per finding
# (P3/P4 perf gap review 2026-09-25 -- the exact row-by-row shape
# _MERGE_BATCH_SIZE's own docstring already names as the pattern that made a
# large write take 20+ minutes live; this one was missed when
# write_flagged_rows/write_run_metrics were fixed) ─────────────────────────

def _finding_for_issue(finding_id: str, **overrides) -> dict:
    row = {
        "finding_id": finding_id, "rule_id": f"SKILL.{finding_id}", "title": f"Finding {finding_id}",
        "observation": "obs", "severity": "Medium",
    }
    row.update(overrides)
    return row


def test_write_issues_for_findings_is_two_statements_not_two_per_finding():
    handlers = {
        "SELECT issue_id FROM cat1.sch1.issues": lambda sql_text, params: (["issue_id"], []),
        "INSERT INTO cat1.sch1.issues": lambda sql_text, params: ([], []),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    findings = [_finding_for_issue(f"F{i}") for i in range(12)]
    created = p.write_issues_for_findings("RUN-1", findings, engagement_id="ENG-1", now=canonical_ts(0))

    assert {c["finding_id"] for c in created} == {f"F{i}" for i in range(12)}
    select_calls = [c for c in conn.calls if c[0].startswith("SELECT issue_id FROM cat1.sch1.issues")]
    insert_calls = [c for c in conn.calls if c[0].startswith("INSERT INTO cat1.sch1.issues")]
    # One existence check for all 12 findings (a single IN-list, not 12 separate
    # SELECTs) and one batched multi-row INSERT for whatever is new (not 12
    # separate INSERTs) -- 12 findings is well under _MERGE_BATCH_SIZE (250),
    # so each is exactly one statement.
    assert len(select_calls) == 1
    assert len(insert_calls) == 1
    # The single INSERT's VALUES list carries all 12 rows -- 11 params per row
    # (iss/eng/rule/title/desc/rating/status/fid/rid/created/updated).
    assert len(insert_calls[0][1]) == 12 * 11


def test_write_issues_for_findings_skips_existing_and_only_inserts_new():
    existing_issue_id = "ISS-F1"

    def _select_handler(sql_text, params):
        # The existence check's IN-list should be asked about every finding's
        # issue_id -- only ISS-F1 is reported back as already present.
        assert set(params.values()) == {"ISS-F0", "ISS-F1", "ISS-F2"}
        return (["issue_id"], [(existing_issue_id,)])

    handlers = {
        "SELECT issue_id FROM cat1.sch1.issues": _select_handler,
        "INSERT INTO cat1.sch1.issues": lambda sql_text, params: ([], []),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    findings = [_finding_for_issue("F0"), _finding_for_issue("F1"), _finding_for_issue("F2")]
    created = p.write_issues_for_findings("RUN-1", findings, engagement_id="ENG-1", now=canonical_ts(0))

    assert {c["finding_id"] for c in created} == {"F0", "F2"}  # F1 already had an issue
    insert_calls = [c for c in conn.calls if c[0].startswith("INSERT INTO cat1.sch1.issues")]
    assert len(insert_calls) == 1
    assert len(insert_calls[0][1]) == 2 * 11  # only F0 and F2's rows, not F1's


def test_write_issues_for_findings_empty_list_issues_no_statements():
    conn = FakeConnection({})
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    assert p.write_issues_for_findings("RUN-1", [], engagement_id="ENG-1", now=canonical_ts(0)) == []
    assert conn.calls == []


# ── Batched writers issue one statement at a time via `_exec1`, and (since
# the P3/P4 perf gap review's per-thread connection fix) run on a connection
# no other thread shares (P3/P4 perf gap review 2026-09-25): write_flagged_rows/
# write_run_metrics/put_test_line_values/write_classification_results used to
# hold one `with self._cursor_ctx()` (and so one acquisition of a single
# shared `_conn_lock`) across their entire existence-check SELECT + every
# MERGE batch + the prune DELETE. A concurrent caller (a /run/<id> render on
# another thread) waited for the whole write, not one statement of it, on
# that same shared connection. `_exec1` scoped that to one statement at a
# time; per-thread connections then removed the shared lock/connection
# entirely, so a concurrent reader on another thread now waits on nothing at
# all. These tests pin the exact statement sequence _exec1 depends on (results
# unchanged), plus the actual concurrency property. ─────────────────────────

def test_write_flagged_rows_issues_one_select_then_one_merge_per_batch():
    handlers = {
        "SELECT source, row_key, flag FROM cat1.sch1.flagged_rows": lambda s, p: (["source", "row_key", "flag"], []),
        "MERGE INTO cat1.sch1.flagged_rows": lambda s, p: ([], []),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    rows = [{"source": "expense", "row_key": f"R{i}", "flag": "RF_X", "group_id": None} for i in range(5)]
    p.write_flagged_rows("RUN-1", rows)

    select_calls = [c for c in conn.calls if c[0].startswith("SELECT source, row_key, flag")]
    merge_calls = [c for c in conn.calls if c[0].startswith("MERGE INTO cat1.sch1.flagged_rows")]
    assert len(select_calls) == 1
    assert len(merge_calls) == 1  # 5 rows is one batch (_MERGE_BATCH_SIZE=250)


def test_write_run_metrics_issues_one_select_then_one_merge_per_batch():
    handlers = {
        "SELECT metric_name FROM cat1.sch1.run_metrics": lambda s, p: (["metric_name"], []),
        "MERGE INTO cat1.sch1.run_metrics": lambda s, p: ([], []),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    metrics = [{"metric_name": f"m{i}", "value": 1.0, "unit": "count", "source_ref": {}, "test_id": "T1"} for i in range(5)]
    p.write_run_metrics("RUN-1", metrics)

    select_calls = [c for c in conn.calls if c[0].startswith("SELECT metric_name FROM cat1.sch1.run_metrics")]
    merge_calls = [c for c in conn.calls if c[0].startswith("MERGE INTO cat1.sch1.run_metrics")]
    assert len(select_calls) == 1
    assert len(merge_calls) == 1


def test_slow_statement_on_one_thread_does_not_delay_a_statement_on_another():
    """The actual property per-thread connections buy (see the block comment
    above): a concurrent reader (a /run/<id> render on another thread) must
    not wait on a writer's slow statements AT ALL, because the two threads
    no longer share a connection or a lock to queue behind. Simulates
    warehouse latency with a sleep inside the MERGE handler and measures how
    long a reader thread's own trivial statement takes while a 3-batch write
    is in progress on another thread."""
    batch_sleep_s = 0.05
    n_batches = 3

    def _merge_handler(sql_text, params):
        time.sleep(batch_sleep_s)
        return ([], [])

    handlers = {
        "SELECT source, row_key, flag FROM cat1.sch1.flagged_rows": lambda s, p: (["source", "row_key", "flag"], []),
        "MERGE INTO cat1.sch1.flagged_rows": _merge_handler,
        "SELECT 1": lambda s, p: (["x"], [(1,)]),
    }
    p = DeltaPersistence(_settings(), connection_factory=lambda: FakeConnection(handlers))
    rows = [
        {"source": "expense", "row_key": f"R{i}", "flag": "RF_X", "group_id": None}
        for i in range(n_batches * 250)
    ]

    writer_started = threading.Event()
    writer_done = threading.Event()
    reader_elapsed: list[float] = []

    def _writer():
        writer_started.set()
        p.write_flagged_rows("RUN-1", rows)
        writer_done.set()

    def _reader():
        writer_started.wait(timeout=5)
        time.sleep(batch_sleep_s * 0.5)  # land mid-write, after its first MERGE batch
        start = time.monotonic()
        with p._cursor_ctx() as conn:
            p._execute(conn, "SELECT 1")
        reader_elapsed.append(time.monotonic() - start)

    t_writer = threading.Thread(target=_writer)
    t_reader = threading.Thread(target=_reader)
    t_writer.start()
    t_reader.start()
    t_writer.join(timeout=5)
    t_reader.join(timeout=5)

    assert writer_done.is_set(), "writer thread did not finish"
    assert reader_elapsed, "reader thread never completed its statement"
    # The reader's own statement is instant on FakeConnection -- if it shared
    # a connection or lock with the writer it would take up to the writer's
    # remaining batches' sleep time. On separate per-thread connections it
    # must complete almost immediately regardless of the writer's progress.
    assert reader_elapsed[0] < batch_sleep_s, (
        f"reader's statement took {reader_elapsed[0]:.3f}s while a writer's slow statement "
        f"was in flight on another thread -- looks like they still share a connection/lock"
    )


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


def test_same_thread_reuses_one_connection_for_lease_and_main_calls():
    """Per-thread connections (P3/P4 perf gap review 2026-09-25) replaced the
    earlier hand-maintained lease/main connection split (RUN-5E0D4353A7BB):
    there is no more separate "lease connection" concept -- a lease call and
    a main-table call from the SAME thread now share that one thread's
    single lazily-opened connection, exactly like any other pair of calls
    from that thread."""
    conns_created = []

    def factory():
        conns_created.append(FakeConnection(_lease_handlers()))
        return conns_created[-1]

    p = DeltaPersistence(_settings(), connection_factory=factory)

    assert p.acquire_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(1)) is True
    assert len(conns_created) == 1

    with p._cursor_ctx() as conn:
        assert conn is conns_created[0]  # reused, not a second connection
    assert len(conns_created) == 1

    assert p.renew_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(2)) is True
    assert len(conns_created) == 1


def test_different_threads_get_different_connections():
    """The property that actually matters and replaces the old lease/main
    split: two different threads calling into the SAME DeltaPersistence
    instance never share a connection -- whether one is doing a lease op and
    the other a main-table op, or both are doing the same kind of call."""
    conns_created = []
    creation_lock = threading.Lock()

    def factory():
        with creation_lock:
            conns_created.append(FakeConnection(_lease_handlers()))
            return conns_created[-1]

    p = DeltaPersistence(_settings(), connection_factory=factory)
    seen: dict[str, object] = {}

    def _lease_thread():
        with p._cursor_ctx() as conn:
            seen["lease"] = conn

    def _main_thread():
        with p._cursor_ctx() as conn:
            seen["main"] = conn

    t1 = threading.Thread(target=_lease_thread)
    t2 = threading.Thread(target=_main_thread)
    t1.start()
    t1.join(timeout=5)
    t2.start()
    t2.join(timeout=5)

    assert len(conns_created) == 2
    assert seen["lease"] is not seen["main"]


def test_renew_lease_is_never_blocked_by_the_main_connections_lock():
    """Structural proof of the fix: a long write on one thread (simulating a
    node's own multi-minute write_flagged_rows/write_run_metrics batch loop)
    must never delay renew_lease running on another thread, because they now
    hold entirely separate connections -- there is no shared lock left to
    queue behind."""
    p = DeltaPersistence(_settings(), connection_factory=lambda: FakeConnection(_lease_handlers()))

    main_lock_held = threading.Event()
    release_main_lock = threading.Event()

    def hold_main_connection():
        with p._cursor_ctx():
            main_lock_held.set()
            release_main_lock.wait(timeout=10)

    holder = threading.Thread(target=hold_main_connection, daemon=True)
    holder.start()
    try:
        assert main_lock_held.wait(timeout=10)

        start = time.monotonic()
        renewed = p.renew_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(1))
        elapsed = time.monotonic() - start

        assert renewed is True
        assert elapsed < 1.0, (
            f"renew_lease took {elapsed:.2f}s while another thread's connection was in use -- "
            "it must never queue behind it"
        )
    finally:
        release_main_lock.set()
        holder.join(timeout=10)


def test_no_connection_opened_until_first_use():
    """Lazy-open, still true under the per-thread model: constructing a
    DeltaPersistence (or leaving it idle between runs) must never touch the
    connection factory -- CLAUDE.md §11's cost incident was exactly this
    kind of unwanted idle activity."""
    factory_calls = {"n": 0}

    def factory():
        factory_calls["n"] += 1
        return FakeConnection({})

    p = DeltaPersistence(_settings(), connection_factory=factory)
    assert factory_calls["n"] == 0

    with p._cursor_ctx():
        pass
    assert factory_calls["n"] == 1


def test_reconnect_once_is_scoped_to_the_failing_threads_own_connection():
    """A connection error on one thread must reconnect only that thread's
    own connection. A different thread's already-open, healthy connection
    must be completely untouched -- never dropped, never reopened -- by
    another thread's reconnect."""
    main_ident = threading.get_ident()
    calls_by_thread: dict[int, int] = {}
    calls_lock = threading.Lock()

    class Cur:
        def __init__(self, should_fail):
            self.should_fail = should_fail
            self.description = [("fingerprint_id",)]
            self._rows: list = []

        def execute(self, sql_text, params=None):
            if self.should_fail:
                raise Exception("Connection reset by peer")
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
        ident = threading.get_ident()
        with calls_lock:
            calls_by_thread[ident] = calls_by_thread.get(ident, 0) + 1
            call_n = calls_by_thread[ident]
        # Only the driving (main) thread's FIRST connection is broken --
        # its reconnect, and every connection any other thread opens, is
        # healthy.
        return Conn(should_fail=(ident == main_ident and call_n == 1))

    p = DeltaPersistence(_settings(), connection_factory=factory)

    healthy_started = threading.Event()
    release_healthy = threading.Event()
    healthy_ident: list[int] = []

    def _healthy_thread():
        healthy_ident.append(threading.get_ident())
        with p._cursor_ctx():
            healthy_started.set()
            release_healthy.wait(timeout=5)

    t_healthy = threading.Thread(target=_healthy_thread)
    t_healthy.start()
    assert healthy_started.wait(timeout=5)

    # Main thread's first connection is the broken one; get_fingerprint
    # reconnects once (on the main thread only) and succeeds.
    with pytest.raises(RunNotFound):
        p.get_fingerprint("FP-MISSING")

    release_healthy.set()
    t_healthy.join(timeout=5)

    assert calls_by_thread[main_ident] == 2  # broken conn + its one reconnect
    assert calls_by_thread[healthy_ident[0]] == 1  # never touched by the other thread's reconnect


def test_connection_closed_when_its_owning_thread_exits():
    """No leak: a thread's connection must be closed once that thread ends,
    without anyone calling close() explicitly -- CPython tears down a
    finished thread's `threading.local` storage, dropping the holder's last
    reference and running `_ThreadConnHolder.__del__`."""
    import gc

    closed = []

    class ClosingConn(FakeConnection):
        def close(self):
            closed.append(self)

    def factory():
        return ClosingConn({})

    p = DeltaPersistence(_settings(), connection_factory=factory)

    def _worker():
        with p._cursor_ctx():
            pass

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)
    gc.collect()  # robust to any interpreter GC-timing detail beyond refcounting

    assert len(closed) == 1


def test_close_closes_every_threads_connection_and_is_idempotent():
    """close() (for an explicit, deterministic shutdown) reaches every
    thread's connection, not just the calling thread's -- and calling it
    again, or after a worker thread has already exited and self-closed its
    own connection, must never double-close or raise."""
    import gc

    closed = []

    class ClosingConn(FakeConnection):
        def close(self):
            closed.append(self)

    def factory():
        return ClosingConn({})

    p = DeltaPersistence(_settings(), connection_factory=factory)

    with p._cursor_ctx():
        pass  # this (main) thread's connection

    def _worker():
        with p._cursor_ctx():
            pass

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)
    gc.collect()
    assert len(closed) == 1  # the worker thread's own connection, already self-closed

    p.close()
    assert len(closed) == 2  # + this thread's, closed by close()

    p.close()  # idempotent: nothing left to close, no error
    assert len(closed) == 2
