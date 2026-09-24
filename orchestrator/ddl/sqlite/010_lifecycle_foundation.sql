-- 010_lifecycle_foundation: LM1 (LIFECYCLE_design.md §4). See
-- orchestrator/ddl/delta/010_lifecycle_foundation.sql for the full rationale
-- -- same schema, sqlite dialect.
--
-- SQLite has no ALTER TABLE ... DROP/ADD CONSTRAINT, so widening the
-- existing run_kind CHECK requires the standard rebuild-and-copy pattern
-- (see 005_p3_indeterminate_severity.sql): create the table with the new
-- CHECK (and the three new executor/executor_ref/executed_as columns) under
-- a temporary name, copy every row across, drop the old table, rename the
-- new one into place, then recreate the trigger sqlite drops along with the
-- old table. Column list and every other constraint are carried over
-- byte-for-byte from 001_p1a_ledger.sql plus the
-- prepared_by/reviewed_by/approved_by additions in 002_p1b_suite.sql.

CREATE TABLE runs_new (
  run_id TEXT NOT NULL PRIMARY KEY,
  run_kind TEXT NOT NULL CHECK (run_kind IN ('fieldwork', 'sensing', 'assessment', 'planning', 'design_assessment', 'reporting', 'evidence')),
  engagement_id TEXT,
  skill_id TEXT,
  skill_version TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('playbook', 'explorer')),
  phase TEXT NOT NULL CHECK (phase IN ('plan', 'execute', 'export')),
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'awaiting_confirmation', 'awaiting_signoff', 'completed', 'failed', 'interrupted')),
  status_reason TEXT,
  audit_period_start TEXT NOT NULL,
  audit_period_end TEXT NOT NULL,
  objective TEXT NOT NULL,
  run_owner TEXT NOT NULL,
  fingerprint_id TEXT NOT NULL,
  state_version INTEGER NOT NULL CHECK (state_version >= 1),
  superseded_by TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  last_state_change_at TEXT NOT NULL,
  prepared_by TEXT,
  reviewed_by TEXT,
  approved_by TEXT,
  executor TEXT,
  executor_ref TEXT,
  executed_as TEXT
);

INSERT INTO runs_new (
  run_id, run_kind, engagement_id, skill_id, skill_version, mode, phase, status, status_reason,
  audit_period_start, audit_period_end, objective, run_owner, fingerprint_id, state_version,
  superseded_by, created_at, started_at, completed_at, last_state_change_at,
  prepared_by, reviewed_by, approved_by
)
SELECT
  run_id, run_kind, engagement_id, skill_id, skill_version, mode, phase, status, status_reason,
  audit_period_start, audit_period_end, objective, run_owner, fingerprint_id, state_version,
  superseded_by, created_at, started_at, completed_at, last_state_change_at,
  prepared_by, reviewed_by, approved_by
FROM runs;

DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;

CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'runs are audit evidence and cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS executor_dispatches (
  dispatch_id TEXT NOT NULL PRIMARY KEY,
  run_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  phase_epoch INTEGER NOT NULL,
  executor TEXT NOT NULL,
  external_run_id TEXT,
  dispatched_by TEXT NOT NULL,
  dispatched_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS executor_dispatches_no_update BEFORE UPDATE ON executor_dispatches BEGIN SELECT RAISE(ABORT, 'executor_dispatches is append-only'); END;

CREATE TRIGGER IF NOT EXISTS executor_dispatches_no_delete BEFORE DELETE ON executor_dispatches BEGIN SELECT RAISE(ABORT, 'executor_dispatches is append-only'); END;

ALTER TABLE engagements ADD COLUMN description TEXT;
ALTER TABLE engagements ADD COLUMN business_unit TEXT;
ALTER TABLE engagements ADD COLUMN materiality REAL;
ALTER TABLE engagements ADD COLUMN materiality_currency TEXT;
ALTER TABLE engagements ADD COLUMN prior_engagement_id TEXT;
ALTER TABLE engagements ADD COLUMN created_by TEXT;

CREATE TABLE IF NOT EXISTS engagement_risks (
  engagement_id TEXT NOT NULL,
  risk_id TEXT NOT NULL,
  added_by TEXT NOT NULL,
  added_at TEXT NOT NULL,
  removed_by TEXT,
  removed_at TEXT,
  reason TEXT,
  PRIMARY KEY (engagement_id, risk_id)
);

-- The CHECK on risks.source is on an existing column, so widening it needs the
-- same rebuild-and-copy pattern as `runs` above; the five new columns ride
-- along in the same rebuild rather than a separate ALTER TABLE ADD COLUMN
-- pass. Column list otherwise carried over byte-for-byte from
-- 002_p1b_suite.sql.

CREATE TABLE risks_new (
  risk_id TEXT NOT NULL PRIMARY KEY,
  engagement_id TEXT,
  title TEXT NOT NULL,
  description TEXT,
  category TEXT,
  owner TEXT,
  status TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'rejected', 'superseded')),
  source TEXT NOT NULL CHECK (source IN ('manual', 'glean', 'regulation', 'erm_import', 'document_corpus', 'explorer')),
  source_ref TEXT,
  as_of_date TEXT,
  confidence REAL,
  prior_risk_id TEXT,
  created_at TEXT NOT NULL,
  proposed_by_run_id TEXT,
  decided_by TEXT,
  decided_at TEXT,
  decision_reason TEXT,
  register_scope TEXT CHECK (register_scope IS NULL OR register_scope IN ('enterprise', 'engagement'))
);

INSERT INTO risks_new (
  risk_id, engagement_id, title, description, category, owner, status, source, source_ref,
  as_of_date, confidence, prior_risk_id, created_at
)
SELECT
  risk_id, engagement_id, title, description, category, owner, status, source, source_ref,
  as_of_date, confidence, prior_risk_id, created_at
FROM risks;

DROP TABLE risks;
ALTER TABLE risks_new RENAME TO risks;

ALTER TABLE controls ADD COLUMN status TEXT CHECK (status IS NULL OR status IN ('proposed', 'accepted', 'rejected', 'superseded'));
ALTER TABLE controls ADD COLUMN proposed_by_run_id TEXT;
ALTER TABLE controls ADD COLUMN decided_by TEXT;
ALTER TABLE controls ADD COLUMN decided_at TEXT;
ALTER TABLE controls ADD COLUMN design_assessment_id TEXT;
ALTER TABLE controls ADD COLUMN design_concluded_by TEXT;
ALTER TABLE controls ADD COLUMN design_concluded_at TEXT;

-- Backfill: see orchestrator/ddl/delta/010_lifecycle_foundation.sql.
UPDATE controls SET status = 'accepted' WHERE status IS NULL;
