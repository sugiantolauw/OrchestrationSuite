from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from orchestrator.adapters.persistence_delta import DeltaPersistence
from orchestrator.config import Settings
from orchestrator.errors import (
    ConfigError,
    ConnectionPoolExhausted,
    RiskStatusRegression,
    RunAlreadyExists,
    RunNotFound,
    StaleStateError,
    TransientInfrastructureError,
)
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


def _settings(**overrides):
    base = dict(catalog="cat1", schema="sch1", warehouse_http_path="/sql/1", host="https://x.cloud.databricks.com")
    base.update(overrides)
    return Settings(**base)


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


# ── BUG-FINALISE-CONCURRENCY-1 (independent review round 3, 2026-09-25) ────
# statement-level concurrency retry for a write OTHER than the run_state CAS
# above -- e.g. `finalise`'s own MERGE into `findings`, reproduced here via
# the simpler append_trace_event write path (same MERGE shape).


def test_write_retries_a_concurrency_error_once_then_succeeds_no_exception():
    calls = {"n": 0}

    class Cur:
        def execute(self, sql_text, params=None):
            if sql_text.startswith("MERGE INTO cat1.sch1.trace_events"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise Exception(
                        "[DELTA_CONCURRENT_APPEND.ROW_LEVEL_CHANGES] Transaction conflict "
                        "detected. Please retry the operation."
                    )
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

    p = DeltaPersistence(_settings(), connection_factory=lambda: Conn())
    # No exception at all -- the caller never sees the transient conflict.
    p.append_trace_event(
        {
            "event_id": "E1", "run_id": "RUN-1", "event_type": "node_completed", "event_time": canonical_ts(1),
            "stage": "Exports", "status": "complete", "message": "done", "actor": "pipeline",
        }
    )
    assert calls["n"] == 2  # failed once, succeeded on retry


def test_write_wraps_persistent_concurrency_error_as_transient_infrastructure_error():
    class Cur:
        def execute(self, sql_text, params=None):
            if sql_text.startswith("MERGE INTO cat1.sch1.trace_events"):
                raise Exception(
                    "[DELTA_CONCURRENT_APPEND.ROW_LEVEL_CHANGES] Transaction conflict "
                    "detected. Please retry the operation."
                )
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

    p = DeltaPersistence(_settings(), connection_factory=lambda: Conn())
    with pytest.raises(TransientInfrastructureError, match="DELTA_CONCURRENT_APPEND"):
        p.append_trace_event(
            {
                "event_id": "E1", "run_id": "RUN-1", "event_type": "node_completed", "event_time": canonical_ts(1),
                "stage": "Exports", "status": "complete", "message": "done", "actor": "pipeline",
            }
        )


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


# ── upsert_risks/upsert_controls: batched, not one SELECT+UPDATE/INSERT per
# row (P3/P4 perf gap review 2026-09-25 -- register_skill's own dominant cost,
# measured live at ~80s: 2 round trips PER risk/control, every single run,
# even when nothing had changed) ────────────────────────────────────────────

def _risk(risk_id: str, **overrides) -> dict:
    row = {"risk_id": risk_id, "title": f"Risk {risk_id}", "status": "proposed", "source": "manual"}
    row.update(overrides)
    return row


def _control(control_id: str, **overrides) -> dict:
    row = {"control_id": control_id, "title": f"Control {control_id}", "risk_id": "RSK-1"}
    row.update(overrides)
    return row


def test_upsert_risks_issues_one_batched_select_then_one_merge_per_batch():
    handlers = {
        "SELECT risk_id, status FROM cat1.sch1.risks": lambda s, p: (["risk_id", "status"], []),
        "MERGE INTO cat1.sch1.risks": lambda s, p: ([], []),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    risks = [_risk(f"RSK-{i}") for i in range(14)]
    p.upsert_risks(risks, now=canonical_ts(0))

    select_calls = [c for c in conn.calls if c[0].startswith("SELECT risk_id, status FROM cat1.sch1.risks")]
    merge_calls = [c for c in conn.calls if c[0].startswith("MERGE INTO cat1.sch1.risks")]
    # One IN-list existence check for all 14 risks (not 14 separate SELECTs)
    # and one batched multi-row MERGE (not 14 separate UPDATE/INSERTs) -- 14
    # rows is well under _MERGE_BATCH_SIZE (250), so each is exactly one
    # statement: a small constant number, not 2xN.
    assert len(select_calls) == 1
    assert len(merge_calls) == 1
    assert len(select_calls[0][1]) == 14  # one bound risk_id per row in the IN-list
    assert len(merge_calls[0][1]) == 14 * 13  # 13 params per risk row


def test_upsert_risks_empty_list_issues_no_statements():
    conn = FakeConnection({})
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    p.upsert_risks([], now=canonical_ts(0))
    assert conn.calls == []


def test_upsert_risks_status_regression_checked_before_any_write():
    def _select_handler(sql_text, params):
        assert set(params.values()) == {"RSK-1", "RSK-2"}
        return (["risk_id", "status"], [("RSK-1", "accepted")])

    handlers = {
        "SELECT risk_id, status FROM cat1.sch1.risks": _select_handler,
        "MERGE INTO cat1.sch1.risks": lambda s, p: ([], []),
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    # RSK-1 already accepted; this call tries to move it back to proposed.
    # RSK-2 has no regression -- but the whole batched call must still be
    # rejected before any MERGE is issued (not just RSK-1's row), matching
    # the single-item contract test_persistence_p2.py already pins.
    risks = [_risk("RSK-1", status="proposed"), _risk("RSK-2", status="proposed")]
    with pytest.raises(RiskStatusRegression):
        p.upsert_risks(risks, now=canonical_ts(0))

    merge_calls = [c for c in conn.calls if c[0].startswith("MERGE INTO cat1.sch1.risks")]
    assert merge_calls == []


def test_upsert_controls_issues_one_merge_per_batch_with_no_select():
    handlers = {"MERGE INTO cat1.sch1.controls": lambda s, p: ([], [])}
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    controls = [_control(f"CTL-{i}") for i in range(14)]
    p.upsert_controls(controls, now=canonical_ts(0))

    # Controls carry no status -- MERGE alone decides insert vs update, so no
    # existence-check SELECT is needed at all, unlike risks.
    assert len(conn.calls) == 1
    merge_calls = [c for c in conn.calls if c[0].startswith("MERGE INTO cat1.sch1.controls")]
    assert len(merge_calls) == 1
    assert len(merge_calls[0][1]) == 14 * 11  # 11 params per control row


def test_upsert_controls_empty_list_issues_no_statements():
    conn = FakeConnection({})
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)
    p.upsert_controls([], now=canonical_ts(0))
    assert conn.calls == []


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


def test_lease_and_main_calls_use_separate_connections_even_on_one_thread():
    """The bounded pool (P3/P4 perf gap review 2026-09-25, bounded-pool
    follow-up) replaced the earlier per-thread model, which itself replaced
    a hand-maintained lease/main connection split (RUN-5E0D4353A7BB). The
    lease connection is dedicated and outside the pool by design -- a lease
    call and a main-table call, even from the very same thread, use
    different connections. Each kind still reuses its own connection on a
    later call, never reopening it."""
    conns_created = []

    def factory():
        conns_created.append(FakeConnection(_lease_handlers()))
        return conns_created[-1]

    p = DeltaPersistence(_settings(), connection_factory=factory)

    assert p.acquire_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(1)) is True
    assert len(conns_created) == 1
    lease_conn = conns_created[0]

    with p._cursor_ctx() as conn:
        assert conn.raw is not lease_conn  # a pool connection, never the lease one
    assert len(conns_created) == 2
    pool_conn = conns_created[1]

    # A further lease call reuses the dedicated lease connection...
    assert p.renew_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(2)) is True
    assert len(conns_created) == 2

    # ...and a further pool call reuses the pool's now-idle connection.
    with p._cursor_ctx() as conn:
        assert conn.raw is pool_conn
    assert len(conns_created) == 2


