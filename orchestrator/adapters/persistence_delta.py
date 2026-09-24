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
    "analyst_set_severity", "severity_basis", "monetary_basis",
    # P6 (docs/specs/P6_narration_design.md §6.1, migration 011): an AI-proposed
    # finding accepted at sign-off (CLAUDE.md §3 NN2 amendment) -- origin
    # distinguishes it from a rule finding, candidate_id traces it back to its
    # finding_candidates row, accepted_by/accepted_at record the decision.
    "origin", "candidate_id", "accepted_by", "accepted_at",
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


def _typed_params(params: dict | None):
    # databricks-sql-connector's default dict-param path (cur.execute(sql, {...}))
    # already infers a Python float as its DoubleParameter (CAST_EXPR "DOUBLE"),
    # verified live to round-trip exactly for every value this adapter writes
    # (467063.73, 0.1+0.2, 1e-7, 123456789.01 all come back bit-identical). But
    # that inference is implicit and version-dependent -- the P2/P3 gate review
    # found monetary values already corrupted to float32 precision in Delta,
    # which this driver version does not reproduce but an earlier or future one
    # could. Bind every float explicitly as DoubleParameter (never the driver's
    # default inference, and never FloatParameter/32-bit) so the DOUBLE cast is
    # asserted in code, not assumed from the installed driver's behaviour.
    # Every dict entry must carry an explicit name in the list form (the
    # connector requires ALL parameters named to use `:name` markers, not `?`),
    # so non-float values are also wrapped via the driver's own primitive
    # inference rather than left bare.
    if not params:
        return {}
    from databricks.sql.parameters.native import DoubleParameter, dbsql_parameter_from_primitive

    out = []
    for name, value in params.items():
        if isinstance(value, float):
            out.append(DoubleParameter(value=value, name=name))
        else:
            out.append(dbsql_parameter_from_primitive(value=value, name=name))
    return out


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
        # CLAUDE.md §0.4 / G8, P2/P3 gate review item 3: whether this finding's
        # severity rests on an analyst-set (not policy-referenced) threshold, and
        # whether severity came from a threshold at all -- build_findings
        # (orchestrator/findings.py) already computes both; only the write path
        # was dropping them. No default value invented here (`.get` with no
        # fallback -> None when a caller omits it) -- readers (XLSX export, UI)
        # raise rather than defaulting False when the persisted value is None.
        "analyst_set_severity": finding.get("analyst_set_severity"),
        "severity_basis": finding.get("severity_basis"),
        "monetary_basis": finding.get("monetary_basis"),
        # P6 §5.2/§6.1: a rule finding omits these (None -> NULL); `finalise`
        # supplies all four for an accepted AI-proposed finding. No default
        # invented here either.
        "origin": finding.get("origin"),
        "candidate_id": finding.get("candidate_id"),
        "accepted_by": finding.get("accepted_by"),
        "accepted_at": finding.get("accepted_at"),
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


# ── P6 narration (docs/specs/P6_narration_design.md §6.1, migration 011) ──────────

_NARRATIVE_COLUMNS = (
    "narrative_id", "run_id", "engagement_id", "target_kind", "target_id", "field",
    "version", "generation", "origin", "template_text", "sources_json", "call_ids_json",
    "served_model_version", "violations_json", "updated_by", "updated_at",
)

_NARRATIVE_EDIT_COLUMNS = (
    "edit_id", "narrative_id", "run_id", "version", "origin", "action", "actor", "at",
    "before_text", "after_text", "diff", "reason", "call_id",
)

_CANDIDATE_COLUMNS = (
    "candidate_id", "run_id", "engagement_id", "skill_id", "generation", "rule_id",
    "title", "metrics_cited_json", "producing_test_ids_json", "proposed_severity",
    "severity_reason", "rationale", "monetary_basis", "monetary_basis_note",
    "exposure_amount", "headline_eligible", "headline_ineligible_reason",
    "candidate_status", "decided_by", "decided_at", "decision_reason",
    "decided_severity", "call_id", "created_at", "updated_at",
)
# A re-upsert of an existing candidate_id (a re-executed `narrate` node, §5.1/§5.2)
# never resets the auditor's decision or the row's first-seen timestamp -- same
# sticky-column discipline as _FINDING_STICKY_COLUMNS above.
_CANDIDATE_STICKY_COLUMNS = (
    "candidate_id", "candidate_status", "decided_by", "decided_at", "decision_reason",
    "decided_severity", "created_at",
)
_CANDIDATE_UPDATE_COLUMNS = tuple(c for c in _CANDIDATE_COLUMNS if c not in _CANDIDATE_STICKY_COLUMNS)

_THEME_COLUMNS = ("theme_id", "run_id", "generation", "ordinal", "finding_ids_json", "superseded", "created_at")


