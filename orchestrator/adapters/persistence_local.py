from __future__ import annotations

import datetime
import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path

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
from orchestrator.migrations import plan_migrations
from orchestrator.state import RunState, assert_json_safe, from_json, to_json, validate
from orchestrator.timeutil import utc_now

logger = logging.getLogger(__name__)

_DEFAULT_DDL_DIR = Path(__file__).resolve().parent.parent / "ddl" / "sqlite"

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
)

# On a re-write of a finding_id that already exists (a node overwriting its own prior
# output, CLAUDE.md §2.3 rule 1), finding_id is the merge key (never a SET target) and
# the rest are left as previously stored rather than reset to the incoming value:
# review_state/prior_finding_id/recurrence_count belong to the review lifecycle and
# rollforward matching (P7/§4.8), not to the engine's finding dict, and created_at is
# the finding's first-seen timestamp.
_FINDING_STICKY_COLUMNS = ("finding_id", "review_state", "prior_finding_id", "recurrence_count", "created_at")
_FINDING_UPDATE_COLUMNS = tuple(c for c in _FINDING_COLUMNS if c not in _FINDING_STICKY_COLUMNS)


def _attempt_id(execution_key: str) -> str:
    return hashlib.sha256(execution_key.encode("utf-8")).hexdigest()[:32]


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _metric_value_columns(value) -> tuple[float | None, str | None]:
    # run_metrics stores a numeric `value` when the metric IS one (the common
    # case -- every build_metrics() kind except a non-numeric "value" metric),
    # and falls back to `value_text` (JSON-encoded) otherwise, so a metric can
    # never be silently coerced or dropped (CLAUDE.md NN14).
    if isinstance(value, bool):
        return None, _canonical_json(value)
    if isinstance(value, (int, float)):
        return float(value), None
    return None, _canonical_json(value)


def _metric_dict_from_row(row: dict) -> dict:
    value = row["value"] if row["value"] is not None else (
        json.loads(row["value_text"]) if row["value_text"] is not None else None
    )
    return {
        "value": value,
        "unit": row.get("unit"),
        "source_ref": json.loads(row["source_ref_json"]) if row.get("source_ref_json") else {},
        "test_id": row.get("test_id"),
    }


def _add_seconds(ts: str, seconds: float) -> str:
    from datetime import timedelta

    from orchestrator.timeutil import normalise_ts

    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return normalise_ts(dt + timedelta(seconds=seconds))


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
        # B2 (CLAUDE.md P2/P3 gate review): required on every finding
        # build_findings produces (findings.yaml's monetary_basis, schema-
        # enforced) -- no default invented here; a caller that omits it gets
        # a persisted None, same tri-state discipline as analyst_set_severity.
        "monetary_basis": finding.get("monetary_basis"),
    }


def _finding_dict_from_row(row: dict) -> dict:
    d = dict(row)
    d["threshold_refs"] = json.loads(d.pop("threshold_refs_json") or "[]")
    d["metrics_cited"] = json.loads(d.pop("metrics_cited_json") or "{}")
    d["evidence_refs"] = json.loads(d.pop("evidence_refs_json") or "[]")
    d["management_questions"] = json.loads(d.pop("management_questions_json") or "[]")
    # sqlite has no native BOOLEAN -- INSERT stores Python bool as INTEGER 0/1
    # (its int subclass adapts transparently), so coerce back to bool/None here
    # to match the Delta backend's native BOOLEAN column, rather than leaking a
    # driver-specific int into the shared contract.
    if d.get("analyst_set_severity") is not None:
        d["analyst_set_severity"] = bool(d["analyst_set_severity"])
    return d


def _skill_version_dict_from_row(row: dict) -> dict:
    d = dict(row)
    d["content"] = json.loads(d.pop("content_json"))
    return d