def test_idle_pool_connection_reused_by_a_different_thread():
    """The requested property directly: a new thread checking out a
    connection gets an existing IDLE one back rather than opening a fresh
    one -- the connection factory is not called again."""
    conns_created = []
    creation_lock = threading.Lock()

    def factory():
        with creation_lock:
            conns_created.append(FakeConnection({}))
            return conns_created[-1]

    p = DeltaPersistence(_settings(), connection_factory=factory)

    def _thread_a():
        with p._cursor_ctx():
            pass  # opens the pool's one connection so far, then returns it idle

    t_a = threading.Thread(target=_thread_a)
    t_a.start()
    t_a.join(timeout=5)
    assert len(conns_created) == 1

    seen: dict[str, object] = {}

    def _thread_b():
        with p._cursor_ctx() as conn:
            seen["conn"] = conn.raw

    t_b = threading.Thread(target=_thread_b)
    t_b.start()
    t_b.join(timeout=5)

    assert len(conns_created) == 1  # reused thread A's idle connection
    assert seen["conn"] is conns_created[0]


def test_pool_bound_respected_extra_checkout_waits_then_proceeds():
    """N+1 concurrent checkouts against a pool of size N: the extra one
    waits (never opens an (N+1)th connection) until one is returned, then
    proceeds with the one that came back."""
    factory_calls = {"n": 0}
    calls_lock = threading.Lock()

    def factory():
        with calls_lock:
            factory_calls["n"] += 1
        return FakeConnection({})

    p = DeltaPersistence(_settings(max_connections=2), connection_factory=factory, pool_checkout_timeout_s=5.0)

    holder_a_ready = threading.Event()
    holder_b_ready = threading.Event()
    release_holders = threading.Event()
    waiter_proceeded = threading.Event()

    def _hold(ready_event):
        with p._cursor_ctx():
            ready_event.set()
            release_holders.wait(timeout=5)

    t_a = threading.Thread(target=_hold, args=(holder_a_ready,))
    t_b = threading.Thread(target=_hold, args=(holder_b_ready,))
    t_a.start()
    t_b.start()
    assert holder_a_ready.wait(timeout=5)
    assert holder_b_ready.wait(timeout=5)
    assert factory_calls["n"] == 2  # the pool (size 2) is now fully checked out

    def _waiter():
        with p._cursor_ctx():
            waiter_proceeded.set()

    t_c = threading.Thread(target=_waiter)
    t_c.start()
    # Must NOT have proceeded yet -- the pool is exhausted.
    assert not waiter_proceeded.wait(timeout=0.3)
    assert factory_calls["n"] == 2  # still no 3rd connection opened while waiting

    release_holders.set()
    t_a.join(timeout=5)
    t_b.join(timeout=5)
    assert waiter_proceeded.wait(timeout=5), "waiter never proceeded after a connection was returned"
    t_c.join(timeout=5)
    assert factory_calls["n"] == 2  # the waiter reused a returned connection, never a 3rd