def _narrative_row_values(row: dict) -> dict:
    return {
        "narrative_id": row["narrative_id"],
        "run_id": row["run_id"],
        "engagement_id": row.get("engagement_id"),
        "target_kind": row["target_kind"],
        "target_id": row["target_id"],
        "field": row["field"],
        "version": row["version"],
        "generation": row["generation"],
        "origin": row["origin"],
        "template_text": row.get("template_text"),
        "sources_json": _canonical_json(row.get("sources", [])),
        "call_ids_json": _canonical_json(row.get("call_ids", [])),
        "served_model_version": row.get("served_model_version"),
        "violations_json": _canonical_json(row["violations"]) if row.get("violations") is not None else None,
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


def _narrative_dict_from_row(row: dict) -> dict:
    d = dict(row)
    d["sources"] = json.loads(d.pop("sources_json") or "[]")
    d["call_ids"] = json.loads(d.pop("call_ids_json") or "[]")
    violations_json = d.pop("violations_json")
    d["violations"] = json.loads(violations_json) if violations_json is not None else None
    return d


def _candidate_row_values(run_id: str, candidate: dict, *, now: str) -> dict:
    return {
        "candidate_id": candidate["candidate_id"],
        "run_id": run_id,
        "engagement_id": candidate.get("engagement_id"),
        "skill_id": candidate.get("skill_id"),
        "generation": candidate["generation"],
        "rule_id": candidate["rule_id"],
        "title": candidate["title"],
        "metrics_cited_json": _canonical_json(candidate.get("metrics_cited", [])),
        "producing_test_ids_json": _canonical_json(candidate.get("producing_test_ids", [])),
        "proposed_severity": candidate["proposed_severity"],
        "severity_reason": candidate.get("severity_reason"),
        "rationale": candidate.get("rationale"),
        "monetary_basis": candidate["monetary_basis"],
        "monetary_basis_note": candidate.get("monetary_basis_note"),
        "exposure_amount": candidate.get("exposure_amount"),
        "headline_eligible": bool(candidate["headline_eligible"]),
        "headline_ineligible_reason": candidate.get("headline_ineligible_reason"),
        "candidate_status": candidate.get("candidate_status", "candidate"),
        "decided_by": candidate.get("decided_by"),
        "decided_at": candidate.get("decided_at"),
        "decision_reason": candidate.get("decision_reason"),
        "decided_severity": candidate.get("decided_severity"),
        "call_id": candidate["call_id"],
        "created_at": now,
        "updated_at": now,
    }


def _candidate_dict_from_row(row: dict) -> dict:
    d = dict(row)
    d["metrics_cited"] = json.loads(d.pop("metrics_cited_json") or "[]")
    d["producing_test_ids"] = json.loads(d.pop("producing_test_ids_json") or "[]")
    return d


def _theme_row_values(run_id: str, theme: dict, *, now: str) -> dict:
    return {
        "theme_id": theme["theme_id"],
        "run_id": run_id,
        "generation": theme["generation"],
        "ordinal": theme["ordinal"],
        "finding_ids_json": _canonical_json(theme.get("finding_ids", [])),
        "superseded": bool(theme.get("superseded", False)),
        "created_at": now,
    }


def _theme_dict_from_row(row: dict) -> dict:
    d = dict(row)
    d["finding_ids"] = json.loads(d.pop("finding_ids_json") or "[]")
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


# Rows per batched MERGE statement for write_flagged_rows/write_run_metrics
# (CLAUDE.md build brief P3 §1): a per-row MERGE -- one round trip per row,
# each 2-8s against a live SQL warehouse (CLAUDE.md §11 recorded latency) --
# made a several-hundred-row execute() write take 20+ minutes live against
# the deployed App. One MERGE per batch, its USING clause a literal VALUES
# list, cuts that to one round trip per _MERGE_BATCH_SIZE rows while keeping
# the same MERGE + prune upsert shape (never DELETE-then-INSERT).
_MERGE_BATCH_SIZE = 250

_LLM_CALLS_COLUMNS = (
    "call_id", "run_id", "engagement_id", "node_name", "execution_key",
    "task", "seq", "transport_attempt", "endpoint_role", "endpoint",
    "served_model_version", "source", "cache_hit", "cache_key",
    "cached_from_call_id", "version_changed", "prompt_template_id",
    "prompt_template_version", "prompt_sha256", "messages_json",
    "params_sent_json", "params_withheld_json", "response_text",
    "reasoning_parts_stripped", "finish_reason", "prompt_tokens",
    "completion_tokens", "total_tokens", "latency_ms", "request_id",
    "outcome", "error_type", "error_status_code", "error_message",
    "pii_columns_masked_json", "pii_whitelist_json", "actor", "created_at",
)


def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


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

    # Every read/write above passes a plain dict, which the driver's own
    # dbsql_parameter_from_primitive infers per-value (a Python float already
    # infers as DoubleParameter -- CAST_EXPR "DOUBLE" -- verified live to
    # round-trip 467063.73, 0.1+0.2, 1e-7 and 123456789.01 bit-exactly with the
    # pinned driver version). _execute_typed is used only for the monetary/
    # metric write paths the P2/P3 gate review flagged, so every float there is
    # bound via an EXPLICIT DoubleParameter in code rather than relying on that
    # implicit, driver-version-dependent inference (never FloatParameter/
    # 32-bit). It cannot replace `_execute` everywhere: the connector's named-
    # parameter list form requires every entry to carry `.name`, and the
    # FakeConnection test harness for CAS/state-machine paths (tests/
    # test_delta_sql.py) asserts on a plain params dict, so those call sites
    # keep using `_execute` unchanged.
    def _execute_typed(self, conn, sql_text: str, params: dict | None = None):
        prepared = _typed_params(params)
        try:
            cur = conn.cursor()
            cur.execute(sql_text, prepared)
            return cur
        except Exception as exc:
            if _is_connection_error(exc):
                self._conn = None
                conn = self._get_connection_locked()
                cur = conn.cursor()
                cur.execute(sql_text, prepared)
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
        # approved_by (P1B's projection column, CLAUDE.md §4.8) has no
        # matching RunState attribute -- it is derived from state.signoff,
        # which the sign_off gate sets BEFORE calling transition() (CLAUDE.md
        # §2.4). Without this it stayed NULL on every completed run forever
        # (found live: P2/P3 gate review item 9).
        approved_by = (state.signoff or {}).get("approver")
        set_clause = ", ".join(f"{c} = :{c}" for c in _RUN_STATE_SUMMARY_COLUMNS)
        params = {c: getattr(state, c) for c in _RUN_STATE_SUMMARY_COLUMNS}
        params.update(
            {
                "audit_period_start": audit_start,
                "audit_period_end": audit_end,
                "approved_by": approved_by,
                "new_state_version": state.state_version,
                "run_id": state.run_id,
            }
        )
        self._execute(
            conn,
            f"UPDATE {self._table('runs')} SET {set_clause}, audit_period_start = :audit_period_start, "
            "audit_period_end = :audit_period_end, approved_by = :approved_by, "
            "state_version = :new_state_version "
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

    def get_fingerprints(self, fingerprint_ids: list[str]) -> dict[str, dict]:
        if not fingerprint_ids:
            return {}
        placeholders = ", ".join(f":f{i}" for i in range(len(fingerprint_ids)))
        params = {f"f{i}": fid for i, fid in enumerate(fingerprint_ids)}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('run_fingerprints')} WHERE fingerprint_id IN ({placeholders})",
                params,
            )
            rows = _fetchall_dicts(cur)
        return {r["fingerprint_id"]: r for r in rows}

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
                # exposure_amount is money (CLAUDE.md P2/P3 gate review item 1) -- bind
                # it as an explicit DOUBLE rather than the driver's implicit inference.
                self._execute_typed(conn, merge_sql, values)

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

    def list_findings_for_runs(self, run_ids: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {rid: [] for rid in run_ids}
        if not run_ids:
            return out
        placeholders = ", ".join(f":r{i}" for i in range(len(run_ids)))
        params = {f"r{i}": rid for i, rid in enumerate(run_ids)}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('findings')} WHERE run_id IN ({placeholders}) ORDER BY run_id, "
                "CASE severity WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 WHEN 'Low' THEN 2 ELSE 3 END, "
                "rule_id",
                params,
            )
            rows = _fetchall_dicts(cur)
        for r in rows:
            d = _finding_dict_from_row(r)
            out[d["run_id"]].append(d)
        return out

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
        # MERGE (upsert) + prune, the same pattern write_findings uses (CLAUDE.md
        # build brief P3 §1) -- never DELETE-then-INSERT, which leaves a window
        # where this run's flagged_rows are entirely absent to any concurrent
        # reader (a drill-down query, a re-admitted G6 pass) between the DELETE
        # committing and the first INSERT landing. Batched (_MERGE_BATCH_SIZE
        # rows' USING clause per MERGE) rather than one MERGE per row -- see
        # that constant's docstring for why.
        new_keys = {(r["source"], r["row_key"], r["flag"]) for r in rows}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT source, row_key, flag FROM {self._table('flagged_rows')} WHERE run_id = :run_id",
                {"run_id": run_id},
            )
            existing_keys = {(r["source"], r["row_key"], r["flag"]) for r in _fetchall_dicts(cur)}

            for batch in _batched(rows, _MERGE_BATCH_SIZE):
                values_sql = ", ".join(
                    f"(:run_id, :s{i}, :rk{i}, :f{i}, :g{i})" for i in range(len(batch))
                )
                params: dict = {"run_id": run_id}
                for i, r in enumerate(batch):
                    params[f"s{i}"] = r["source"]
                    params[f"rk{i}"] = r["row_key"]
                    params[f"f{i}"] = r["flag"]
                    params[f"g{i}"] = r.get("group_id")
                # Spark SQL rejects a direct "USING (...) AS s(col, ...)" column
                # alias list on MERGE ([COLUMN_ALIASES_NOT_ALLOWED]) -- name the
                # VALUES columns (Spark's own default col1, col2, ...) via an
                # inner SELECT instead, and alias only the SELECT itself as `s`.
                merge_sql = (
                    f"MERGE INTO {self._table('flagged_rows')} t "
                    "USING (SELECT col1 AS run_id, col2 AS source, col3 AS row_key, "
                    f"col4 AS flag, col5 AS group_id FROM (VALUES {values_sql})) s "
                    "ON t.run_id = s.run_id AND t.source = s.source AND t.row_key = s.row_key AND t.flag = s.flag "
                    "WHEN MATCHED THEN UPDATE SET group_id = s.group_id "
                    "WHEN NOT MATCHED THEN INSERT (run_id, source, row_key, flag, group_id) "
                    "VALUES (s.run_id, s.source, s.row_key, s.flag, s.group_id)"
                )
                self._execute(conn, merge_sql, params)

            orphans = existing_keys - new_keys
            if orphans:
                clauses = []
                params: dict = {"run_id": run_id}
                for i, (source, row_key, flag) in enumerate(orphans):
                    clauses.append(f"(source = :s{i} AND row_key = :rk{i} AND flag = :f{i})")
                    params[f"s{i}"] = source
                    params[f"rk{i}"] = row_key
                    params[f"f{i}"] = flag
                self._execute(
                    conn,
                    f"DELETE FROM {self._table('flagged_rows')} WHERE run_id = :run_id AND ({' OR '.join(clauses)})",
                    params,
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
        # MERGE + prune (see write_flagged_rows above) -- a re-executed execute/
        # prioritise node's metrics are never absent, even momentarily, to a
        # concurrent get_run_payload/get_run_frames read. Batched, same reason
        # as write_flagged_rows.
        new_names = {m["metric_name"] for m in metrics}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT metric_name FROM {self._table('run_metrics')} WHERE run_id = :run_id",
                {"run_id": run_id},
            )
            existing_names = {r["metric_name"] for r in _fetchall_dicts(cur)}

            for batch in _batched(metrics, _MERGE_BATCH_SIZE):
                values_sql = ", ".join(
                    f"(:run_id, :n{i}, :v{i}, :vt{i}, :u{i}, :sr{i}, :t{i})" for i in range(len(batch))
                )
                params: dict = {"run_id": run_id}
                for i, m in enumerate(batch):
                    value, value_text = _metric_value_columns(m.get("value"))
                    params[f"n{i}"] = m["metric_name"]
                    params[f"v{i}"] = value
                    params[f"vt{i}"] = value_text
                    params[f"u{i}"] = m.get("unit")
                    params[f"sr{i}"] = _canonical_json(m.get("source_ref", {}))
                    params[f"t{i}"] = m.get("test_id")
                # See write_flagged_rows above for why the VALUES columns are
                # named via an inner SELECT rather than a column-alias list
                # directly on MERGE's USING clause.
                merge_sql = (
                    f"MERGE INTO {self._table('run_metrics')} t "
                    "USING (SELECT col1 AS run_id, col2 AS metric_name, col3 AS value, "
                    "col4 AS value_text, col5 AS unit, col6 AS source_ref_json, col7 AS test_id "
                    f"FROM (VALUES {values_sql})) s "
                    "ON t.run_id = s.run_id AND t.metric_name = s.metric_name "
                    "WHEN MATCHED THEN UPDATE SET value = s.value, value_text = s.value_text, "
                    "unit = s.unit, source_ref_json = s.source_ref_json, test_id = s.test_id "
                    "WHEN NOT MATCHED THEN INSERT (run_id, metric_name, value, value_text, unit, "
                    "source_ref_json, test_id) VALUES (s.run_id, s.metric_name, s.value, "
                    "s.value_text, s.unit, s.source_ref_json, s.test_id)"
                )
                # metric value is money/count data (CLAUDE.md P2/P3 gate review item 1)
                # -- bind it as an explicit DOUBLE rather than the driver's inference.
                self._execute_typed(conn, merge_sql, params)

            orphans = existing_names - new_names
            if orphans:
                placeholders = ", ".join(f":m{i}" for i in range(len(orphans)))
                params = {f"m{i}": name for i, name in enumerate(orphans)}
                params["run_id"] = run_id
                self._execute(
                    conn,
                    f"DELETE FROM {self._table('run_metrics')} WHERE run_id = :run_id "
                    f"AND metric_name IN ({placeholders})",
                    params,
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

    def put_test_line_values(self, run_id: str, rows: list[dict]) -> None:
        # P6 WP N4: a minimal write-through for the `test_line_values` table
        # (migration 011) ahead of N3b's own full persistence-contract
        # methods for it (docs/specs/P6_narration_design.md WP N3b) -- MERGE
        # + prune, the same pattern write_flagged_rows/write_run_metrics use
        # (never DELETE-then-INSERT, which leaves a window where this run's
        # test_line_values are entirely absent to a concurrent reader).
        # Batched (_MERGE_BATCH_SIZE rows' USING clause per MERGE), typed
        # (spend_amount/excess_amount are money -- CLAUDE.md P2/P3 gate
        # review item 1 -- bound as an explicit DOUBLE, never the driver's
        # implicit inference).
        new_keys = {(r["test_id"], r["source"], r["row_key"]) for r in rows}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT test_id, source, row_key FROM {self._table('test_line_values')} "
                "WHERE run_id = :run_id",
                {"run_id": run_id},
            )
            existing_keys = {(r["test_id"], r["source"], r["row_key"]) for r in _fetchall_dicts(cur)}

            for batch in _batched(rows, _MERGE_BATCH_SIZE):
                values_sql = ", ".join(
                    f"(:run_id, :t{i}, :s{i}, :rk{i}, :lk{i}, :sa{i}, :ea{i})" for i in range(len(batch))
                )
                params: dict = {"run_id": run_id}
                for i, r in enumerate(batch):
                    params[f"t{i}"] = r["test_id"]
                    params[f"s{i}"] = r["source"]
                    params[f"rk{i}"] = r["row_key"]
                    params[f"lk{i}"] = r["line_key"]
                    params[f"sa{i}"] = float(r["spend_amount"])
                    excess = r.get("excess_amount")
                    params[f"ea{i}"] = float(excess) if excess is not None else None
                merge_sql = (
                    f"MERGE INTO {self._table('test_line_values')} t "
                    "USING (SELECT col1 AS run_id, col2 AS test_id, col3 AS source, col4 AS row_key, "
                    "col5 AS line_key, col6 AS spend_amount, col7 AS excess_amount "
                    f"FROM (VALUES {values_sql})) s "
                    "ON t.run_id = s.run_id AND t.test_id = s.test_id AND t.source = s.source "
                    "AND t.row_key = s.row_key "
                    "WHEN MATCHED THEN UPDATE SET line_key = s.line_key, spend_amount = s.spend_amount, "
                    "excess_amount = s.excess_amount "
                    "WHEN NOT MATCHED THEN INSERT (run_id, test_id, source, row_key, line_key, "
                    "spend_amount, excess_amount) VALUES (s.run_id, s.test_id, s.source, s.row_key, "
                    "s.line_key, s.spend_amount, s.excess_amount)"
                )
                self._execute_typed(conn, merge_sql, params)

            orphans = existing_keys - new_keys
            if orphans:
                clauses = []
                params = {"run_id": run_id}
                for i, (test_id, source, row_key) in enumerate(orphans):
                    clauses.append(f"(test_id = :t{i} AND source = :s{i} AND row_key = :rk{i})")
                    params[f"t{i}"] = test_id
                    params[f"s{i}"] = source
                    params[f"rk{i}"] = row_key
                self._execute(
                    conn,
                    f"DELETE FROM {self._table('test_line_values')} WHERE run_id = :run_id "
                    f"AND ({' OR '.join(clauses)})",
                    params,
                )

    def get_run_metrics_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, dict]]:
        out: dict[str, dict[str, dict]] = {rid: {} for rid in run_ids}
        if not run_ids:
            return out
        placeholders = ", ".join(f":r{i}" for i in range(len(run_ids)))
        params = {f"r{i}": rid for i, rid in enumerate(run_ids)}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('run_metrics')} WHERE run_id IN ({placeholders}) "
                "ORDER BY run_id, metric_name",
                params,
            )
            rows = _fetchall_dicts(cur)
        for r in rows:
            out[r["run_id"]][r["metric_name"]] = _metric_dict_from_row(r)
        return out

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
        # MERGE + prune (see write_flagged_rows above) -- action_id is
        # deterministic per finding_id (nodes/fieldwork.py's act()), so a
        # re-executed act node upserts by that id instead of ever leaving
        # management_actions momentarily empty for this run.
        new_ids = {a["action_id"] for a in actions}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT action_id FROM {self._table('management_actions')} WHERE run_id = :run_id",
                {"run_id": run_id},
            )
            existing_ids = {r["action_id"] for r in _fetchall_dicts(cur)}

            merge_sql = (
                f"MERGE INTO {self._table('management_actions')} t "
                "USING (SELECT :action_id AS action_id) s ON t.action_id = s.action_id "
                "WHEN MATCHED THEN UPDATE SET issue_id = :issue_id, finding_id = :finding_id, "
                "run_id = :run_id, engagement_id = :engagement_id, skill_id = :skill_id, "
                "title = :title, description = :description, owner = :owner, risk = :risk, "
                "status = :status, target_date = :target_date, "
                "potential_exposure = :potential_exposure, evidence_link = :evidence_link, "
                "last_updated = :last_updated, description_origin = :description_origin "
                "WHEN NOT MATCHED THEN INSERT (action_id, issue_id, finding_id, run_id, "
                "engagement_id, skill_id, title, description, owner, risk, status, target_date, "
                "potential_exposure, evidence_link, created_at, last_updated, description_origin) "
                "VALUES (:action_id, :issue_id, :finding_id, :run_id, :engagement_id, :skill_id, "
                ":title, :description, :owner, :risk, :status, :target_date, :potential_exposure, "
                ":evidence_link, :created_at, :last_updated, :description_origin)"
            )
            for a in actions:
                # potential_exposure is money (CLAUDE.md P2/P3 gate review item 1) --
                # bind it as an explicit DOUBLE rather than the driver's inference.
                self._execute_typed(
                    conn,
                    merge_sql,
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
                        # P6 §6.1: template|model|human -- no default invented (None ->
                        # NULL) for an action write that predates this field or omits it.
                        "description_origin": a.get("description_origin"),
                    },
                )

            orphans = existing_ids - new_ids
            if orphans:
                placeholders = ", ".join(f":a{i}" for i in range(len(orphans)))
                params = {f"a{i}": aid for i, aid in enumerate(orphans)}
                params["run_id"] = run_id
                self._execute(
                    conn,
                    f"DELETE FROM {self._table('management_actions')} WHERE run_id = :run_id "
                    f"AND action_id IN ({placeholders})",
                    params,
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

    def list_management_actions_for_runs(self, run_ids: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {rid: [] for rid in run_ids}
        if not run_ids:
            return out
        placeholders = ", ".join(f":r{i}" for i in range(len(run_ids)))
        params = {f"r{i}": rid for i, rid in enumerate(run_ids)}
        sql_text = (
            f"SELECT ma.*, f.title AS finding_title, f.observation AS finding_observation "
            f"FROM {self._table('management_actions')} ma "
            f"LEFT JOIN {self._table('findings')} f ON f.finding_id = ma.finding_id "
            f"WHERE ma.run_id IN ({placeholders}) ORDER BY ma.run_id, ma.created_at DESC"
        )
        with self._cursor_ctx() as conn:
            cur = self._execute(conn, sql_text, params)
            rows = _fetchall_dicts(cur)
        for r in rows:
            out[r["run_id"]].append(r)
        return out

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

    # ── narration (P6, docs/specs/P6_narration_design.md §6.1 / WP N3b) ────────

    def upsert_narrative(self, row: dict) -> None:
        values = _narrative_row_values(row)
        update_set = ", ".join(f"{c} = :{c}" for c in _NARRATIVE_COLUMNS if c != "narrative_id")
        insert_cols = ", ".join(_NARRATIVE_COLUMNS)
        insert_vals = ", ".join(f":{c}" for c in _NARRATIVE_COLUMNS)
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"MERGE INTO {self._table('narratives')} t "
                "USING (SELECT :narrative_id AS narrative_id) s ON t.narrative_id = s.narrative_id "
                f"WHEN MATCHED THEN UPDATE SET {update_set} "
                f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})",
                values,
            )

    def get_narratives(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('narratives')} WHERE run_id = :run_id "
                "ORDER BY target_kind, target_id, field",
                {"run_id": run_id},
            )
            rows = _fetchall_dicts(cur)
        return [_narrative_dict_from_row(r) for r in rows]

    def append_narrative_edit(self, row: dict) -> None:
        # G14 append-only: MERGE with only a WHEN NOT MATCHED clause -- a retried
        # write of the same edit_id is a no-op, never an update (this table is
        # never rewritten, only ever grown). Same shape as put_llm_cache_if_absent.
        insert_cols = ", ".join(_NARRATIVE_EDIT_COLUMNS)
        insert_vals = ", ".join(f":{c}" for c in _NARRATIVE_EDIT_COLUMNS)
        params = {c: row.get(c) for c in _NARRATIVE_EDIT_COLUMNS}
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"MERGE INTO {self._table('narrative_edits')} t "
                "USING (SELECT :edit_id AS edit_id) s ON t.edit_id = s.edit_id "
                f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})",
                params,
            )

    def list_narrative_edits(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('narrative_edits')} WHERE run_id = :run_id "
                "ORDER BY narrative_id, version",
                {"run_id": run_id},
            )
            return _fetchall_dicts(cur)

    def write_candidates(self, run_id: str, candidates: list[dict], *, now: str) -> None:
        # Upsert only -- never prunes and never deletes (§5.2: a superseded or
        # decided candidate stays in Delta forever). A re-upsert of an existing
        # candidate_id (an idempotent retry of `narrate` for the SAME generation,
        # CLAUDE.md §2.3 rule 1) leaves the decision columns untouched
        # (_CANDIDATE_STICKY_COLUMNS), the same discipline write_findings uses
        # for review-lifecycle columns. Looped rather than batched (§5.1 caps
        # candidates at NARRATION_MAX_CANDIDATES, default 3 -- never worth a
        # VALUES-list batch like write_flagged_rows/write_run_metrics).
        update_set = ", ".join(f"{c} = :{c}" for c in _CANDIDATE_UPDATE_COLUMNS)
        insert_cols = ", ".join(_CANDIDATE_COLUMNS)
        insert_vals = ", ".join(f":{c}" for c in _CANDIDATE_COLUMNS)
        merge_sql = (
            f"MERGE INTO {self._table('finding_candidates')} t "
            "USING (SELECT :candidate_id AS candidate_id) s ON t.candidate_id = s.candidate_id "
            f"WHEN MATCHED THEN UPDATE SET {update_set} "
            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
        )
        with self._cursor_ctx() as conn:
            for candidate in candidates:
                values = _candidate_row_values(run_id, candidate, now=now)
                # exposure_amount is money (CLAUDE.md P2/P3 gate review item 1) --
                # bind it as an explicit DOUBLE rather than the driver's inference.
                self._execute_typed(conn, merge_sql, values)

    def list_candidates(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('finding_candidates')} WHERE run_id = :run_id "
                "ORDER BY generation, rule_id",
                {"run_id": run_id},
            )
            rows = _fetchall_dicts(cur)
        return [_candidate_dict_from_row(r) for r in rows]

    def decide_candidate_cas(
        self,
        candidate_id: str,
        *,
        decision: str,
        reason: str | None,
        decided_severity: str | None,
        actor: str,
        now: str,
    ) -> bool:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"UPDATE {self._table('finding_candidates')} SET candidate_status = :decision, "
                "decided_by = :actor, decided_at = :now, decision_reason = :reason, "
                "decided_severity = :decided_severity, updated_at = :now "
                "WHERE candidate_id = :candidate_id AND candidate_status = 'candidate'",
                {
                    "decision": decision, "actor": actor, "now": now, "reason": reason,
                    "decided_severity": decided_severity, "candidate_id": candidate_id,
                },
            )
            return _num_affected_rows(cur) > 0

    def supersede_undecided(self, run_id: str, *, below_generation: int, now: str) -> int:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"UPDATE {self._table('finding_candidates')} SET candidate_status = 'superseded', "
                "updated_at = :now WHERE run_id = :run_id AND generation < :below_generation "
                "AND candidate_status = 'candidate'",
                {"now": now, "run_id": run_id, "below_generation": below_generation},
            )
            return _num_affected_rows(cur)

    def write_themes(self, run_id: str, themes: list[dict], *, now: str) -> None:
        # Upsert only, same never-prune/never-delete discipline as
        # write_candidates -- a prior generation's themes remain, distinguished
        # by generation/superseded, never overwritten out from under a caller
        # that only re-reads them by run_id. created_at is sticky (first-seen).
        update_cols = tuple(c for c in _THEME_COLUMNS if c not in ("theme_id", "created_at"))
        update_set = ", ".join(f"{c} = :{c}" for c in update_cols)
        insert_cols = ", ".join(_THEME_COLUMNS)
        insert_vals = ", ".join(f":{c}" for c in _THEME_COLUMNS)
        merge_sql = (
            f"MERGE INTO {self._table('finding_themes')} t "
            "USING (SELECT :theme_id AS theme_id) s ON t.theme_id = s.theme_id "
            f"WHEN MATCHED THEN UPDATE SET {update_set} "
            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
        )
        with self._cursor_ctx() as conn:
            for theme in themes:
                self._execute(conn, merge_sql, _theme_row_values(run_id, theme, now=now))

    def list_themes(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('finding_themes')} WHERE run_id = :run_id "
                "ORDER BY generation, ordinal",
                {"run_id": run_id},
            )
            rows = _fetchall_dicts(cur)
        return [_theme_dict_from_row(r) for r in rows]

    def put_test_line_values(self, run_id: str, rows: list[dict]) -> None:
        # MERGE + prune, batched (see write_flagged_rows above) -- overwrite
        # semantics: `rows` becomes this run's entire test_line_values set.
        new_keys = {(r["test_id"], r["source"], r["row_key"]) for r in rows}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT test_id, source, row_key FROM {self._table('test_line_values')} "
                "WHERE run_id = :run_id",
                {"run_id": run_id},
            )
            existing_keys = {(r["test_id"], r["source"], r["row_key"]) for r in _fetchall_dicts(cur)}

            for batch in _batched(rows, _MERGE_BATCH_SIZE):
                values_sql = ", ".join(
                    f"(:run_id, :t{i}, :s{i}, :rk{i}, :lk{i}, :sp{i}, :ex{i})" for i in range(len(batch))
                )
                params: dict = {"run_id": run_id}
                for i, r in enumerate(batch):
                    params[f"t{i}"] = r["test_id"]
                    params[f"s{i}"] = r["source"]
                    params[f"rk{i}"] = r["row_key"]
                    params[f"lk{i}"] = r["line_key"]
                    params[f"sp{i}"] = r["spend_amount"]
                    params[f"ex{i}"] = r.get("excess_amount")
                merge_sql = (
                    f"MERGE INTO {self._table('test_line_values')} t "
                    "USING (SELECT col1 AS run_id, col2 AS test_id, col3 AS source, "
                    "col4 AS row_key, col5 AS line_key, col6 AS spend_amount, col7 AS excess_amount "
                    f"FROM (VALUES {values_sql})) s "
                    "ON t.run_id = s.run_id AND t.test_id = s.test_id AND t.source = s.source "
                    "AND t.row_key = s.row_key "
                    "WHEN MATCHED THEN UPDATE SET line_key = s.line_key, "
                    "spend_amount = s.spend_amount, excess_amount = s.excess_amount "
                    "WHEN NOT MATCHED THEN INSERT (run_id, test_id, source, row_key, line_key, "
                    "spend_amount, excess_amount) VALUES (s.run_id, s.test_id, s.source, s.row_key, "
                    "s.line_key, s.spend_amount, s.excess_amount)"
                )
                # spend_amount/excess_amount are money (CLAUDE.md P2/P3 gate review
                # item 1) -- bind them as explicit DOUBLEs rather than the driver's
                # inference.
                self._execute_typed(conn, merge_sql, params)

            orphans = existing_keys - new_keys
            if orphans:
                clauses = []
                params = {"run_id": run_id}
                for i, (test_id, source, row_key) in enumerate(orphans):
                    clauses.append(f"(test_id = :t{i} AND source = :s{i} AND row_key = :rk{i})")
                    params[f"t{i}"] = test_id
                    params[f"s{i}"] = source
                    params[f"rk{i}"] = row_key
                self._execute(
                    conn,
                    f"DELETE FROM {self._table('test_line_values')} WHERE run_id = :run_id "
                    f"AND ({' OR '.join(clauses)})",
                    params,
                )

    def list_test_line_values(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('test_line_values')} WHERE run_id = :run_id "
                "ORDER BY test_id, source, row_key",
                {"run_id": run_id},
            )
            return _fetchall_dicts(cur)

    # ── uploaded files (P5) ──────────────────────────────────────────────────

    def record_uploaded_file(self, row: dict) -> dict:
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"INSERT INTO {self._table('uploaded_files')} (upload_id, engagement_id, filename, "
                "volume_path, size_bytes, sha256, uploaded_by, uploaded_at, status, row_count, "
                "columns_json, error) VALUES (:upload_id, :engagement_id, :filename, :volume_path, "
                ":size_bytes, :sha256, :uploaded_by, :uploaded_at, :status, :row_count, "
                ":columns_json, :error)",
                {
                    "upload_id": row["upload_id"], "engagement_id": row.get("engagement_id"),
                    "filename": row["filename"], "volume_path": row["volume_path"],
                    "size_bytes": row["size_bytes"], "sha256": row["sha256"],
                    "uploaded_by": row["uploaded_by"], "uploaded_at": row["uploaded_at"],
                    "status": row["status"], "row_count": row.get("row_count"),
                    "columns_json": row.get("columns_json"), "error": row.get("error"),
                },
            )
        return dict(row)

    def update_uploaded_file(self, upload_id: str, *, status: str, row_count: int | None = None,
                              columns_json: str | None = None, error: str | None = None) -> None:
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"UPDATE {self._table('uploaded_files')} SET status = :status, row_count = :row_count, "
                "columns_json = :columns_json, error = :error WHERE upload_id = :upload_id",
                {
                    "status": status, "row_count": row_count, "columns_json": columns_json,
                    "error": error, "upload_id": upload_id,
                },
            )

    def get_uploaded_file(self, upload_id: str) -> dict | None:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('uploaded_files')} WHERE upload_id = :upload_id",
                {"upload_id": upload_id},
            )
            rows = _fetchall_dicts(cur)
        return rows[0] if rows else None

    def list_uploaded_files(self, engagement_id: str | None = None) -> list[dict]:
        with self._cursor_ctx() as conn:
            if engagement_id:
                cur = self._execute(
                    conn,
                    f"SELECT * FROM {self._table('uploaded_files')} WHERE engagement_id = :engagement_id "
                    "ORDER BY uploaded_at DESC",
                    {"engagement_id": engagement_id},
                )
            else:
                cur = self._execute(
                    conn, f"SELECT * FROM {self._table('uploaded_files')} ORDER BY uploaded_at DESC"
                )
            return _fetchall_dicts(cur)

    # ── LLM call ledger (independent review 2026-09-24 item 3) ──────────────

    def record_llm_call(self, row: dict) -> None:
        params = {name: row.get(name) for name in _LLM_CALLS_COLUMNS}
        params["cache_hit"] = bool(params["cache_hit"])
        params["version_changed"] = bool(params["version_changed"])
        values_sql = ", ".join(f":{name}" for name in _LLM_CALLS_COLUMNS)
        update_sql = ", ".join(
            f"{name} = :{name}" for name in _LLM_CALLS_COLUMNS if name != "call_id"
        )
        with self._cursor_ctx() as conn:
            self._execute(
                conn,
                f"MERGE INTO {self._table('llm_calls')} t "
                "USING (SELECT :call_id AS call_id) s ON t.call_id = s.call_id "
                f"WHEN MATCHED THEN UPDATE SET {update_sql} "
                f"WHEN NOT MATCHED THEN INSERT ({', '.join(_LLM_CALLS_COLUMNS)}) VALUES ({values_sql})",
                params,
            )

    def last_live_version(self, endpoint: str) -> str | None:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT served_model_version FROM {self._table('llm_calls')} WHERE endpoint = :endpoint "
                "AND source = 'live' AND outcome = 'succeeded' ORDER BY created_at DESC LIMIT 1",
                {"endpoint": endpoint},
            )
            row = _fetchone_dict(cur)
        return row["served_model_version"] if row else None

    def get_llm_cache(self, cache_key: str) -> dict | None:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('llm_cache')} WHERE cache_key = :cache_key",
                {"cache_key": cache_key},
            )
            return _fetchone_dict(cur)

    def find_llm_cache(self, prompt_sha256: str, endpoint: str, params_json: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('llm_cache')} WHERE prompt_sha256 = :prompt_sha256 "
                "AND endpoint = :endpoint AND params_json = :params_json ORDER BY created_at DESC",
                {"prompt_sha256": prompt_sha256, "endpoint": endpoint, "params_json": params_json},
            )
            return _fetchall_dicts(cur)

    def put_llm_cache_if_absent(self, row: dict) -> bool:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"MERGE INTO {self._table('llm_cache')} t "
                "USING (SELECT :cache_key AS cache_key) s ON t.cache_key = s.cache_key "
                "WHEN NOT MATCHED THEN INSERT (cache_key, prompt_sha256, endpoint, "
                "served_model_version, params_json, response_text, finish_reason, usage_json, "
                "source_call_id, created_at) VALUES (:cache_key, :prompt_sha256, :endpoint, "
                ":served_model_version, :params_json, :response_text, :finish_reason, :usage_json, "
                ":source_call_id, :created_at)",
                row,
            )
            return _num_affected_rows(cur) > 0

    def list_llm_calls(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('llm_calls')} WHERE run_id = :run_id ORDER BY created_at",
                {"run_id": run_id},
            )
            return _fetchall_dicts(cur)

    def sum_llm_call_tokens_since(self, since_iso: str) -> int:
        # LIFECYCLE_design.md §2.6 monthly admission: one query, never
        # polled, at the start of a lifecycle run that calls a model.
        # created_at is a canonical ISO-8601 UTC string (fixed-width,
        # zero-padded), so a lexicographic >= comparison is a correct time
        # comparison without parsing.
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT COALESCE(SUM(total_tokens), 0) AS total FROM {self._table('llm_calls')} "
                "WHERE created_at >= :since",
                {"since": since_iso},
            )
            row = _fetchone_dict(cur)
        return int(row["total"]) if row else 0

    # ── row-level LLM classification (independent review item 4) ───────────

    def write_classification_results(self, run_id: str, rows: list[dict]) -> None:
        new_keys = {r["row_key"] for r in rows}
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT row_key FROM {self._table('t43_classifications')} WHERE run_id = :run_id",
                {"run_id": run_id},
            )
            existing_keys = {r["row_key"] for r in _fetchall_dicts(cur)}

            for batch in _batched(rows, _MERGE_BATCH_SIZE):
                values_sql = ", ".join(f"(:run_id, :k{i}, :pe{i}, :c{i}, :r{i}, :ci{i}, :t{i})" for i in range(len(batch)))
                params: dict = {"run_id": run_id}
                for i, r in enumerate(batch):
                    params[f"k{i}"] = r["row_key"]
                    params[f"pe{i}"] = bool(r["personal_expense"])
                    params[f"c{i}"] = float(r["confidence"])
                    params[f"r{i}"] = r.get("rationale")
                    params[f"ci{i}"] = r["call_id"]
                    params[f"t{i}"] = r["created_at"]
                self._execute(
                    conn,
                    f"MERGE INTO {self._table('t43_classifications')} t "
                    "USING (SELECT col1 AS run_id, col2 AS row_key, col3 AS personal_expense, "
                    "col4 AS confidence, col5 AS rationale, col6 AS call_id, col7 AS created_at "
                    f"FROM (VALUES {values_sql})) s "
                    "ON t.run_id = s.run_id AND t.row_key = s.row_key "
                    "WHEN MATCHED THEN UPDATE SET personal_expense = s.personal_expense, "
                    "confidence = s.confidence, rationale = s.rationale, call_id = s.call_id, "
                    "created_at = s.created_at "
                    "WHEN NOT MATCHED THEN INSERT (run_id, row_key, personal_expense, confidence, "
                    "rationale, call_id, created_at) VALUES (s.run_id, s.row_key, s.personal_expense, "
                    "s.confidence, s.rationale, s.call_id, s.created_at)",
                    params,
                )

            orphans = existing_keys - new_keys
            if orphans:
                placeholders = ", ".join(f":k{i}" for i in range(len(orphans)))
                params = {f"k{i}": key for i, key in enumerate(orphans)}
                params["run_id"] = run_id
                self._execute(
                    conn,
                    f"DELETE FROM {self._table('t43_classifications')} WHERE run_id = :run_id "
                    f"AND row_key IN ({placeholders})",
                    params,
                )

    def list_classification_results(self, run_id: str) -> list[dict]:
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"SELECT * FROM {self._table('t43_classifications')} WHERE run_id = :run_id ORDER BY row_key",
                {"run_id": run_id},
            )
            return _fetchall_dicts(cur)

    # ── leases (CLAUDE.md §9C P1A concurrency foundation, §2.3 rule 3) ──────

    def acquire_lease(self, run_id: str, worker_id: str, *, ttl_s: float, now: str) -> bool:
        # Item 6 (CLAUDE.md P2/P3 gate review): the old SELECT-then-INSERT/
        # UPDATE was non-atomic -- two workers could both see "no row"
        # (Delta has no enforced PRIMARY KEY, so both INSERTs would succeed,
        # leaving two owners) or both see "expired" and both believe their
        # own conditional UPDATE won. A single MERGE makes "no row" (INSERT)
        # and "row expired" (UPDATE) one atomic decision -- Delta's own
        # transaction protocol rejects a conflicting concurrent MERGE against
        # the same row rather than letting both succeed -- and ownership is
        # decided from num_affected_rows (this MERGE's own INSERT+UPDATE
        # count), never a separate SELECT after the write.
        expires_at = _add_seconds(now, ttl_s)
        with self._cursor_ctx() as conn:
            cur = self._execute(
                conn,
                f"MERGE INTO {self._table('run_leases')} t "
                "USING (SELECT :run_id AS run_id) s ON t.run_id = s.run_id "
                "WHEN MATCHED AND t.lease_expires_at <= :now THEN UPDATE SET "
                "claimed_by = :claimed_by, claimed_at = :now, heartbeat_at = :now, "
                "lease_expires_at = :expires_at "
                "WHEN NOT MATCHED THEN INSERT (run_id, claimed_by, claimed_at, heartbeat_at, "
                "lease_expires_at) VALUES (:run_id, :claimed_by, :now, :now, :expires_at)",
                {"run_id": run_id, "claimed_by": worker_id, "now": now, "expires_at": expires_at},
            )
            return _num_affected_rows(cur) > 0

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