class LocalPersistence:
    def __init__(self, db_path: str, ddl_dir: Path | None = None):
        self.db_path = db_path
        self.ddl_dir = Path(ddl_dir) if ddl_dir else _DEFAULT_DDL_DIR
        self._is_memory = db_path == ":memory:"
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if self._is_memory:
            self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        if not self._is_memory:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _connect(self) -> sqlite3.Connection:
        return self._conn if self._is_memory else self._open()

    def _release(self, conn: sqlite3.Connection) -> None:
        if not self._is_memory:
            conn.close()

    def _writer(self):
        return _WriteTxn(self)

    # ── migrations ───────────────────────────────────────────────────────────

    def migrate(self) -> list[str]:
        # sqlite parses full scripts (incl. multi-statement triggers) natively, and the
        # connection runs in autocommit mode (isolation_level=None), so no explicit
        # transaction wrapper is needed here.
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version TEXT NOT NULL PRIMARY KEY,"
                    "description TEXT NOT NULL,"
                    "checksum TEXT NOT NULL,"
                    "applied_at TEXT NOT NULL)"
                )
                rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
                applied = {r["version"]: r["checksum"] for r in rows}
                pending, _ = plan_migrations(self.ddl_dir, applied)
                newly_applied = []
                for m in pending:
                    conn.executescript(m.sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, description, checksum, applied_at) "
                        "VALUES (?,?,?,?)",
                        (m.version, m.description, m.checksum, utc_now()),
                    )
                    newly_applied.append(m.version)
                return newly_applied
            finally:
                self._release(conn)

    # ── runs / run_state ─────────────────────────────────────────────────────

    def create_run(self, state: RunState, fingerprint: dict) -> RunState:
        validate(state)
        assert_json_safe(state)
        new_state = _with_version(state, 1)
        new_json = to_json(new_state)
        with self._writer() as conn:
            # run_state is the system of record (CLAUDE.md §9C/B4): write it FIRST, and
            # let its primary key be the existence check -- 0 rows inserted means a run
            # with this run_id is already there.
            cur = conn.execute(
                "INSERT OR IGNORE INTO run_state (run_id, state_version, state_json, status, updated_at) "
                "VALUES (?,?,?,?,?)",
                (new_state.run_id, new_state.state_version, new_json, new_state.status, utc_now()),
            )
            if cur.rowcount == 0:
                raise RunAlreadyExists(state.run_id)

            self._upsert_fingerprint(conn, fingerprint)

            audit_start, audit_end = new_state.audit_period
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, run_kind, engagement_id, skill_id, skill_version, "
                "mode, phase, status, status_reason, audit_period_start, audit_period_end, "
                "objective, run_owner, fingerprint_id, state_version, superseded_by, "
                "created_at, started_at, completed_at, last_state_change_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_state.run_id,
                    new_state.run_kind,
                    new_state.engagement_id,
                    new_state.skill_id,
                    new_state.skill_version,
                    new_state.mode,
                    new_state.phase,
                    new_state.status,
                    new_state.status_reason,
                    audit_start,
                    audit_end,
                    new_state.objective,
                    new_state.run_owner,
                    new_state.fingerprint_id,
                    new_state.state_version,
                    None,
                    new_state.created_at,
                    new_state.started_at,
                    new_state.completed_at,
                    new_state.last_state_change_at,
                ),
            )
            return new_state

    def _upsert_fingerprint(self, conn: sqlite3.Connection, fingerprint: dict) -> None:
        fingerprint_id = fingerprint["fingerprint_id"]
        row = conn.execute(
            "SELECT * FROM run_fingerprints WHERE fingerprint_id = ?", (fingerprint_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO run_fingerprints (fingerprint_id, source_table_versions, "
                "uploaded_file_hashes, reference_data_hashes, skill_content_hash, code_revision, "
                "dependency_lock_hash, runtime_config_hash, endpoint_config, "
                "prompt_template_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint_id,
                    fingerprint["source_table_versions"],
                    fingerprint["uploaded_file_hashes"],
                    fingerprint.get("reference_data_hashes", "{}"),
                    fingerprint.get("skill_content_hash"),
                    fingerprint["code_revision"],
                    fingerprint["dependency_lock_hash"],
                    fingerprint["runtime_config_hash"],
                    fingerprint["endpoint_config"],
                    fingerprint["prompt_template_version"],
                    fingerprint.get("created_at") or utc_now(),
                ),
            )
            return
        differing = []
        for c in _FINGERPRINT_COLUMNS:
            # reference_data_hashes may be absent from hand-built (pre-existing) test
            # fingerprints that predate it -- absence is not a conflict with a stored "{}".
            if c == "reference_data_hashes" and c not in fingerprint:
                continue
            if row[c] != fingerprint.get(c):
                differing.append(c)
        if differing:
            raise FingerprintConflict(fingerprint_id, differing)

    def load_state(self, run_id: str) -> RunState:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state_json FROM run_state WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            self._release(conn)
        if row is None:
            raise RunNotFound(run_id)
        return from_json(row["state_json"])

    def save_state(self, state: RunState) -> RunState:
        validate(state)
        expected = state.state_version
        new_state = _with_version(state, expected + 1)
        assert_json_safe(new_state)
        new_json = to_json(new_state)
        # run_state is the system of record and is the ONLY thing this method must
        # commit for a save to count as durable (CLAUDE.md §9C/B4). One statement sets
        # state_json, state_version, status and updated_at together.
        with self._writer() as conn:
            cur = conn.execute(
                "UPDATE run_state SET state_version = ?, state_json = ?, status = ?, updated_at = ? "
                "WHERE run_id = ? AND state_version = ?",
                (new_state.state_version, new_json, new_state.status, utc_now(), state.run_id, expected),
            )
            if cur.rowcount == 0:
                actual_row = conn.execute(
                    "SELECT state_version FROM run_state WHERE run_id = ?", (state.run_id,)
                ).fetchone()
                actual = actual_row["state_version"] if actual_row else None
                raise StaleStateError(state.run_id, expected, actual)

        # The `runs` projection is a convenience for cheap listing/filtering queries; it
        # is retried and then logged+swallowed on failure so a committed run_state save
        # never raises because of it (CLAUDE.md §9C/B4). repair_projections() catches up
        # any row this leaves behind.
        self._update_runs_projection_with_retry(new_state)
        return new_state

    def _update_runs_projection(self, conn: sqlite3.Connection, state: RunState) -> None:
        audit_start, audit_end = state.audit_period
        # approved_by (P1B's projection column, CLAUDE.md §4.8) has no
        # matching RunState attribute -- it is derived from state.signoff,
        # which the sign_off gate sets BEFORE calling transition() (CLAUDE.md
        # §2.4). Without this it stayed NULL on every completed run forever
        # (found live: P2/P3 gate review item 9).
        approved_by = (state.signoff or {}).get("approver")
        values = [getattr(state, c) for c in _RUN_STATE_SUMMARY_COLUMNS]
        set_clause = ", ".join(f"{c} = ?" for c in _RUN_STATE_SUMMARY_COLUMNS)
        conn.execute(
            f"UPDATE runs SET {set_clause}, audit_period_start = ?, audit_period_end = ?, "
            f"approved_by = ?, state_version = ? WHERE run_id = ? AND state_version < ?",
            (*values, audit_start, audit_end, approved_by, state.state_version, state.run_id, state.state_version),
        )

    def _update_runs_projection_with_retry(self, state: RunState) -> None:
        last_exc: Exception | None = None
        for _ in range(_PROJECTION_RETRY_ATTEMPTS):
            try:
                with self._writer() as conn:
                    self._update_runs_projection(conn, state)
                return
            except Exception as exc:  # pragma: no cover - defensive, projection only
                last_exc = exc
        logger.warning(
            "runs projection update failed after %d attempts for run_id=%s: %r",
            _PROJECTION_RETRY_ATTEMPTS, state.run_id, last_exc,
        )

    def repair_projections(self) -> int:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT rs.run_id, rs.state_json FROM run_state rs "
                "JOIN runs r ON r.run_id = rs.run_id WHERE r.state_version < rs.state_version"
            ).fetchall()
        finally:
            self._release(conn)
        repaired = 0
        for row in rows:
            state = from_json(row["state_json"])
            try:
                with self._writer() as conn2:
                    self._update_runs_projection(conn2, state)
                repaired += 1
            except Exception:  # pragma: no cover - defensive
                logger.warning("repair_projections failed for run_id=%s", row["run_id"])
        return repaired

    def get_fingerprint(self, fingerprint_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM run_fingerprints WHERE fingerprint_id = ?", (fingerprint_id,)
            ).fetchone()
        finally:
            self._release(conn)
        if row is None:
            raise RunNotFound(fingerprint_id)
        return dict(row)

    # ── engagements (P1B) ────────────────────────────────────────────────────

    def get_engagement(self, engagement_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM engagements WHERE engagement_id = ?", (engagement_id,)
            ).fetchone()
        finally:
            self._release(conn)
        return dict(row) if row is not None else None

    def list_engagements(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM engagements ORDER BY created_at").fetchall()
        finally:
            self._release(conn)
        return [dict(r) for r in rows]

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
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?", (skill_id, version)
            ).fetchone()
            if row is not None:
                if row["content_hash"] != content_hash:
                    raise SkillVersionConflict(skill_id, version, row["content_hash"], content_hash)
                return _skill_version_dict_from_row(dict(row))
            conn.execute(
                "INSERT INTO skill_versions (skill_id, version, content_hash, content_json, status, "
                "created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (skill_id, version, content_hash, content_json, status, created_by, now),
            )
        return {
            "skill_id": skill_id, "version": version, "content_hash": content_hash,
            "status": status, "created_by": created_by, "created_at": now,
            "reviewed_by": None, "published_by": None, "published_at": None,
            "superseded_by": None, "surface2_results_json": None, "content": content,
        }

    def get_skill_version(self, skill_id: str, version: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?", (skill_id, version)
            ).fetchone()
        finally:
            self._release(conn)
        return _skill_version_dict_from_row(dict(row)) if row is not None else None

    def list_skill_versions(self, skill_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_id = ? ORDER BY version", (skill_id,)
            ).fetchall()
        finally:
            self._release(conn)
        return [_skill_version_dict_from_row(dict(r)) for r in rows]

    # ── risk / control register (P2) ─────────────────────────────────────────

    def upsert_risks(self, risks: list[dict], *, now: str) -> None:
        with self._writer() as conn:
            for risk in risks:
                risk_id = risk["risk_id"]
                existing = conn.execute(
                    "SELECT status FROM risks WHERE risk_id = ?", (risk_id,)
                ).fetchone()
                if existing is not None:
                    if existing["status"] in ("accepted", "rejected") and risk["status"] == "proposed":
                        raise RiskStatusRegression(risk_id, existing["status"], risk["status"])
                    conn.execute(
                        "UPDATE risks SET engagement_id=?, title=?, description=?, category=?, "
                        "owner=?, status=?, source=?, source_ref=?, as_of_date=?, confidence=?, "
                        "prior_risk_id=? WHERE risk_id = ?",
                        (
                            risk.get("engagement_id"), risk["title"], risk.get("description"),
                            risk.get("category"), risk.get("owner"), risk["status"], risk["source"],
                            risk.get("source_ref"), risk.get("as_of_date"), risk.get("confidence"),
                            risk.get("prior_risk_id"), risk_id,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO risks (risk_id, engagement_id, title, description, category, "
                        "owner, status, source, source_ref, as_of_date, confidence, prior_risk_id, "
                        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            risk_id, risk.get("engagement_id"), risk["title"], risk.get("description"),
                            risk.get("category"), risk.get("owner"), risk["status"], risk["source"],
                            risk.get("source_ref"), risk.get("as_of_date"), risk.get("confidence"),
                            risk.get("prior_risk_id"), risk.get("created_at") or now,
                        ),
                    )

    def upsert_controls(self, controls: list[dict], *, now: str) -> None:
        with self._writer() as conn:
            for control in controls:
                control_id = control["control_id"]
                existing = conn.execute(
                    "SELECT control_id FROM controls WHERE control_id = ?", (control_id,)
                ).fetchone()
                if existing is not None:
                    conn.execute(
                        "UPDATE controls SET risk_id=?, engagement_id=?, title=?, description=?, "
                        "type=?, frequency=?, owner=?, design_conclusion=?, operating_conclusion=? "
                        "WHERE control_id = ?",
                        (
                            control.get("risk_id"), control.get("engagement_id"), control["title"],
                            control.get("description"), control.get("type"), control.get("frequency"),
                            control.get("owner"), control.get("design_conclusion"),
                            control.get("operating_conclusion"), control_id,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO controls (control_id, risk_id, engagement_id, title, "
                        "description, type, frequency, owner, design_conclusion, "
                        "operating_conclusion, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            control_id, control.get("risk_id"), control.get("engagement_id"),
                            control["title"], control.get("description"), control.get("type"),
                            control.get("frequency"), control.get("owner"),
                            control.get("design_conclusion"), control.get("operating_conclusion"),
                            control.get("created_at") or now,
                        ),
                    )

    def list_risks(self, engagement_id: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if engagement_id is None:
                rows = conn.execute("SELECT * FROM risks ORDER BY risk_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM risks WHERE engagement_id = ? ORDER BY risk_id", (engagement_id,)
                ).fetchall()
        finally:
            self._release(conn)
        return [dict(r) for r in rows]

    def list_controls(self, engagement_id: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if engagement_id is None:
                rows = conn.execute("SELECT * FROM controls ORDER BY control_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM controls WHERE engagement_id = ? ORDER BY control_id", (engagement_id,)
                ).fetchall()
        finally:
            self._release(conn)
        return [dict(r) for r in rows]

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
        with self._writer() as conn:
            existing_rows = conn.execute(
                "SELECT finding_id, review_state FROM findings WHERE run_id = ?", (run_id,)
            ).fetchall()
            orphans = [dict(r) for r in existing_rows if r["finding_id"] not in new_ids]
            # Checked BEFORE any write so a rejected call leaves data untouched, not
            # merely rolled back -- this holds even against a backend with no real
            # multi-statement transaction (DeltaPersistence.write_findings below).
            blocking = [o["finding_id"] for o in orphans if o["review_state"] != "draft"]
            if blocking:
                raise NonDraftFindingWouldBeDeleted(run_id, blocking)

            update_clause = ", ".join(f"{c} = excluded.{c}" for c in _FINDING_UPDATE_COLUMNS)
            placeholders = ",".join("?" for _ in _FINDING_COLUMNS)
            for finding in findings:
                values = _finding_row_values(
                    run_id, finding, engagement_id=engagement_id, skill_id=skill_id,
                    skill_version=skill_version, now=now,
                )
                conn.execute(
                    f"INSERT INTO findings ({','.join(_FINDING_COLUMNS)}) VALUES ({placeholders}) "
                    f"ON CONFLICT(finding_id) DO UPDATE SET {update_clause}",
                    tuple(values[c] for c in _FINDING_COLUMNS),
                )

            to_delete = [o["finding_id"] for o in orphans]
            if to_delete:
                del_placeholders = ",".join("?" for _ in to_delete)
                conn.execute(
                    f"DELETE FROM findings WHERE run_id = ? AND finding_id IN ({del_placeholders})",
                    (run_id, *to_delete),
                )
        return self.list_findings(run_id)

    def list_findings(self, run_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM findings WHERE run_id = ? ORDER BY "
                "CASE severity WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 WHEN 'Low' THEN 2 ELSE 3 END, "
                "rule_id",
                (run_id,),
            ).fetchall()
        finally:
            self._release(conn)
        return [_finding_dict_from_row(dict(r)) for r in rows]

    def set_finding_review_state(self, finding_id: str, *, to_state: str, actor: str, now: str) -> dict:
        with self._writer() as conn:
            row = conn.execute("SELECT * FROM findings WHERE finding_id = ?", (finding_id,)).fetchone()
            if row is None:
                raise FindingNotFound(finding_id)
            current = row["review_state"]
            if (
                current not in _REVIEW_STATE_ORDER
                or to_state not in _REVIEW_STATE_ORDER
                or _REVIEW_STATE_ORDER.index(to_state) != _REVIEW_STATE_ORDER.index(current) + 1
            ):
                raise InvalidReviewStateTransition(finding_id, current, to_state)
            conn.execute(
                "UPDATE findings SET review_state = ?, updated_at = ? WHERE finding_id = ?",
                (to_state, now, finding_id),
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
        with self._writer() as conn:
            open_row = conn.execute(
                "SELECT * FROM node_attempts WHERE run_id = ? AND node_name = ? AND phase = ? "
                "AND phase_epoch = ? AND outcome IS NULL",
                (run_id, node_name, phase, phase_epoch),
            ).fetchone()
            if open_row is not None:
                return dict(open_row)

            completed_count = conn.execute(
                "SELECT COUNT(*) AS n FROM node_attempts WHERE run_id = ? AND node_name = ? "
                "AND phase = ? AND phase_epoch = ? AND outcome IS NOT NULL",
                (run_id, node_name, phase, phase_epoch),
            ).fetchone()["n"]
            attempt_number = completed_count + 1
            execution_key = f"{run_id}:{phase}:{phase_epoch}:{node_name}:{attempt_number}"
            attempt_id = _attempt_id(execution_key)

            conn.execute(
                "INSERT OR IGNORE INTO node_attempts (attempt_id, execution_key, run_id, phase, "
                "phase_epoch, node_index, node_name, attempt_number, started_at, completed_at, "
                "outcome, error_detail, state_version_before, state_version_after, "
                "result_state_json) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,NULL,NULL)",
                (
                    attempt_id,
                    execution_key,
                    run_id,
                    phase,
                    phase_epoch,
                    node_index,
                    node_name,
                    attempt_number,
                    now,
                    state_version_before,
                ),
            )
            row = conn.execute(
                "SELECT * FROM node_attempts WHERE execution_key = ?", (execution_key,)
            ).fetchone()
            return dict(row)

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
        with self._writer() as conn:
            cur = conn.execute(
                "UPDATE node_attempts SET outcome = ?, completed_at = ?, state_version_after = ?, "
                "result_state_json = ?, error_detail = ? WHERE execution_key = ? AND outcome IS NULL",
                (outcome, now, state_version_after, result_state_json, error_detail, execution_key),
            )
            if cur.rowcount == 0:
                row = conn.execute(
                    "SELECT outcome FROM node_attempts WHERE execution_key = ?", (execution_key,)
                ).fetchone()
                if row is None:
                    raise AttemptNotFound(execution_key)
                if row["outcome"] == outcome:
                    return  # already closed with this outcome -- idempotent no-op
                raise AttemptAlreadyClosed(execution_key, row["outcome"], outcome)

    def list_node_attempts(self, run_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM node_attempts WHERE run_id = ? ORDER BY started_at, attempt_number",
                (run_id,),
            ).fetchall()
        finally:
            self._release(conn)
        return [dict(r) for r in rows]

    def close_open_attempts(
        self, run_id: str, *, outcome: str, now: str, error_detail: str | None = None
    ) -> None:
        with self._writer() as conn:
            conn.execute(
                "UPDATE node_attempts SET outcome = ?, completed_at = ?, error_detail = ? "
                "WHERE run_id = ? AND outcome IS NULL",
                (outcome, now, error_detail, run_id),
            )

    # ── trace events ─────────────────────────────────────────────────────────

    def append_trace_event(self, event: dict) -> None:
        with self._writer() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO trace_events (event_id, run_id, engagement_id, event_type, "
                "event_time, stage, status, message, duration_s, node_name, execution_key, actor, "
                "state_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event["event_id"],
                    event["run_id"],
                    event.get("engagement_id"),
                    event["event_type"],
                    event["event_time"],
                    event["stage"],
                    event["status"],
                    event["message"],
                    event.get("duration_s"),
                    event.get("node_name"),
                    event.get("execution_key"),
                    event["actor"],
                    event.get("state_version"),
                ),
            )

    def list_trace_events(self, run_id: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if run_id is None:
                rows = conn.execute("SELECT * FROM trace_events ORDER BY event_time").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trace_events WHERE run_id = ? ORDER BY event_time", (run_id,)
                ).fetchall()
        finally:
            self._release(conn)
        return [_trace_event_ui_shape(dict(r)) for r in rows]

    # ── run listing ──────────────────────────────────────────────────────────

    def list_runs(self, filters: dict | None = None) -> list[dict]:
        # run_state is the system of record for status (CLAUDE.md §9C/B4): join it in
        # and always report ITS status, never the (possibly lagging) runs projection.
        clauses = []
        params: list = []
        filters = filters or {}
        for col in ("skill_id", "run_kind"):
            if filters.get(col):
                clauses.append(f"r.{col} = ?")
                params.append(filters[col])
        if filters.get("status"):
            clauses.append("rs.status = ?")
            params.append(filters["status"])
        sql = "SELECT r.*, rs.status AS rs_status FROM runs r JOIN run_state rs ON r.run_id = rs.run_id"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.created_at DESC"
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            self._release(conn)
        results = []
        for r in rows:
            d = dict(r)
            d["status"] = d.pop("rs_status")
            results.append(d)
        return results

    def find_runs(self, statuses: list[str]) -> list[str]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT run_id FROM run_state WHERE status IN ({placeholders})", statuses
            ).fetchall()
        finally:
            self._release(conn)
        return [r["run_id"] for r in rows]

    # ── P3 run outputs ───────────────────────────────────────────────────────

    def write_flagged_rows(self, run_id: str, rows: list[dict]) -> None:
        # ON CONFLICT DO UPDATE + prune, the same pattern write_findings uses --
        # matches DeltaPersistence's MERGE + prune shape (CLAUDE.md build brief
        # P3 §1) rather than DELETE-then-INSERT, even though this backend's own
        # single-transaction write already made the delete/insert atomic to
        # other readers; keeping both backends' upsert semantics identical is
        # what the P1A cross-backend persistence-contract test relies on.
        new_keys = {(r["source"], r["row_key"], r["flag"]) for r in rows}
        with self._writer() as conn:
            existing_keys = {
                (r["source"], r["row_key"], r["flag"])
                for r in conn.execute(
                    "SELECT source, row_key, flag FROM flagged_rows WHERE run_id = ?", (run_id,)
                ).fetchall()
            }
            conn.executemany(
                "INSERT INTO flagged_rows (run_id, source, row_key, flag, group_id) VALUES (?,?,?,?,?) "
                "ON CONFLICT(run_id, source, row_key, flag) DO UPDATE SET group_id = excluded.group_id",
                [
                    (run_id, r["source"], r["row_key"], r["flag"], r.get("group_id"))
                    for r in rows
                ],
            )
            orphans = existing_keys - new_keys
            if orphans:
                conn.executemany(
                    "DELETE FROM flagged_rows WHERE run_id = ? AND source = ? AND row_key = ? AND flag = ?",
                    [(run_id, source, row_key, flag) for source, row_key, flag in orphans],
                )

    def list_flagged_rows(self, run_id: str, flag: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if flag is None:
                rows = conn.execute(
                    "SELECT * FROM flagged_rows WHERE run_id = ? ORDER BY source, row_key, flag",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM flagged_rows WHERE run_id = ? AND flag = ? ORDER BY source, row_key",
                    (run_id, flag),
                ).fetchall()
        finally:
            self._release(conn)
        return [dict(r) for r in rows]

    def write_run_metrics(self, run_id: str, metrics: list[dict]) -> None:
        # ON CONFLICT DO UPDATE + prune (see write_flagged_rows above).
        new_names = {m["metric_name"] for m in metrics}
        with self._writer() as conn:
            existing_names = {
                r["metric_name"]
                for r in conn.execute(
                    "SELECT metric_name FROM run_metrics WHERE run_id = ?", (run_id,)
                ).fetchall()
            }
            conn.executemany(
                "INSERT INTO run_metrics (run_id, metric_name, value, value_text, unit, "
                "source_ref_json, test_id) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id, metric_name) DO UPDATE SET value = excluded.value, "
                "value_text = excluded.value_text, unit = excluded.unit, "
                "source_ref_json = excluded.source_ref_json, test_id = excluded.test_id",
                [
                    (
                        run_id,
                        m["metric_name"],
                        *_metric_value_columns(m.get("value")),
                        m.get("unit"),
                        _canonical_json(m.get("source_ref", {})),
                        m.get("test_id"),
                    )
                    for m in metrics
                ],
            )
            orphans = existing_names - new_names
            if orphans:
                conn.executemany(
                    "DELETE FROM run_metrics WHERE run_id = ? AND metric_name = ?",
                    [(run_id, name) for name in orphans],
                )

    def get_run_metrics(self, run_id: str) -> dict[str, dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM run_metrics WHERE run_id = ? ORDER BY metric_name", (run_id,)
            ).fetchall()
        finally:
            self._release(conn)
        return {r["metric_name"]: _metric_dict_from_row(dict(r)) for r in rows}

    def write_issues_for_findings(
        self, run_id: str, findings: list[dict], *, engagement_id, now: str
    ) -> list[dict]:
        created: list[dict] = []
        with self._writer() as conn:
            for finding in findings:
                issue_id = f"ISS-{finding['finding_id']}"
                existing = conn.execute(
                    "SELECT issue_id FROM issues WHERE issue_id = ?", (issue_id,)
                ).fetchone()
                if existing is not None:
                    continue
                conn.execute(
                    "INSERT INTO issues (issue_id, engagement_id, rule_id, title, description, "
                    "rating, status, raised_by, raised_at, owner, due_date, remediation_plan, "
                    "management_response, prior_issue_id, finding_ids_json, run_ids_json, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        issue_id,
                        engagement_id,
                        finding.get("rule_id"),
                        finding["title"],
                        finding.get("observation"),
                        finding.get("severity"),
                        "draft",
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        _canonical_json([finding["finding_id"]]),
                        _canonical_json([run_id]),
                        now,
                        now,
                    ),
                )
                created.append({"issue_id": issue_id, "finding_id": finding["finding_id"]})
        return created

    def write_management_actions(self, run_id: str, actions: list[dict], *, now: str) -> None:
        # ON CONFLICT DO UPDATE + prune (see write_flagged_rows above) --
        # action_id is deterministic per finding_id (nodes/fieldwork.py's
        # act()), so a re-executed act node upserts by that id.
        new_ids = {a["action_id"] for a in actions}
        with self._writer() as conn:
            existing_ids = {
                r["action_id"]
                for r in conn.execute(
                    "SELECT action_id FROM management_actions WHERE run_id = ?", (run_id,)
                ).fetchall()
            }
            for a in actions:
                conn.execute(
                    "INSERT INTO management_actions (action_id, issue_id, finding_id, run_id, "
                    "engagement_id, skill_id, title, description, owner, risk, status, "
                    "target_date, potential_exposure, evidence_link, created_at, last_updated) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(action_id) DO UPDATE SET issue_id = excluded.issue_id, "
                    "finding_id = excluded.finding_id, run_id = excluded.run_id, "
                    "engagement_id = excluded.engagement_id, skill_id = excluded.skill_id, "
                    "title = excluded.title, description = excluded.description, "
                    "owner = excluded.owner, risk = excluded.risk, status = excluded.status, "
                    "target_date = excluded.target_date, "
                    "potential_exposure = excluded.potential_exposure, "
                    "evidence_link = excluded.evidence_link, last_updated = excluded.last_updated",
                    (
                        a["action_id"],
                        a.get("issue_id"),
                        a.get("finding_id"),
                        run_id,
                        a.get("engagement_id"),
                        a.get("skill_id"),
                        a["title"],
                        a.get("description"),
                        a.get("owner"),
                        a.get("risk"),
                        a.get("status", "draft"),
                        a.get("target_date"),
                        a.get("potential_exposure"),
                        a.get("evidence_link"),
                        now,
                        now,
                    ),
                )
            orphans = existing_ids - new_ids
            if orphans:
                conn.executemany(
                    "DELETE FROM management_actions WHERE run_id = ? AND action_id = ?",
                    [(run_id, aid) for aid in orphans],
                )

    def list_management_actions(self, filters: dict | None = None) -> list[dict]:
        filters = filters or {}
        clauses = []
        params: list = []
        for col in ("run_id", "skill_id", "engagement_id", "status"):
            if filters.get(col):
                clauses.append(f"ma.{col} = ?")
                params.append(filters[col])
        sql = (
            "SELECT ma.*, f.title AS finding_title, f.observation AS finding_observation "
            "FROM management_actions ma LEFT JOIN findings f ON f.finding_id = ma.finding_id"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ma.created_at DESC"
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            self._release(conn)
        return [dict(r) for r in rows]

    def record_export(
        self, run_id: str, kind: str, *, path: str, sha256: str, created_by: str, now: str
    ) -> dict:
        with self._writer() as conn:
            conn.execute(
                "INSERT INTO exports (run_id, kind, path, sha256, created_at, created_by) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(run_id, kind) DO UPDATE SET "
                "path = excluded.path, sha256 = excluded.sha256, created_at = excluded.created_at, "
                "created_by = excluded.created_by",
                (run_id, kind, path, sha256, now, created_by),
            )
        return {
            "run_id": run_id, "kind": kind, "path": path, "sha256": sha256,
            "created_at": now, "created_by": created_by,
        }

    def list_exports(self, run_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM exports WHERE run_id = ? ORDER BY kind", (run_id,)
            ).fetchall()
        finally:
            self._release(conn)
        return [dict(r) for r in rows]

    # ── leases (CLAUDE.md §9C P1A concurrency foundation, §2.3 rule 3) ──────

    def acquire_lease(self, run_id: str, worker_id: str, *, ttl_s: float, now: str) -> bool:
        # Item 6 (CLAUDE.md P2/P3 gate review): a single upsert, not a
        # read-then-insert/update -- SQLite's UPSERT ... WHERE evaluates the
        # WHERE clause against the EXISTING row at the same statement the
        # conflict is detected in, so "no row" (fresh INSERT), "row expired"
        # (DO UPDATE fires) and "row still held by someone else" (DO UPDATE's
        # WHERE is false, a genuine no-op) are all one atomic decision, and
        # ownership is read from the affected-row count, never a SEPARATE
        # SELECT after the write (mirrors DeltaPersistence.acquire_lease's
        # MERGE).
        expires_at = _add_seconds(now, ttl_s)
        with self._writer() as conn:
            cur = conn.execute(
                "INSERT INTO run_leases (run_id, claimed_by, claimed_at, heartbeat_at, "
                "lease_expires_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET claimed_by = excluded.claimed_by, "
                "claimed_at = excluded.claimed_at, heartbeat_at = excluded.heartbeat_at, "
                "lease_expires_at = excluded.lease_expires_at "
                "WHERE run_leases.lease_expires_at <= ?",
                (run_id, worker_id, now, now, expires_at, now),
            )
            return cur.rowcount > 0

    def renew_lease(self, run_id: str, worker_id: str, *, ttl_s: float, now: str) -> bool:
        expires_at = _add_seconds(now, ttl_s)
        with self._writer() as conn:
            cur = conn.execute(
                "UPDATE run_leases SET heartbeat_at = ?, lease_expires_at = ? "
                "WHERE run_id = ? AND claimed_by = ?",
                (now, expires_at, run_id, worker_id),
            )
            return cur.rowcount > 0

    def release_lease(self, run_id: str, worker_id: str) -> None:
        with self._writer() as conn:
            conn.execute(
                "DELETE FROM run_leases WHERE run_id = ? AND claimed_by = ?", (run_id, worker_id)
            )

    def expired_leases(self, now: str) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT run_id FROM run_leases WHERE lease_expires_at <= ?", (now,)
            ).fetchall()
        finally:
            self._release(conn)
        return [r["run_id"] for r in rows]


def _with_version(state: RunState, version: int) -> RunState:
    import dataclasses

    return dataclasses.replace(state, state_version=version)


def _trace_event_ui_shape(row: dict) -> dict:
    row = dict(row)
    row["timestamp"] = row.pop("event_time")
    return row


class _WriteTxn:
    def __init__(self, persistence: LocalPersistence):
        self._p = persistence
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self._p._lock.acquire()
        self._conn = self._p._connect()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._p._release(self._conn)
            self._p._lock.release()
        return False
