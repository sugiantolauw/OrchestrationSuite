from __future__ import annotations

import sqlite3

import pytest

from orchestrator import runs
from orchestrator.errors import EngagementNotFound

# P1B: engagement scoping (orchestrator/runs.py) and the schema DDL in
# orchestrator/ddl/{delta,sqlite}/002_p1b_suite.sql. Schema-level checks run against
# LocalPersistence only (Delta stays fake-cursor-tested, CLAUDE.md §11) via the
# `local_persistence` fixture; the higher-level engagement-scoping behaviour runs
# against every backend via the `persistence` fixture.


def _fingerprint(fp_id="FP-ENG"):
    return dict(
        fingerprint_id=fp_id,
        source_table_versions="{}",
        uploaded_file_hashes="{}",
        reference_data_hashes="{}",
        skill_content_hash=None,
        code_revision="rev1",
        dependency_lock_hash="dep1",
        runtime_config_hash="rc1",
        endpoint_config="{}",
        prompt_template_version="none",
        created_at="2026-01-01T00:00:00.000000Z",
    )


# ── engagement scoping behaviour (backend-agnostic) ──────────────────────────────────────


def test_default_engagement_seeded_and_gettable(persistence):
    eng = persistence.get_engagement("ENG-DEFAULT")
    assert eng is not None
    assert eng["engagement_id"] == "ENG-DEFAULT"
    assert eng["status"] == "open"


def test_list_engagements_includes_default(persistence):
    ids = {e["engagement_id"] for e in persistence.list_engagements()}
    assert "ENG-DEFAULT" in ids


def test_get_engagement_missing_returns_none(persistence):
    assert persistence.get_engagement("ENG-DOES-NOT-EXIST") is None


@pytest.mark.parametrize("run_kind", ["fieldwork", "assessment", "planning"])
def test_create_run_rejects_unknown_engagement(persistence, run_kind, clock):
    with pytest.raises(EngagementNotFound):
        runs.create_run(
            persistence, run_kind=run_kind, engagement_id="ENG-NOPE", skill_id=None,
            skill_version=None, mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
            objective="t", run_owner="alice", fingerprint=_fingerprint(), now=clock(),
        )


@pytest.mark.parametrize("run_kind", ["fieldwork", "assessment", "planning"])
def test_create_run_rejects_missing_engagement_for_scoped_kinds(persistence, run_kind, clock):
    with pytest.raises(EngagementNotFound):
        runs.create_run(
            persistence, run_kind=run_kind, engagement_id=None, skill_id=None,
            skill_version=None, mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
            objective="t", run_owner="alice", fingerprint=_fingerprint(), now=clock(),
        )


def test_create_run_accepts_none_engagement_for_sensing(persistence, clock):
    state = runs.create_run(
        persistence, run_kind="sensing", engagement_id=None, skill_id=None,
        skill_version=None, mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
        objective="t", run_owner="alice", fingerprint=_fingerprint(), now=clock(),
    )
    assert state.engagement_id is None


def test_create_run_accepts_known_engagement(persistence, clock):
    state = runs.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None,
        skill_version=None, mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
        objective="t", run_owner="alice", fingerprint=_fingerprint(), now=clock(),
    )
    assert state.engagement_id == "ENG-DEFAULT"


# ── schema (local only, CLAUDE.md §11) ────────────────────────────────────────────────────

_EXPECTED_COLUMNS = {
    "engagements": {"engagement_id", "name", "owner", "status", "stage", "created_at"},
    "findings": {"finding_id", "run_id", "rule_id", "severity", "metrics_cited_json", "review_state", "recurrence_count"},
    "issues": {"issue_id", "status", "finding_ids_json", "run_ids_json"},
    "management_actions": {"action_id", "status"},
    "skill_versions": {"skill_id", "version", "content_hash", "status"},
    "risks": {"risk_id", "title", "status", "source"},
    "controls": {"control_id", "title"},
    "risk_assessments": {"assessment_id", "risk_id", "dimension", "score", "method", "assessed_by", "assessed_at"},
    "review_notes": {"note_id", "raised_by", "raised_at", "body", "state"},
}


def _table_columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_every_p1b_table_exists_with_expected_columns(local_persistence):
    conn = local_persistence._connect()
    try:
        for table, expected in _EXPECTED_COLUMNS.items():
            actual = _table_columns(conn, table)
            assert actual, f"table {table!r} does not exist or has no columns"
            missing = expected - actual
            assert not missing, f"table {table!r} missing columns: {missing}"
    finally:
        local_persistence._release(conn)


