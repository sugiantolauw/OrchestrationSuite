from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from orchestrator.config import Settings
from orchestrator.errors import (
    AttemptAlreadyClosed,
    AttemptNotFound,
    FingerprintConflict,
    RunAlreadyExists,
    RunNotFound,
    StaleStateError,
)
from orchestrator.migrations import plan_migrations, split_statements
from orchestrator.state import RunState, assert_json_safe, from_json, to_json, validate
from orchestrator.timeutil import utc_now

logger = logging.getLogger(__name__)

_DEFAULT_DDL_DIR = Path(__file__).resolve().parent.parent / "ddl" / "delta"

_CONCURRENCY_MARKERS = (
    "ConcurrentAppend",
    "ConcurrentDelete",
    "ConcurrentTransaction",
    "DELTA_CONCURRENT",
)

# Heuristic markers for "the connection itself is dead", distinct from a Delta
# concurrency conflict above. Best-effort: this workspace is unreachable today
# (CLAUDE.md §11), so this path is exercised only against the FakeConnection test
# harness, never a live driver.
_CONNECTION_ERROR_MARKERS = (
    "Connection",
    "connection",
    "Broken pipe",
    "EOF occurred",
    "Socket closed",
)

_ALREADY_EXISTS_MARKERS = (
    "ALREADY_EXISTS",
    "already exists",
    "DUPLICATE_COLUMN",
    "FIELDS_ALREADY_EXIST",
)

_CAS_RETRY_ATTEMPTS = 3
_CAS_RETRY_BACKOFF_S = 0.05

_FINGERPRINT_COLUMNS = (
    "source_table_versions",
    "uploaded_file_hashes",
    "reference_data_hashes",
    "skill_content_hash",
    "code_revision",
    "dependency_lock_hash",
    "runtime_config_hash",
    "endpoint_config",
    "prompt_template_version",
)

_RUN_STATE_SUMMARY_COLUMNS = (
    "run_kind",
    "engagement_id",
    "skill_id",
    "skill_version",
    "mode",
    "phase",
    "status",
    "status_reason",
    "objective",
    "run_owner",
    "fingerprint_id",
    "started_at",
    "completed_at",
    "last_state_change_at",
)

_PROJECTION_RETRY_ATTEMPTS = 3

_SCHEMA_MIGRATIONS_BOOTSTRAP = (
    "CREATE TABLE IF NOT EXISTS {qualified} ("
    "version STRING NOT NULL,"
    "description STRING NOT NULL,"
    "checksum STRING NOT NULL,"
    "applied_at TIMESTAMP NOT NULL,"
    "CONSTRAINT schema_migrations_pk PRIMARY KEY (version)"
    ") USING DELTA TBLPROPERTIES ("
    "'delta.enableDeletionVectors' = 'true',"
    "'delta.enableRowTracking' = 'true'"
    ")"
)


def _attempt_id(execution_key: str) -> str:
    return hashlib.sha256(execution_key.encode("utf-8")).hexdigest()[:32]


def _is_concurrency_error(exc: Exception) -> bool:
    text = str(exc)
    return any(marker in text for marker in _CONCURRENCY_MARKERS)


def _is_connection_error(exc: Exception) -> bool:
    text = str(exc)
    return any(marker in text for marker in _CONNECTION_ERROR_MARKERS)


def _is_already_exists_error(exc: Exception) -> bool:
    text = str(exc)
    return any(marker in text for marker in _ALREADY_EXISTS_MARKERS)


def _row_to_dict(cursor, row) -> dict:
    columns = [d[0] for d in cursor.description]
    return dict(zip(columns, row))


def _fetchall_dicts(cursor) -> list[dict]:
    rows = cursor.fetchall()
    return [_row_to_dict(cursor, r) for r in rows]


def _fetchone_dict(cursor) -> dict | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(cursor, row)


def _num_result_rows(cursor, column: str) -> int:
    row = cursor.fetchone()
    if row is None:
        return 0
    try:
        return int(getattr(row, column))
    except AttributeError:
        pass
    try:
        columns = [d[0] for d in cursor.description]
        idx = columns.index(column)
        return int(row[idx])
    except (ValueError, IndexError, TypeError):
        return int(row[0])


def _num_affected_rows(cursor) -> int:
    return _num_result_rows(cursor, "num_affected_rows")


def _num_inserted_rows(cursor) -> int:
    return _num_result_rows(cursor, "num_inserted_rows")


