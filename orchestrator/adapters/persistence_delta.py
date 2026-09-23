from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from orchestrator.config import Settings
from orchestrator.errors import (
    AttemptAlreadyClosed,
    AttemptNotFound,
    FindingNotFound,
    FingerprintConflict,
    InvalidReviewStateTransition,
    NonDraftFindingWouldBeDeleted,
    RiskStatusRegression,
    RunAlreadyExists,
    RunNotFound,
    SkillVersionConflict,
    StaleStateError,
)
from orchestrator.migrations import plan_migrations, split_statements
from orchestrator.state import RunState, assert_json_safe, from_json, to_json, validate
from orchestrator.timeutil import normalise_ts, utc_now

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

_REVIEW_STATE_ORDER = ("draft", "prepared", "reviewed", "approved")

_FINDING_COLUMNS = (
    "finding_id", "run_id", "engagement_id", "rule_id", "skill_id", "skill_version",
    "test_id", "control_id", "risk_id", "assertion", "title", "severity", "severity_rule",
    "threshold_refs_json", "proposed_severity", "proposed_severity_reason",
    "metrics_cited_json", "evidence_refs_json", "observation", "recommendation",
    "management_questions_json", "exposure_amount", "exposure_basis", "theme_id",
    "review_state", "prior_finding_id", "recurrence_count", "created_at", "updated_at",
)

# On a re-write of a finding_id that already exists (a node overwriting its own prior
# output, CLAUDE.md §2.3 rule 1), finding_id is the merge key (never a SET target) and
# the rest are left as previously stored rather than reset to the incoming value:
# review_state/prior_finding_id/recurrence_count belong to the review lifecycle and
# rollforward matching (P7/§4.8), not to the engine's finding dict, and created_at is
# the finding's first-seen timestamp.
_FINDING_STICKY_COLUMNS = ("finding_id", "review_state", "prior_finding_id", "recurrence_count", "created_at")
_FINDING_UPDATE_COLUMNS = tuple(c for c in _FINDING_COLUMNS if c not in _FINDING_STICKY_COLUMNS)


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _metric_value_columns(value) -> tuple[float | None, str | None]:
    if isinstance(value, bool):
        return None, _canonical_json(value)
    if isinstance(value, (int, float)):
        return float(value), None
    return None, _canonical_json(value)


def _metric_dict_from_row(row: dict) -> dict:
    value = row["value"] if row["value"] is not None else (
        json.loads(row["value_text"]) if row.get("value_text") is not None else None
    )
    return {
        "value": value,
        "unit": row.get("unit"),
        "source_ref": json.loads(row["source_ref_json"]) if row.get("source_ref_json") else {},
        "test_id": row.get("test_id"),
    }


def _add_seconds(ts: str, seconds: float) -> str:
    dt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return normalise_ts(dt + _dt.timedelta(seconds=seconds))


def _finding_row_values(
    run_id: str, finding: dict, *, engagement_id, skill_id, skill_version, now: str
) -> dict:
    return {
        "finding_id": finding["finding_id"],
        "run_id": run_id,
        "engagement_id": engagement_id,
        "rule_id": finding["rule_id"],
        "skill_id": skill_id,
        "skill_version": skill_version,
        "test_id": finding.get("test_id"),
        "control_id": finding.get("control_id"),
        "risk_id": finding.get("risk_id"),
        "assertion": finding.get("assertion"),
        "title": finding["title"],
        "severity": finding["severity"],
        "severity_rule": finding.get("severity_rule"),
        "threshold_refs_json": _canonical_json(finding.get("threshold_refs", [])),
        "proposed_severity": finding.get("proposed_severity"),
        "proposed_severity_reason": finding.get("proposed_severity_reason"),
        "metrics_cited_json": _canonical_json(finding.get("metrics_cited", {})),
        "evidence_refs_json": _canonical_json(finding.get("evidence_refs", [])),
        "observation": finding.get("observation"),
        "recommendation": finding.get("recommendation"),
        "management_questions_json": _canonical_json(finding.get("management_questions", [])),
        "exposure_amount": finding.get("exposure_amount"),
        "exposure_basis": finding.get("exposure_basis"),
        "theme_id": finding.get("theme_id"),
        "review_state": finding.get("review_state", "draft"),
        "prior_finding_id": finding.get("prior_finding_id"),
        "recurrence_count": finding.get("recurrence_count", 0),
        "created_at": now,
        "updated_at": now,
    }