def test_runs_has_prepared_reviewed_approved_by_columns(local_persistence):
    conn = local_persistence._connect()
    try:
        cols = _table_columns(conn, "runs")
        assert {"prepared_by", "reviewed_by", "approved_by"} <= cols
    finally:
        local_persistence._release(conn)


_CHECK_CONSTRAINT_CASES = [
    ("engagements", "INSERT INTO engagements (engagement_id, name, owner, status, stage, created_at) "
                     "VALUES ('X1', 'n', 'o', 'bogus', 'fieldwork', 't0')"),
    ("findings", "INSERT INTO findings (finding_id, run_id, rule_id, title, severity, metrics_cited_json, "
                  "review_state, recurrence_count, created_at, updated_at) VALUES "
                  "('F1', 'R1', 'RULE.1', 't', 'bogus', '[]', 'draft', 0, 't0', 't0')"),
    ("issues", "INSERT INTO issues (issue_id, title, status, finding_ids_json, run_ids_json, created_at, updated_at) "
               "VALUES ('I1', 't', 'bogus', '[]', '[]', 't0', 't0')"),
    ("management_actions", "INSERT INTO management_actions (action_id, title, status, created_at, last_updated) "
                            "VALUES ('A1', 't', 'bogus', 't0', 't0')"),
    ("skill_versions", "INSERT INTO skill_versions (skill_id, version, content_hash, content_json, status, "
                        "created_by, created_at) VALUES ('S1', '1', 'h', '{}', 'bogus', 'u', 't0')"),
    ("risks", "INSERT INTO risks (risk_id, title, status, source, created_at) VALUES "
              "('RSK1', 't', 'bogus', 'manual', 't0')"),
    ("risk_assessments", "INSERT INTO risk_assessments (assessment_id, risk_id, dimension, score, method, "
                          "assessed_by, assessed_at) VALUES ('AS1', 'RSK1', 'bogus', 1.0, 'human', 'u', 't0')"),
    ("review_notes", "INSERT INTO review_notes (note_id, raised_by, raised_at, body, state) VALUES "
                      "('N1', 'u', 't0', 'b', 'bogus')"),
]


@pytest.mark.parametrize("table,bad_insert", _CHECK_CONSTRAINT_CASES, ids=[c[0] for c in _CHECK_CONSTRAINT_CASES])
def test_check_constraint_rejects_bad_value(local_persistence, table, bad_insert):
    conn = local_persistence._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(bad_insert)
    finally:
        local_persistence._release(conn)


def test_risk_assessments_is_append_only(local_persistence):
    conn = local_persistence._connect()
    try:
        conn.execute(
            "INSERT INTO risk_assessments (assessment_id, risk_id, dimension, score, method, assessed_by, "
            "assessed_at) VALUES ('AS-AO', 'RSK1', 'financial', 5.0, 'human', 'alice', 't0')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE risk_assessments SET score = 9.0 WHERE assessment_id = 'AS-AO'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM risk_assessments WHERE assessment_id = 'AS-AO'")
    finally:
        local_persistence._release(conn)


def test_eng_default_seed_insert_is_idempotent_even_if_run_twice(local_persistence):
    # migrate() itself never re-applies 002 once its checksum is recorded, but the seed
    # row's own statement (INSERT OR IGNORE) must independently be safe to run twice --
    # this is what actually protects a race between concurrent first-time migrations
    # (CLAUDE.md §9C concurrency model, P1B DoD).
    conn = local_persistence._connect()
    try:
        insert_sql = (
            "INSERT OR IGNORE INTO engagements (engagement_id, name, entity, period_start, "
            "period_end, owner, status, stage, created_at) VALUES ('ENG-DEFAULT', "
            "'Default engagement', NULL, NULL, NULL, 'system', 'open', 'fieldwork', "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        )
        conn.execute(insert_sql)
        conn.execute(insert_sql)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM engagements WHERE engagement_id = 'ENG-DEFAULT'"
        ).fetchone()
        assert row["n"] == 1
    finally:
        local_persistence._release(conn)


def test_migrate_called_twice_does_not_duplicate_default_engagement(local_persistence):
    local_persistence.migrate()  # already migrated by the fixture; must stay a no-op
    conn = local_persistence._connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM engagements WHERE engagement_id = 'ENG-DEFAULT'"
        ).fetchone()
        assert row["n"] == 1
    finally:
        local_persistence._release(conn)