def test_pool_opens_concurrent_connections_in_parallel_not_serially():
    """Regression test for a real defect found live (2026-09-25, bounded-pool
    follow-up review): checkout() used to call the slow, blocking
    `_factory()` (a real connection open measured at ~5-6s against the live
    warehouse) WHILE HOLDING the pool's lock, serialising every concurrent
    checkout behind it one open at a time -- a burst of concurrent checkouts
    against a cold pool showed a MINIMUM latency of ~19-20s, consistent with
    several 5-6s opens happening in series rather than in parallel. The fix
    reserves a slot (a `_POOL_RESERVED` placeholder) before releasing the
    lock, so up to `max_size` opens can genuinely proceed at once. Simulates
    the slow open with a sleeping factory and asserts N concurrent checkouts
    against a cold pool of size N take roughly ONE open's worth of time, not
    N of them."""
    open_delay_s = 0.2
    n_concurrent = 4

    def factory():
        time.sleep(open_delay_s)
        return FakeConnection({})

    p = DeltaPersistence(_settings(max_connections=n_concurrent), connection_factory=factory)

    def _checkout_and_return():
        with p._cursor_ctx():
            pass

    threads = [threading.Thread(target=_checkout_and_return) for _ in range(n_concurrent)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    total_elapsed = time.monotonic() - start

    # Serialised (the bug): ~n_concurrent * open_delay_s (0.8s for 4 x 0.2s).
    # Parallel (the fix): ~one open_delay_s (0.2s), regardless of n_concurrent.
    assert total_elapsed < open_delay_s * (n_concurrent / 2), (
        f"{n_concurrent} concurrent checkouts against a cold pool took {total_elapsed:.3f}s "
        f"for a {open_delay_s}s-per-open factory -- looks like opens are serialised, not parallel"
    )
    assert p._pool.size() == n_concurrent  # all N connections genuinely opened


def test_pool_checkout_times_out_loudly_when_never_released():
    """CLAUDE.md NN14: an exhausted pool that never frees up must fail
    loudly with a clear error, not hang forever or silently proceed with no
    connection."""

    def factory():
        return FakeConnection({})

    p = DeltaPersistence(_settings(max_connections=1), connection_factory=factory, pool_checkout_timeout_s=0.2)

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def _hold():
        with p._cursor_ctx():
            holder_ready.set()
            release_holder.wait(timeout=5)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    try:
        assert holder_ready.wait(timeout=5)

        start = time.monotonic()
        with pytest.raises(ConnectionPoolExhausted):
            with p._cursor_ctx():
                pass
        elapsed = time.monotonic() - start
        assert 0.15 <= elapsed < 2.0, f"checkout timeout took {elapsed:.2f}s, expected ~0.2s"
    finally:
        release_holder.set()
        t.join(timeout=5)


def test_lease_never_blocked_when_pool_is_fully_exhausted():
    """acquire_lease/renew_lease must succeed even while every pool
    connection is checked out and nothing is being returned -- they use
    their own dedicated connection, never the pool, so pool exhaustion
    cannot reach them."""

    def factory():
        return FakeConnection(_lease_handlers())

    p = DeltaPersistence(_settings(max_connections=1), connection_factory=factory, pool_checkout_timeout_s=5.0)

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def _hold_pool():
        with p._cursor_ctx():
            holder_ready.set()
            release_holder.wait(timeout=10)

    t = threading.Thread(target=_hold_pool, daemon=True)
    t.start()
    try:
        assert holder_ready.wait(timeout=5)

        start = time.monotonic()
        assert p.acquire_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(1)) is True
        assert p.renew_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(2)) is True
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, (
            f"lease ops took {elapsed:.2f}s while the pool was fully exhausted -- "
            "they must never queue behind it"
        )
    finally:
        release_holder.set()
        t.join(timeout=10)