def _finding_dict_from_row(row: dict) -> dict:
    d = dict(row)
    d["threshold_refs"] = json.loads(d.pop("threshold_refs_json") or "[]")
    d["metrics_cited"] = json.loads(d.pop("metrics_cited_json") or "{}")
    d["evidence_refs"] = json.loads(d.pop("evidence_refs_json") or "[]")
    d["management_questions"] = json.loads(d.pop("management_questions_json") or "[]")
    return d


def _skill_version_dict_from_row(row: dict) -> dict:
    d = dict(row)
    d["content"] = json.loads(d.pop("content_json"))
    return d


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


def _normalise_value(value):
    # The driver hands back native datetime.datetime/datetime.date objects for
    # TIMESTAMP/DATE columns (verified live, CLAUDE.md §9B Layer 4); LocalPersistence's
    # sqlite backend stores and returns plain strings for the same columns. Normalise
    # here so both backends hand callers the same canonical string shape (CLAUDE.md
    # §4.1 P1A gate review) rather than leaking a driver-specific type into the shared
    # contract. datetime is a subclass of date, so it must be checked first.
    if isinstance(value, _dt.datetime):
        return normalise_ts(value)
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value


def _row_to_dict(cursor, row) -> dict:
    columns = [d[0] for d in cursor.description]
    return {col: _normalise_value(val) for col, val in zip(columns, row)}


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

    # ── skill versions (P2) ──────────────────────────────────────────────────

    def record_skill_version(
        self,
        *,
        skill_id: str,
        version: str,
        content_hash: str,
        content: dict,
        created_by: str,
        now: str,
        status: str = "draft",
    ) -> dict:
        content_json = _canonical_json(content)
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('skill_versions')} WHERE skill_id = :skill_id "
                "AND version = :version",
                {"skill_id": skill_id, "version": version},
            )
            row = _fetchone_dict(cur)
            if row is not None:
                if row["content_hash"] != content_hash:
                    raise SkillVersionConflict(skill_id, version, row["content_hash"], content_hash)
                return _skill_version_dict_from_row(row)
            self._execute(
                conn,
                f"INSERT INTO {self._table('skill_versions')} (skill_id, version, content_hash, "
                "content_json, status, created_by, created_at) VALUES (:skill_id, :version, "
                ":content_hash, :content_json, :status, :created_by, :created_at)",
                {
                    "skill_id": skill_id, "version": version, "content_hash": content_hash,
                    "content_json": content_json, "status": status, "created_by": created_by,
                    "created_at": now,
                },
            )
        return {
            "skill_id": skill_id, "version": version, "content_hash": content_hash,
            "status": status, "created_by": created_by, "created_at": now,
            "reviewed_by": None, "published_by": None, "published_at": None,
            "superseded_by": None, "surface2_results_json": None, "content": content,
        }

    def get_skill_version(self, skill_id: str, version: str) -> dict | None:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('skill_versions')} WHERE skill_id = :skill_id "
                "AND version = :version",
                {"skill_id": skill_id, "version": version},
            )
            row = _fetchone_dict(cur)
        return _skill_version_dict_from_row(row) if row is not None else None

    def list_skill_versions(self, skill_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('skill_versions')} WHERE skill_id = :skill_id "
                "ORDER BY version",
                {"skill_id": skill_id},
            )
            rows = _fetchall_dicts(cur)
        return [_skill_version_dict_from_row(r) for r in rows]

    # ── risk / control register (P2) ─────────────────────────────────────────

    def upsert_risks(self, risks: list[dict], *, now: str) -> None:
        with self._cursor_ctx() as conn:
            for risk in risks:
                risk_id = risk["risk_id"]
                cur = self._execute(
                    conn,
                    f"SELECT status FROM {self._table('risks')} WHERE risk_id = :risk_id",
                    {"risk_id": risk_id},
                )
                existing = _fetchone_dict(cur)
                if existing is not None:
                    if existing["status"] in ("accepted", "rejected") and risk["status"] == "proposed":
                        raise RiskStatusRegression(risk_id, existing["status"], risk["status"])
                    self._execute(
                        conn,
                        f"UPDATE {self._table('risks')} SET engagement_id=:engagement_id, "
                        "title=:title, description=:description, category=:category, "
                        "owner=:owner, status=:status, source=:source, source_ref=:source_ref, "
                        "as_of_date=:as_of_date, confidence=:confidence, "
                        "prior_risk_id=:prior_risk_id WHERE risk_id = :risk_id",
                        {
                            "engagement_id": risk.get("engagement_id"), "title": risk["title"],
                            "description": risk.get("description"), "category": risk.get("category"),
                            "owner": risk.get("owner"), "status": risk["status"],
                            "source": risk["source"], "source_ref": risk.get("source_ref"),
                            "as_of_date": risk.get("as_of_date"), "confidence": risk.get("confidence"),
                            "prior_risk_id": risk.get("prior_risk_id"), "risk_id": risk_id,
                        },
                    )
                else:
                    self._execute(
                        conn,
                        f"INSERT INTO {self._table('risks')} (risk_id, engagement_id, title, "
                        "description, category, owner, status, source, source_ref, as_of_date, "
                        "confidence, prior_risk_id, created_at) VALUES (:risk_id, :engagement_id, "
                        ":title, :description, :category, :owner, :status, :source, :source_ref, "
                        ":as_of_date, :confidence, :prior_risk_id, :created_at)",
                        {
                            "risk_id": risk_id, "engagement_id": risk.get("engagement_id"),
                            "title": risk["title"], "description": risk.get("description"),
                            "category": risk.get("category"), "owner": risk.get("owner"),
                            "status": risk["status"], "source": risk["source"],
                            "source_ref": risk.get("source_ref"), "as_of_date": risk.get("as_of_date"),
                            "confidence": risk.get("confidence"),
                            "prior_risk_id": risk.get("prior_risk_id"),
                            "created_at": risk.get("created_at") or now,
                        },
                    )

    def upsert_controls(self, controls: list[dict], *, now: str) -> None:
        with self._cursor_ctx() as conn:
            for control in controls:
                control_id = control["control_id"]
                cur = self._execute(
                    conn,
                    f"SELECT control_id FROM {self._table('controls')} WHERE control_id = :control_id",
                    {"control_id": control_id},
                )
                existing = _fetchone_dict(cur)
                if existing is not None:
                    self._execute(
                        conn,
                        f"UPDATE {self._table('controls')} SET risk_id=:risk_id, "
                        "engagement_id=:engagement_id, title=:title, description=:description, "
                        "type=:type, frequency=:frequency, owner=:owner, "
                        "design_conclusion=:design_conclusion, "
                        "operating_conclusion=:operating_conclusion WHERE control_id = :control_id",
                        {
                            "risk_id": control.get("risk_id"), "engagement_id": control.get("engagement_id"),
                            "title": control["title"], "description": control.get("description"),
                            "type": control.get("type"), "frequency": control.get("frequency"),
                            "owner": control.get("owner"),
                            "design_conclusion": control.get("design_conclusion"),
                            "operating_conclusion": control.get("operating_conclusion"),
                            "control_id": control_id,
                        },
                    )
                else:
                    self._execute(
                        conn,
                        f"INSERT INTO {self._table('controls')} (control_id, risk_id, engagement_id, "
                        "title, description, type, frequency, owner, design_conclusion, "
                        "operating_conclusion, created_at) VALUES (:control_id, :risk_id, "
                        ":engagement_id, :title, :description, :type, :frequency, :owner, "
                        ":design_conclusion, :operating_conclusion, :created_at)",
                        {
                            "control_id": control_id, "risk_id": control.get("risk_id"),
                            "engagement_id": control.get("engagement_id"), "title": control["title"],
                            "description": control.get("description"), "type": control.get("type"),
                            "frequency": control.get("frequency"), "owner": control.get("owner"),
                            "design_conclusion": control.get("design_conclusion"),
                            "operating_conclusion": control.get("operating_conclusion"),
                            "created_at": control.get("created_at") or now,
                        },
                    )

    def list_risks(self, engagement_id: str | None = None) -> list[dict]:
        with self._cursor_ctx() as conn:
            if engagement_id is None:
                cur = self._execute(conn, f"SELECT * FROM {self._table('risks')} ORDER BY risk_id")
            else:
                cur = self._execute(
                    conn,
                    f"SELECT * FROM {self._table('risks')} WHERE engagement_id = :engagement_id "
                    "ORDER BY risk_id",
                    {"engagement_id": engagement_id},
                )
            return _fetchall_dicts(cur)

    def list_controls(self, engagement_id: str | None = None) -> list[dict]:
        with self._cursor_ctx() as conn:
            if engagement_id is None:
                cur = self._execute(conn, f"SELECT * FROM {self._table('controls')} ORDER BY control_id")
            else:
                cur = self._execute(
                    conn,
                    f"SELECT * FROM {self._table('controls')} WHERE engagement_id = :engagement_id "
                    "ORDER BY control_id",
                    {"engagement_id": engagement_id},
                )
            return _fetchall_dicts(cur)

    # ── findings (P2) ────────────────────────────────────────────────────────

    def write_findings(
        self,
        run_id: str,
        findings: list[dict],
        *,
        engagement_id,
        skill_id,
        skill_version,
        now: str,
    ) -> list[dict]:
        new_ids = {f["finding_id"] for f in findings}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT finding_id, review_state FROM {self._table('findings')} "
                "WHERE run_id = :run_id",
                {"run_id": run_id},
            )
            existing_rows = _fetchall_dicts(cur)
            orphans = [r for r in existing_rows if r["finding_id"] not in new_ids]
            # Checked BEFORE any write, since Delta statements here are not wrapped in
            # a multi-statement transaction -- a rejected call must leave data
            # untouched, which only a precondition check (not a rollback) can
            # guarantee on this backend.
            blocking = [o["finding_id"] for o in orphans if o["review_state"] != "draft"]
            if blocking:
                raise NonDraftFindingWouldBeDeleted(run_id, blocking)

            update_set = ", ".join(f"{c} = :{c}" for c in _FINDING_UPDATE_COLUMNS)
            insert_cols = ", ".join(_FINDING_COLUMNS)
            insert_vals = ", ".join(f":{c}" for c in _FINDING_COLUMNS)
            merge_sql = (
                f"MERGE INTO {self._table('findings')} t "
                "USING (SELECT :finding_id AS finding_id) s ON t.finding_id = s.finding_id "
                f"WHEN MATCHED THEN UPDATE SET {update_set} "
                f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
            )
            for finding in findings:
                values = _finding_row_values(
                    run_id, finding, engagement_id=engagement_id, skill_id=skill_id,
                    skill_version=skill_version, now=now,
                )
                self._execute(conn, merge_sql, values)

            to_delete = [o["finding_id"] for o in orphans]
            if to_delete:
                placeholders = ", ".join(f":fid{i}" for i in range(len(to_delete)))
                params = {f"fid{i}": fid for i, fid in enumerate(to_delete)}
                params["run_id"] = run_id
                self._execute(
                    conn,
                    f"DELETE FROM {self._table('findings')} WHERE run_id = :run_id "
                    f"AND finding_id IN ({placeholders})",
                    params,
                )
        return self.list_findings(run_id)

    def list_findings(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('findings')} WHERE run_id = :run_id ORDER BY "
                "CASE severity WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 WHEN 'Low' THEN 2 ELSE 3 END, "
                "rule_id",
                {"run_id": run_id},
            )
            rows = _fetchall_dicts(cur)
        return [_finding_dict_from_row(r) for r in rows]

    def set_finding_review_state(self, finding_id: str, *, to_state: str, actor: str, now: str) -> dict:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('findings')} WHERE finding_id = :finding_id",
                {"finding_id": finding_id},
            )
            row = _fetchone_dict(cur)
            if row is None:
                raise FindingNotFound(finding_id)
            current = row["review_state"]
            if (
                current not in _REVIEW_STATE_ORDER
                or to_state not in _REVIEW_STATE_ORDER
                or _REVIEW_STATE_ORDER.index(to_state) != _REVIEW_STATE_ORDER.index(current) + 1
            ):
                raise InvalidReviewStateTransition(finding_id, current, to_state)
            self._execute(
                conn,
                f"UPDATE {self._table('findings')} SET review_state = :review_state, "
                "updated_at = :updated_at WHERE finding_id = :finding_id",
                {"review_state": to_state, "updated_at": now, "finding_id": finding_id},
            )
            updated = dict(row)
            updated["review_state"] = to_state
            updated["updated_at"] = now
        return _finding_dict_from_row(updated)

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

    # ── P3 run outputs ───────────────────────────────────────────────────────

    def write_flagged_rows(self, run_id: str, rows: list[dict]) -> None:
        with self._cursor_ctx() as conn:
            self._execute(
                conn, f"DELETE FROM {self._table('flagged_rows')} WHERE run_id = :run_id", {"run_id": run_id}
            )
            for r in rows:
                self._execute(
                    conn,
                    f"INSERT INTO {self._table('flagged_rows')} (run_id, source, row_key, flag, group_id) "
                    "VALUES (:run_id, :source, :row_key, :flag, :group_id)",
                    {
                        "run_id": run_id, "source": r["source"], "row_key": r["row_key"],
                        "flag": r["flag"], "group_id": r.get("group_id"),
                    },
                )

    def list_flagged_rows(self, run_id: str, flag: str | None = None) -> list[dict]:
        with self._cursor_ctx() as conn:
            if flag is None:
                cur = self._execute(
                    conn,
                    f"SELECT * FROM {self._table('flagged_rows')} WHERE run_id = :run_id "
                    "ORDER BY source, row_key, flag",
                    {"run_id": run_id},
                )
            else:
                cur = self._execute(
                    conn,
                    f"SELECT * FROM {self._table('flagged_rows')} WHERE run_id = :run_id AND flag = :flag "
                    "ORDER BY source, row_key",
                    {"run_id": run_id, "flag": flag},
                )
            return _fetchall_dicts(cur)

    def write_run_metrics(self, run_id: str, metrics: list[dict]) -> None:
        with self._cursor_ctx() as conn:
            self._execute(
                conn, f"DELETE FROM {self._table('run_metrics')} WHERE run_id = :run_id", {"run_id": run_id}
            )
            for m in metrics:
                value, value_text = _metric_value_columns(m.get("value"))
                self._execute(
                    conn,
                    f"INSERT INTO {self._table('run_metrics')} (run_id, metric_name, value, value_text, "
                    "unit, source_ref_json, test_id) VALUES (:run_id, :metric_name, :value, :value_text, "
                    ":unit, :source_ref_json, :test_id)",
                    {
                        "run_id": run_id,
                        "metric_name": m["metric_name"],
                        "value": value,
                        "value_text": value_text,
                        "unit": m.get("unit"),
                        "source_ref_json": _canonical_json(m.get("source_ref", {})),
                        "test_id": m.get("test_id"),
                    },
                )

    def get_run_metrics(self, run_id: str) -> dict[str, dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('run_metrics')} WHERE run_id = :run_id ORDER BY metric_name",
                {"run_id": run_id},
            )
            rows = _fetchall_dicts(cur)
        return {r["metric_name"]: _metric_dict_from_row(r) for r in rows}

    def write_issues_for_findings(
        self, run_id: str, findings: list[dict], *, engagement_id, now: str
    ) -> list[dict]:
        created: list[dict] = []
        with self._cursor_ctx() as conn:
            for finding in findings:
                issue_id = f"ISS-{finding['finding_id']}"
                cur = self._execute(
                    conn,
                    f"SELECT issue_id FROM {self._table('issues')} WHERE issue_id = :issue_id",
                    {"issue_id": issue_id},
                )
                if _fetchone_dict(cur) is not None:
                    continue
                self._execute(
                    conn,
                    f"INSERT INTO {self._table('issues')} (issue_id, engagement_id, rule_id, title, "
                    "description, rating, status, raised_by, raised_at, owner, due_date, "
                    "remediation_plan, management_response, prior_issue_id, finding_ids_json, "
                    "run_ids_json, created_at, updated_at) VALUES (:issue_id, :engagement_id, "
                    ":rule_id, :title, :description, :rating, :status, NULL, NULL, NULL, NULL, "
                    "NULL, NULL, NULL, :finding_ids_json, :run_ids_json, :created_at, :updated_at)",
                    {
                        "issue_id": issue_id,
                        "engagement_id": engagement_id,
                        "rule_id": finding.get("rule_id"),
                        "title": finding["title"],
                        "description": finding.get("observation"),
                        "rating": finding.get("severity"),
                        "status": "draft",
                        "finding_ids_json": _canonical_json([finding["finding_id"]]),
                        "run_ids_json": _canonical_json([run_id]),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                created.append({"issue_id": issue_id, "finding_id": finding["finding_id"]})
        return created

    def write_management_actions(self, run_id: str, actions: list[dict], *, now: str) -> None:
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"DELETE FROM {self._table('management_actions')} WHERE run_id = :run_id",
                {"run_id": run_id},
            )
            for a in actions:
                self._execute(
                    conn,
                    f"INSERT INTO {self._table('management_actions')} (action_id, issue_id, "
                    "finding_id, run_id, engagement_id, skill_id, title, description, owner, risk, "
                    "status, target_date, potential_exposure, evidence_link, created_at, "
                    "last_updated) VALUES (:action_id, :issue_id, :finding_id, :run_id, "
                    ":engagement_id, :skill_id, :title, :description, :owner, :risk, :status, "
                    ":target_date, :potential_exposure, :evidence_link, :created_at, :last_updated)",
                    {
                        "action_id": a["action_id"],
                        "issue_id": a.get("issue_id"),
                        "finding_id": a.get("finding_id"),
                        "run_id": run_id,
                        "engagement_id": a.get("engagement_id"),
                        "skill_id": a.get("skill_id"),
                        "title": a["title"],
                        "description": a.get("description"),
                        "owner": a.get("owner"),
                        "risk": a.get("risk"),
                        "status": a.get("status", "draft"),
                        "target_date": a.get("target_date"),
                        "potential_exposure": a.get("potential_exposure"),
                        "evidence_link": a.get("evidence_link"),
                        "created_at": now,
                        "last_updated": now,
                    },
                )

    def list_management_actions(self, filters: dict | None = None) -> list[dict]:
        filters = filters or {}
        clauses = []
        params: dict = {}
        for col in ("run_id", "skill_id", "engagement_id", "status"):
            if filters.get(col):
                clauses.append(f"ma.{col} = :{col}")
                params[col] = filters[col]
        sql_text = (
            f"SELECT ma.*, f.title AS finding_title, f.observation AS finding_observation "
            f"FROM {self._table('management_actions')} ma "
            f"LEFT JOIN {self._table('findings')} f ON f.finding_id = ma.finding_id"
        )
        if clauses:
            sql_text += " WHERE " + " AND ".join(clauses)
        sql_text += " ORDER BY ma.created_at DESC"
        with self._cursor_ctx() as conn:
            cur = self._execute(conn, sql_text, params)
            return _fetchall_dicts(cur)

    def record_export(
        self, run_id: str, kind: str, *, path: str, sha256: str, created_by: str, now: str
    ) -> dict:
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"MERGE INTO {self._table('exports')} t "
                "USING (SELECT :run_id AS run_id, :kind AS kind) s "
                "ON t.run_id = s.run_id AND t.kind = s.kind "
                "WHEN MATCHED THEN UPDATE SET path = :path, sha256 = :sha256, "
                "created_at = :created_at, created_by = :created_by "
                "WHEN NOT MATCHED THEN INSERT (run_id, kind, path, sha256, created_at, created_by) "
                "VALUES (:run_id, :kind, :path, :sha256, :created_at, :created_by)",
                {
                    "run_id": run_id, "kind": kind, "path": path, "sha256": sha256,
                    "created_at": now, "created_by": created_by,
                },
            )
        return {
            "run_id": run_id, "kind": kind, "path": path, "sha256": sha256,
            "created_at": now, "created_by": created_by,
        }

    def list_exports(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('exports')} WHERE run_id = :run_id ORDER BY kind",
                {"run_id": run_id},
            )
            return _fetchall_dicts(cur)

    # ── leases (CLAUDE.md §9C P1A concurrency foundation, §2.3 rule 3) ──────

    def acquire_lease(self, run_id: str, worker_id: str, *, ttl_s: float, now: str) -> bool:
        expires_at = _add_seconds(now, ttl_s)
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT claimed_by, lease_expires_at FROM {self._table('run_leases')} "
                "WHERE run_id = :run_id",
                {"run_id": run_id},
            )
            row = _fetchone_dict(cur)
            if row is None:
                self._execute(
                    conn,
                    f"INSERT INTO {self._table('run_leases')} (run_id, claimed_by, claimed_at, "
                    "heartbeat_at, lease_expires_at) VALUES (:run_id, :claimed_by, :now, :now, "
                    ":expires_at)",
                    {"run_id": run_id, "claimed_by": worker_id, "now": now, "expires_at": expires_at},
                )
                return True
            if row["lease_expires_at"] <= now:
                self._execute(
                    conn,
                    f"UPDATE {self._table('run_leases')} SET claimed_by = :claimed_by, "
                    "claimed_at = :now, heartbeat_at = :now, lease_expires_at = :expires_at "
                    "WHERE run_id = :run_id AND lease_expires_at = :prior_expiry",
                    {
                        "claimed_by": worker_id, "now": now, "expires_at": expires_at,
                        "run_id": run_id, "prior_expiry": row["lease_expires_at"],
                    },
                )
                cur2 = self._execute(
                    conn,
                    f"SELECT claimed_by FROM {self._table('run_leases')} WHERE run_id = :run_id",
                    {"run_id": run_id},
                )
                return _fetchone_dict(cur2)["claimed_by"] == worker_id
            return False

    def renew_lease(self, run_id: str, worker_id: str, *, ttl_s: float, now: str) -> bool:
        expires_at = _add_seconds(now, ttl_s)
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"UPDATE {self._table('run_leases')} SET heartbeat_at = :now, "
                "lease_expires_at = :expires_at WHERE run_id = :run_id AND claimed_by = :claimed_by",
                {"now": now, "expires_at": expires_at, "run_id": run_id, "claimed_by": worker_id},
            )
            return _num_affected_rows(cur) > 0

    def release_lease(self, run_id: str, worker_id: str) -> None:
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"DELETE FROM {self._table('run_leases')} WHERE run_id = :run_id AND claimed_by = :claimed_by",
                {"run_id": run_id, "claimed_by": worker_id},
            )

    def expired_leases(self, now: str) -> list[str]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT run_id FROM {self._table('run_leases')} WHERE lease_expires_at <= :now",
                {"now": now},
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
