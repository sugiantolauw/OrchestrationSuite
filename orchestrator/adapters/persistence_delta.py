from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orchestrator.config import Settings
from orchestrator.errors import (
    FingerprintConflict,
    RunAlreadyExists,
    RunNotFound,
    StaleStateError,
)
from orchestrator.migrations import plan_migrations, split_statements
from orchestrator.state import RunState, assert_json_safe, from_json, to_json, validate

_DEFAULT_DDL_DIR = Path(__file__).resolve().parent.parent / "ddl" / "delta"

_CONCURRENCY_MARKERS = (
    "ConcurrentAppend",
    "ConcurrentDelete",
    "ConcurrentTransaction",
    "DELTA_CONCURRENT",
)

_FINGERPRINT_COLUMNS = (
    "source_table_versions",
    "uploaded_file_hashes",
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attempt_id(execution_key: str) -> str:
    return hashlib.sha256(execution_key.encode("utf-8")).hexdigest()[:32]


def _is_concurrency_error(exc: Exception) -> bool:
    text = str(exc)
    return any(marker in text for marker in _CONCURRENCY_MARKERS)


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


def _num_affected_rows(cursor) -> int:
    row = cursor.fetchone()
    if row is None:
        return 0
    try:
        return int(row.num_affected_rows)
    except AttributeError:
        pass
    try:
        columns = [d[0] for d in cursor.description]
        idx = columns.index("num_affected_rows")
        return int(row[idx])
    except (ValueError, IndexError, TypeError):
        return int(row[0])


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

    def _cursor_ctx(self):
        return _CursorCtx(self._connection_factory())

    def _execute(self, conn, sql_text: str, params: dict | None = None):
        cur = conn.cursor()
        cur.execute(sql_text, params or {})
        return cur

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
                    self._execute(conn, stmt)
                self._execute(
                    conn,
                    f"INSERT INTO {self._table('schema_migrations')} "
                    "(version, description, checksum, applied_at) VALUES (:version, :description, :checksum, :applied_at)",
                    {
                        "version": m.version,
                        "description": m.description,
                        "checksum": m.checksum,
                        "applied_at": _now_iso(),
                    },
                )
                newly_applied.append(m.version)
            return newly_applied

    # ── runs / run_state ─────────────────────────────────────────────────────

    def create_run(self, state: RunState, fingerprint: dict) -> RunState:
        validate(state)
        assert_json_safe(state)
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn, f"SELECT run_id FROM {self._table('runs')} WHERE run_id = :run_id", {"run_id": state.run_id}
            )
            if _fetchone_dict(cur) is not None:
                raise RunAlreadyExists(state.run_id)

            self._upsert_fingerprint(conn, fingerprint)

            new_state = _with_version(state, 1)
            audit_start, audit_end = new_state.audit_period
            self._execute(
                conn,
                f"INSERT INTO {self._table('runs')} (run_id, run_kind, engagement_id, skill_id, "
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
            self._execute(
                conn,
                f"INSERT INTO {self._table('run_state')} (run_id, state_version, state_json, updated_at) "
                "VALUES (:run_id, :state_version, :state_json, :updated_at)",
                {
                    "run_id": new_state.run_id,
                    "state_version": new_state.state_version,
                    "state_json": to_json(new_state),
                    "updated_at": _now_iso(),
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
                "uploaded_file_hashes, skill_content_hash, code_revision, dependency_lock_hash, "
                "runtime_config_hash, endpoint_config, prompt_template_version, created_at) "
                "VALUES (:fingerprint_id, :source_table_versions, :uploaded_file_hashes, "
                ":skill_content_hash, :code_revision, :dependency_lock_hash, :runtime_config_hash, "
                ":endpoint_config, :prompt_template_version, :created_at)",
                {
                    "fingerprint_id": fingerprint_id,
                    "source_table_versions": fingerprint["source_table_versions"],
                    "uploaded_file_hashes": fingerprint["uploaded_file_hashes"],
                    "skill_content_hash": fingerprint.get("skill_content_hash"),
                    "code_revision": fingerprint["code_revision"],
                    "dependency_lock_hash": fingerprint["dependency_lock_hash"],
                    "runtime_config_hash": fingerprint["runtime_config_hash"],
                    "endpoint_config": fingerprint["endpoint_config"],
                    "prompt_template_version": fingerprint["prompt_template_version"],
                    "created_at": fingerprint.get("created_at") or _now_iso(),
                },
            )
            return
        differing = [c for c in _FINGERPRINT_COLUMNS if row.get(c) != fingerprint.get(c)]
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
            try:
                cur = self._execute(
                    conn,
                    f"UPDATE {self._table('run_state')} SET state_version = :new_version, "
                    "state_json = :state_json, updated_at = :updated_at "
                    "WHERE run_id = :run_id AND state_version = :expected",
                    {
                        "new_version": new_state.state_version,
                        "state_json": new_json,
                        "updated_at": _now_iso(),
                        "run_id": state.run_id,
                        "expected": expected,
                    },
                )
                affected = _num_affected_rows(cur)
            except Exception as exc:
                if _is_concurrency_error(exc):
                    affected = 0
                else:
                    raise

            if affected == 0:
                cur = self._execute(
                    conn,
                    f"SELECT state_version FROM {self._table('run_state')} WHERE run_id = :run_id",
                    {"run_id": state.run_id},
                )
                actual_row = _fetchone_dict(cur)
                actual = actual_row["state_version"] if actual_row else None
                raise StaleStateError(state.run_id, expected, actual)

            audit_start, audit_end = new_state.audit_period
            set_clause = ", ".join(f"{c} = :{c}" for c in _RUN_STATE_SUMMARY_COLUMNS)
            params = {c: getattr(new_state, c) for c in _RUN_STATE_SUMMARY_COLUMNS}
            params.update(
                {
                    "audit_period_start": audit_start,
                    "audit_period_end": audit_end,
                    "new_state_version": new_state.state_version,
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
            return new_state

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

    # ── node attempts ────────────────────────────────────────────────────────

    def begin_node_attempt(
        self,
        *,
        run_id: str,
        phase: str,
        node_index: int,
        node_name: str,
        state_version_before: int,
        now: str,
    ) -> dict:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('node_attempts')} WHERE run_id = :run_id AND node_name = :node_name "
                "AND outcome IS NULL",
                {"run_id": run_id, "node_name": node_name},
            )
            open_row = _fetchone_dict(cur)
            if open_row is not None:
                return open_row

            cur = self._execute(
                conn,
                f"SELECT COUNT(*) AS n FROM {self._table('node_attempts')} WHERE run_id = :run_id "
                "AND node_name = :node_name AND outcome IS NOT NULL",
                {"run_id": run_id, "node_name": node_name},
            )
            attempt_number = _fetchone_dict(cur)["n"] + 1
            execution_key = f"{run_id}:{node_name}:{attempt_number}"
            attempt_id = _attempt_id(execution_key)

            self._execute(
                conn,
                f"MERGE INTO {self._table('node_attempts')} t "
                "USING (SELECT :execution_key AS execution_key) s "
                "ON t.execution_key = s.execution_key "
                "WHEN NOT MATCHED THEN INSERT (attempt_id, execution_key, run_id, phase, node_index, "
                "node_name, attempt_number, started_at, state_version_before) "
                "VALUES (:attempt_id, :execution_key, :run_id, :phase, :node_index, :node_name, "
                ":attempt_number, :started_at, :state_version_before)",
                {
                    "execution_key": execution_key,
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "phase": phase,
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
            self._execute(
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
        clauses = []
        params: dict = {}
        filters = filters or {}
        for col in ("status", "skill_id", "run_kind"):
            if filters.get(col):
                clauses.append(f"{col} = :{col}")
                params[col] = filters[col]
        sql_text = f"SELECT * FROM {self._table('runs')}"
        if clauses:
            sql_text += " WHERE " + " AND ".join(clauses)
        sql_text += " ORDER BY created_at DESC"
        with self._cursor_ctx() as conn:
            cur = self._execute(conn, sql_text, params)
            return _fetchall_dicts(cur)

    def find_runs(self, statuses: list[str]) -> list[str]:
        if not statuses:
            return []
        placeholders = ", ".join(f":s{i}" for i in range(len(statuses)))
        params = {f"s{i}": s for i, s in enumerate(statuses)}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn, f"SELECT run_id FROM {self._table('runs')} WHERE status IN ({placeholders})", params
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
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        close = getattr(self._conn, "close", None)
        if close is not None:
            close()
        return False