def test_no_connection_opened_until_first_use():
    """Lazy-open, still true under the pool model: constructing a
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


def test_reconnect_once_replaces_only_the_broken_connection_not_others():
    """A connection error must drop and replace only the ONE broken
    connection. A different connection the pool is concurrently holding
    checked out (by another thread) must be completely untouched -- never
    dropped, never reopened."""
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
        # Only the driving (main) thread's FIRST connection is broken -- its
        # reconnect, and every connection any other thread opens, is healthy.
        return Conn(should_fail=(ident == main_ident and call_n == 1))

    p = DeltaPersistence(_settings(max_connections=2), connection_factory=factory)

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

    # The pool has one connection checked out (healthy). The main thread's
    # own checkout opens a SECOND (pool max_connections=2), which is the
    # broken one; get_fingerprint reconnects once and succeeds, never
    # touching the healthy thread's connection.
    with pytest.raises(RunNotFound):
        p.get_fingerprint("FP-MISSING")

    release_healthy.set()
    t_healthy.join(timeout=5)

    assert calls_by_thread[main_ident] == 2  # broken conn + its one reconnect
    assert calls_by_thread[healthy_ident[0]] == 1  # never touched by the other thread's reconnect


def test_close_closes_pool_and_lease_connection_and_is_idempotent():
    """close() (for an explicit, deterministic shutdown) reaches every
    connection the pool currently owns plus the dedicated lease connection --
    and calling it again, or after nothing further has opened, must never
    double-close or raise."""
    closed = []

    class ClosingConn(FakeConnection):
        def close(self):
            closed.append(self)

    def factory():
        return ClosingConn(_lease_handlers())

    p = DeltaPersistence(_settings(), connection_factory=factory)

    with p._cursor_ctx():
        pass  # opens one pool connection
    assert p.acquire_lease("RUN-1", "worker-1", ttl_s=90, now=canonical_ts(1)) is True  # opens the lease connection

    p.close()
    assert len(closed) == 2  # the pool connection + the lease connection

    p.close()  # idempotent: nothing left to close, no error
    assert len(closed) == 2


def test_update_management_action_normalizes_empty_target_date_before_binding():
    """BUG-ACTIONS-EDIT-OWNER-1 (independent review round 5, RUN-DE56A9DED2DB):
    live editing only an action's owner through /workspace/tne left owner
    unset in Delta. Root cause: the callback's target_date State comes back
    "" rather than None when the auditor never touches the date field, and
    target_date is DATE on this dialect (STRING/TEXT on LocalPersistence's
    sqlite, which tolerates ""). Binding an empty-string DATE param raises
    Delta's own CAST_INVALID_INPUT and fails the WHOLE UPDATE -- owner
    included -- not just target_date. This FakeConnection reproduces that
    dialect behaviour (raises if target_date is ever bound as "") and
    proves DeltaPersistence.update_management_action never sends "" for
    target_date, even when called with target_date="" directly."""

    class CastStrictError(Exception):
        pass

    def _update_handler(sql_text, params):
        if params.get("target_date") == "":
            raise CastStrictError(
                "[CAST_INVALID_INPUT] The value '' of the type \"STRING\" cannot be "
                "cast to \"DATE\" because it is malformed."
            )
        return ([], [])

    handlers = {
        "SELECT ma.*": lambda sql_text, params: (
            ["action_id", "owner", "status", "target_date", "description", "updated_by"],
            [("MA-1", None, "draft", None, None, None)],
        ),
        "UPDATE cat1.sch1.management_actions": _update_handler,
    }
    conn = FakeConnection(handlers)
    p = DeltaPersistence(_settings(), connection_factory=lambda: conn)

    updated = p.update_management_action(
        "MA-1", owner="Alex Chen", status="agreed", target_date="",
        response="ack", updated_by="reviewer@example.com", now=canonical_ts(1),
    )
    assert updated["owner"] == "Alex Chen"

    update_calls = [c for c in conn.calls if c[0].startswith("UPDATE cat1.sch1.management_actions")]
    assert len(update_calls) == 1
    _, params = update_calls[0]
    assert params["target_date"] is None