class DeltaPersistence:
    def __init__(
        self,
        settings: Settings,
        *,
        ddl_dir: Path | None = None,
        connection_factory: Callable[[], object] | None = None,
    ):
        settings.require("catalog", "schema", "warehouse_http_path", "host")
        self.settings = settings
        self.ddl_dir = Path(ddl_dir) if ddl_dir else _DEFAULT_DDL_DIR
        self._connection_factory = connection_factory or self._default_connection_factory
        self._prefix = f"{settings.catalog}.{settings.schema}"
        self._conn_lock = threading.Lock()
        self._conn = None

    def _default_connection_factory(self):
        from databricks import sql
        from databricks.sdk.core import Config

        cfg = Config(host=self.settings.host)
        hostname = self.settings.host.replace("https://", "").replace("http://", "").rstrip("/")
        return sql.connect(
            server_hostname=hostname,
            http_path=self.settings.warehouse_http_path,
            credentials_provider=lambda: cfg.authenticate,
        )

    def _table(self, name: str) -> str:
        return f"{self._prefix}.{name}"

    # One connection is opened lazily and reused for the life of this instance (a
    # DeltaPersistence is expected to live for the process, not per-call) instead of
    # opening a fresh one on every method call. On a connection-shaped error the
    # connection is dropped and reopened exactly once before the statement is retried
    # (CLAUDE.md §9C non-blocking item).
    def _cursor_ctx(self):
        return _CursorCtx(self)

    def _get_connection_locked(self):
        if self._conn is None:
            self._conn = self._connection_factory()
        return self._conn

    def _execute(self, conn, sql_text: str, params: dict | None = None):
        try:
            cur = conn.cursor()
            cur.execute(sql_text, params or {})
            return cur
        except Exception as exc:
            if _is_connection_error(exc):
                self._conn = None
                conn = self._get_connection_locked()
                cur = conn.cursor()
                cur.execute(sql_text, params or {})
                return cur
            raise

    # ── migrations ───────────────────────────────────────────────────────────

    def migrate(self) -> list[str]:
        with self._cursor_ctx() as conn:
            self._execute(
                conn, _SCHEMA_MIGRATIONS_BOOTSTRAP.format(qualified=self._table("schema_migrations"))
            )
            cur = self._execute(conn, f"SELECT version, checksum FROM {self._table('schema_migrations')}")
            applied = {r["version"]: r["checksum"] for r in _fetchall_dicts(cur)}
            pending, _ = plan_migrations(self.ddl_dir, applied)
            newly_applied = []
            for m in pending:
                sql_text = m.sql.replace("${catalog}", self.settings.catalog).replace(
                    "${schema}", self.settings.schema
                )
                for stmt in split_statements(sql_text):
                    try:
                        self._execute(conn, stmt)
                    except Exception as exc:
                        # CREATE TABLE already carries IF NOT EXISTS; this covers the
                        # statements that don't (ALTER ... ADD CONSTRAINT / ADD COLUMNS),
                        # so re-running a migration file is a no-op rather than an error
                        # (CLAUDE.md §9C non-blocking item).
                        if not _is_already_exists_error(exc):
                            raise
                self._execute(
                    conn,
                    f"MERGE INTO {self._table('schema_migrations')} t "
                    "USING (SELECT :version AS version) s ON t.version = s.version "
                    "WHEN NOT MATCHED THEN INSERT (version, description, checksum, applied_at) "
                    "VALUES (:version, :description, :checksum, :applied_at)",
                    {
                        "version": m.version,
                        "description": m.description,
                        "checksum": m.checksum,
                        "applied_at": utc_now(),
                    },
                )
                newly_applied.append(m.version)
            return newly_applied

    # ── runs / run_state ─────────────────────────────────────────────────────

    def create_run(self, state: RunState, fingerprint: dict) -> RunState:
        validate(state)
        assert_json_safe(state)
        new_state = _with_version(state, 1)
        new_json = to_json(new_state)
        with self._cursor_ctx() as conn:
            # run_state is the system of record (CLAUDE.md §9C/B4): MERGE it in FIRST
            # and use num_inserted_rows as the existence check.
            cur = self._execute(
                conn,
                f"MERGE INTO {self._table('run_state')} t "
                "USING (SELECT :run_id AS run_id) s ON t.run_id = s.run_id "
                "WHEN NOT MATCHED THEN INSERT (run_id, state_version, state_json, status, updated_at) "
                "VALUES (:run_id, :state_version, :state_json, :status, :updated_at)",
                {
                    "run_id": new_state.run_id,
                    "state_version": new_state.state_version,
                    "state_json": new_json,
                    "status": new_state.status,
                    "updated_at": utc_now(),
                },
            )
            if _num_inserted_rows(cur) == 0:
                raise RunAlreadyExists(state.run_id)

            self._upsert_fingerprint(conn, fingerprint)

            audit_start, audit_end = new_state.audit_period
            self._execute(
                conn,
                f"MERGE INTO {self._table('runs')} t "
                "USING (SELECT :run_id AS run_id) s ON t.run_id = s.run_id "
                "WHEN NOT MATCHED THEN INSERT (run_id, run_kind, engagement_id, skill_id, "
                "skill_version, mode, phase, status, status_reason, audit_period_start, "
                "audit_period_end, objective, run_owner, fingerprint_id, state_version, "
                "superseded_by, created_at, started_at, completed_at, last_state_change_at) "
                "VALUES (:run_id, :run_kind, :engagement_id, :skill_id, :skill_version, :mode, "
                ":phase, :status, :status_reason, :audit_period_start, :audit_period_end, "
                ":objective, :run_owner, :fingerprint_id, :state_version, NULL, :created_at, "
                ":started_at, :completed_at, :last_state_change_at)",
                {
                    "run_id": new_state.run_id,
                    "run_kind": new_state.run_kind,
                    "engagement_id": new_state.engagement_id,
                    "skill_id": new_state.skill_id,
                    "skill_version": new_state.skill_version,
                    "mode": new_state.mode,
                    "phase": new_state.phase,
                    "status": new_state.status,
                    "status_reason": new_state.status_reason,
                    "audit_period_start": audit_start,
                    "audit_period_end": audit_end,
                    "objective": new_state.objective,
                    "run_owner": new_state.run_owner,
                    "fingerprint_id": new_state.fingerprint_id,
                    "state_version": new_state.state_version,
                    "created_at": new_state.created_at,
                    "started_at": new_state.started_at,
                    "completed_at": new_state.completed_at,
                    "last_state_change_at": new_state.last_state_change_at,
                },
            )
            return new_state

    def _upsert_fingerprint(self, conn, fingerprint: dict) -> None:
        fingerprint_id = fingerprint["fingerprint_id"]
        cur = self._execute(
            conn,
            f"SELECT * FROM {self._table('run_fingerprints')} WHERE fingerprint_id = :fingerprint_id",
            {"fingerprint_id": fingerprint_id},
        )
        row = _fetchone_dict(cur)
        if row is None:
            self._execute(
                conn,
                f"INSERT INTO {self._table('run_fingerprints')} (fingerprint_id, source_table_versions, "
                "uploaded_file_hashes, reference_data_hashes, skill_content_hash, code_revision, "
                "dependency_lock_hash, runtime_config_hash, endpoint_config, prompt_template_version, "
                "created_at) VALUES (:fingerprint_id, :source_table_versions, :uploaded_file_hashes, "
                ":reference_data_hashes, :skill_content_hash, :code_revision, :dependency_lock_hash, "
                ":runtime_config_hash, :endpoint_config, :prompt_template_version, :created_at)",
                {
                    "fingerprint_id": fingerprint_id,
                    "source_table_versions": fingerprint["source_table_versions"],
                    "uploaded_file_hashes": fingerprint["uploaded_file_hashes"],
                    "reference_data_hashes": fingerprint.get("reference_data_hashes", "{}"),
                    "skill_content_hash": fingerprint.get("skill_content_hash"),
                    "code_revision": fingerprint["code_revision"],
                    "dependency_lock_hash": fingerprint["dependency_lock_hash"],
                    "runtime_config_hash": fingerprint["runtime_config_hash"],
                    "endpoint_config": fingerprint["endpoint_config"],
                    "prompt_template_version": fingerprint["prompt_template_version"],
                    "created_at": fingerprint.get("created_at") or utc_now(),
                },
            )
            return
        differing = []
        for c in _FINGERPRINT_COLUMNS:
            if c == "reference_data_hashes" and c not in fingerprint:
                continue
            if row.get(c) != fingerprint.get(c):
                differing.append(c)
        if differing:
            raise FingerprintConflict(fingerprint_id, differing)

    def load_state(self, run_id: str) -> RunState:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn, f"SELECT state_json FROM {self._table('run_state')} WHERE run_id = :run_id", {"run_id": run_id}
            )
            row = _fetchone_dict(cur)
        if row is None:
            raise RunNotFound(run_id)
        return from_json(row["state_json"])

    def save_state(self, state: RunState) -> RunState:
        validate(state)
        expected = state.state_version
        new_state = _with_version(state, expected + 1)
        assert_json_safe(new_state)
        new_json = to_json(new_state)
        with self._cursor_ctx() as conn:
            affected = self._cas_update_run_state(conn, state.run_id, expected, new_state, new_json)
            if affected == 0:
                cur = self._execute(
                    conn,
                    f"SELECT state_version FROM {self._table('run_state')} WHERE run_id = :run_id",
                    {"run_id": state.run_id},
                )
                actual_row = _fetchone_dict(cur)
                actual = actual_row["state_version"] if actual_row else None
                raise StaleStateError(state.run_id, expected, actual)

        # The `runs` projection is a convenience for cheap listing/filtering; retried
        # and then logged+swallowed on failure so a committed run_state save never
        # raises because of it (CLAUDE.md §9C/B4). repair_projections() catches up.
        self._update_runs_projection_with_retry(new_state)
        return new_state

    def _cas_update_run_state(self, conn, run_id: str, expected: int, new_state: RunState, new_json: str) -> int:
        params = {
            "new_version": new_state.state_version,
            "state_json": new_json,
            "status": new_state.status,
            "updated_at": utc_now(),
            "run_id": run_id,
            "expected": expected,
        }
        sql_text = (
            f"UPDATE {self._table('run_state')} SET state_version = :new_version, "
            "state_json = :state_json, status = :status, updated_at = :updated_at "
            "WHERE run_id = :run_id AND state_version = :expected"
        )
        for attempt in range(_CAS_RETRY_ATTEMPTS):
            try:
                cur = self._execute(conn, sql_text, params)
                return _num_affected_rows(cur)
            except Exception as exc:
                if not _is_concurrency_error(exc):
                    raise
                cur = self._execute(
                    conn,
                    f"SELECT state_version FROM {self._table('run_state')} WHERE run_id = :run_id",
                    {"run_id": run_id},
                )
                actual_row = _fetchone_dict(cur)
                actual = actual_row["state_version"] if actual_row else None
                if actual != expected:
                    # Someone else genuinely moved it -- this is a real stale write,
                    # not a spurious concurrent-transaction error. Raise with the true
                    # actual version rather than retrying.
                    raise StaleStateError(run_id, expected, actual) from exc
                if attempt < _CAS_RETRY_ATTEMPTS - 1:
                    time.sleep(_CAS_RETRY_BACKOFF_S * (attempt + 1))
        # Exhausted retries while the stored version kept reading back as `expected`
        # (a persistently contended write) -- surface it as stale rather than hang.
        raise StaleStateError(run_id, expected, expected)

    def _update_runs_projection(self, conn, state: RunState) -> None:
        audit_start, audit_end = state.audit_period
        set_clause = ", ".join(f"{c} = :{c}" for c in _RUN_STATE_SUMMARY_COLUMNS)
        params = {c: getattr(state, c) for c in _RUN_STATE_SUMMARY_COLUMNS}
        params.update(
            {
                "audit_period_start": audit_start,
                "audit_period_end": audit_end,
                "new_state_version": state.state_version,
                "run_id": state.run_id,
            }
        )
        self._execute(
            conn,
            f"UPDATE {self._table('runs')} SET {set_clause}, audit_period_start = :audit_period_start, "
            "audit_period_end = :audit_period_end, state_version = :new_state_version "
            "WHERE run_id = :run_id AND state_version < :new_state_version",
            params,
        )

    def _update_runs_projection_with_retry(self, state: RunState) -> None:
        last_exc: Exception | None = None
        for _ in range(_PROJECTION_RETRY_ATTEMPTS):
            try:
                with self._cursor_ctx() as conn:
                    self._update_runs_projection(conn, state)
                return
            except Exception as exc:  # pragma: no cover - defensive, projection only
                last_exc = exc
        logger.warning(
            "runs projection update failed after %d attempts for run_id=%s: %r",
            _PROJECTION_RETRY_ATTEMPTS, state.run_id, last_exc,
        )

    def repair_projections(self) -> int:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT rs.run_id, rs.state_json FROM {self._table('run_state')} rs "
                f"JOIN {self._table('runs')} r ON r.run_id = rs.run_id "
                "WHERE r.state_version < rs.state_version",
            )
            rows = _fetchall_dicts(cur)
        repaired = 0
        for row in rows:
            state = from_json(row["state_json"])
            try:
                with self._cursor_ctx() as conn2:
                    self._update_runs_projection(conn2, state)
                repaired += 1
            except Exception:  # pragma: no cover - defensive
                logger.warning("repair_projections failed for run_id=%s", row["run_id"])
        return repaired

    def get_fingerprint(self, fingerprint_id: str) -> dict:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('run_fingerprints')} WHERE fingerprint_id = :fingerprint_id",
                {"fingerprint_id": fingerprint_id},
            )
            row = _fetchone_dict(cur)
        if row is None:
            raise RunNotFound(fingerprint_id)
        return row

    # ── engagements (P1B) ────────────────────────────────────────────────────

    def get_engagement(self, engagement_id: str) -> dict | None:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('engagements')} WHERE engagement_id = :engagement_id",
                {"engagement_id": engagement_id},
            )
            return _fetchone_dict(cur)

    def list_engagements(self) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(conn, f"SELECT * FROM {self._table('engagements')} ORDER BY created_at")
            return _fetchall_dicts(cur)

    # ── node attempts ────────────────────────────────────────────────────────

    def begin_node_attempt(
        self,
        *,
        run_id: str,
        phase: str,
        phase_epoch: int = 1,
        node_index: int,
        node_name: str,
        state_version_before: int,
        now: str,
    ) -> dict:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('node_attempts')} WHERE run_id = :run_id AND node_name = :node_name "
                "AND phase = :phase AND phase_epoch = :phase_epoch AND outcome IS NULL",
                {"run_id": run_id, "node_name": node_name, "phase": phase, "phase_epoch": phase_epoch},
            )
            open_row = _fetchone_dict(cur)
            if open_row is not None:
                return open_row

            cur = self._execute(
                conn,
                f"SELECT COUNT(*) AS n FROM {self._table('node_attempts')} WHERE run_id = :run_id "
                "AND node_name = :node_name AND phase = :phase AND phase_epoch = :phase_epoch "
                "AND outcome IS NOT NULL",
                {"run_id": run_id, "node_name": node_name, "phase": phase, "phase_epoch": phase_epoch},
            )
            attempt_number = _fetchone_dict(cur)["n"] + 1
            execution_key = f"{run_id}:{phase}:{phase_epoch}:{node_name}:{attempt_number}"
            attempt_id = _attempt_id(execution_key)

            self._execute(
                conn,
                f"MERGE INTO {self._table('node_attempts')} t "
                "USING (SELECT :execution_key AS execution_key) s "
                "ON t.execution_key = s.execution_key "
                "WHEN NOT MATCHED THEN INSERT (attempt_id, execution_key, run_id, phase, phase_epoch, "
                "node_index, node_name, attempt_number, started_at, state_version_before) "
                "VALUES (:attempt_id, :execution_key, :run_id, :phase, :phase_epoch, :node_index, "
                ":node_name, :attempt_number, :started_at, :state_version_before)",
                {
                    "execution_key": execution_key,
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "phase": phase,
                    "phase_epoch": phase_epoch,
                    "node_index": node_index,
                    "node_name": node_name,
                    "attempt_number": attempt_number,
                    "started_at": now,
                    "state_version_before": state_version_before,
                },
            )
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('node_attempts')} WHERE execution_key = :execution_key",
                {"execution_key": execution_key},
            )
            return _fetchone_dict(cur)

    def complete_node_attempt(
        self,
        execution_key: str,
        *,
        outcome: str,
        now: str,
        state_version_after: int | None = None,
        result_state_json: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"UPDATE {self._table('node_attempts')} SET outcome = :outcome, completed_at = :now, "
                "state_version_after = :state_version_after, result_state_json = :result_state_json, "
                "error_detail = :error_detail WHERE execution_key = :execution_key AND outcome IS NULL",
                {
                    "outcome": outcome,
                    "now": now,
                    "state_version_after": state_version_after,
                    "result_state_json": result_state_json,
                    "error_detail": error_detail,
                    "execution_key": execution_key,
                },
            )
            if _num_affected_rows(cur) == 0:
                cur2 = self._execute(
                    conn,
                    f"SELECT outcome FROM {self._table('node_attempts')} WHERE execution_key = :execution_key",
                    {"execution_key": execution_key},
                )
                row = _fetchone_dict(cur2)
                if row is None:
                    raise AttemptNotFound(execution_key)
                if row["outcome"] == outcome:
                    return
                raise AttemptAlreadyClosed(execution_key, row["outcome"], outcome)

    def list_node_attempts(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('node_attempts')} WHERE run_id = :run_id "
                "ORDER BY started_at, attempt_number",
                {"run_id": run_id},
            )
            return _fetchall_dicts(cur)

    def close_open_attempts(
        self, run_id: str, *, outcome: str, now: str, error_detail: str | None = None
    ) -> None:
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"UPDATE {self._table('node_attempts')} SET outcome = :outcome, completed_at = :now, "
                "error_detail = :error_detail WHERE run_id = :run_id AND outcome IS NULL",
                {"outcome": outcome, "now": now, "error_detail": error_detail, "run_id": run_id},
            )

    # ── trace events ─────────────────────────────────────────────────────────

    def append_trace_event(self, event: dict) -> None:
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"MERGE INTO {self._table('trace_events')} t "
                "USING (SELECT :event_id AS event_id) s ON t.event_id = s.event_id "
                "WHEN NOT MATCHED THEN INSERT (event_id, run_id, engagement_id, event_type, event_time, "
                "stage, status, message, duration_s, node_name, execution_key, actor, state_version) "
                "VALUES (:event_id, :run_id, :engagement_id, :event_type, :event_time, :stage, :status, "
                ":message, :duration_s, :node_name, :execution_key, :actor, :state_version)",
                {
                    "event_id": event["event_id"],
                    "run_id": event["run_id"],
                    "engagement_id": event.get("engagement_id"),
                    "event_type": event["event_type"],
                    "event_time": event["event_time"],
                    "stage": event["stage"],
                    "status": event["status"],
                    "message": event["message"],
                    "duration_s": event.get("duration_s"),
                    "node_name": event.get("node_name"),
                    "execution_key": event.get("execution_key"),
                    "actor": event["actor"],
                    "state_version": event.get("state_version"),
                },
            )

    def list_trace_events(self, run_id: str | None = None) -> list[dict]:
        with self._cursor_ctx() as conn:
            if run_id is None:
                cur = self._execute(conn, f"SELECT * FROM {self._table('trace_events')} ORDER BY event_time")
            else:
                cur = self._execute(
                    conn,
                    f"SELECT * FROM {self._table('trace_events')} WHERE run_id = :run_id ORDER BY event_time",
                    {"run_id": run_id},
                )
            rows = _fetchall_dicts(cur)
        return [_trace_event_ui_shape(r) for r in rows]

    # ── run listing ──────────────────────────────────────────────────────────

    def list_runs(self, filters: dict | None = None) -> list[dict]:
        # run_state is the system of record for status (CLAUDE.md §9C/B4): join it in
        # and report ITS status, never the (possibly lagging) runs projection.
        clauses = []
        params: dict = {}
        filters = filters or {}
        for col in ("skill_id", "run_kind"):
            if filters.get(col):
                clauses.append(f"r.{col} = :{col}")
                params[col] = filters[col]
        if filters.get("status"):
            clauses.append("rs.status = :status")
            params["status"] = filters["status"]
        sql_text = (
            f"SELECT r.*, rs.status AS rs_status FROM {self._table('runs')} r "
            f"JOIN {self._table('run_state')} rs ON r.run_id = rs.run_id"
        )
        if clauses:
            sql_text += " WHERE " + " AND ".join(clauses)
        sql_text += " ORDER BY r.created_at DESC"
        with self._cursor_ctx() as conn:
            cur = self._execute(conn, sql_text, params)
            rows = _fetchall_dicts(cur)
        results = []
        for r in rows:
            d = dict(r)
            d["status"] = d.pop("rs_status")
            results.append(d)
        return results

    def find_runs(self, statuses: list[str]) -> list[str]:
        if not statuses:
            return []
        placeholders = ", ".join(f":s{i}" for i in range(len(statuses)))
        params = {f"s{i}": s for i, s in enumerate(statuses)}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn, f"SELECT run_id FROM {self._table('run_state')} WHERE status IN ({placeholders})", params
            )
            return [r["run_id"] for r in _fetchall_dicts(cur)]


def _with_version(state: RunState, version: int) -> RunState:
    import dataclasses

    return dataclasses.replace(state, state_version=version)


def _trace_event_ui_shape(row: dict) -> dict:
    row = dict(row)
    row["timestamp"] = row.pop("event_time")
    return row


class _CursorCtx:
    def __init__(self, persistence: DeltaPersistence):
        self._p = persistence

    def __enter__(self):
        self._p._conn_lock.acquire()
        return self._p._get_connection_locked()

    def __exit__(self, exc_type, exc, tb):
        self._p._conn_lock.release()
        return False
