-- 015_p7_review_workflow: preparer -> reviewer -> approver segregation of
-- duties (docs/specs/P7_mapping_authoring_design.md §3.3-3.4). Adds a new
-- append-only `review_steps` table (one row per prepared/reviewed/returned/
-- approved action -- the evidence trail a "who did what, when, under which
-- policy" question is answered from), extends `review_notes` with who
-- raised/responded/cleared under which role (`review_notes` itself already
-- exists from 002_p1b_suite.sql; P7 only adds columns), extends
-- `narrative_edits` so an edit made in reply to a note is traceable to it,
-- and adds `runs.prepared_at`/`reviewed_at` alongside the existing
-- `prepared_by`/`reviewed_by`/`approved_by` (002_p1b_suite.sql).
--
-- `step_id` is deterministic (`sha256(run_id:action:state_version)`,
-- orchestrator.runs), so a replayed click under the same CAS state_version
-- is a no-op insert, never a duplicate row -- the same idempotency
-- discipline as `trace_events`' own `_trace_event_id`.

CREATE TABLE IF NOT EXISTS review_steps (
  step_id TEXT NOT NULL PRIMARY KEY,
  run_id TEXT NOT NULL,
  engagement_id TEXT,
  action TEXT NOT NULL CHECK (action IN ('prepared', 'reviewed', 'returned', 'approved')),
  actor TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('preparer', 'reviewer', 'approver')),
  matched_group TEXT,
  role_source TEXT NOT NULL CHECK (role_source IN ('workspace_groups', 'config')),
  sod_mode TEXT NOT NULL CHECK (sod_mode IN ('enforced', 'labelled')),
  reason TEXT,
  theme_generation INTEGER,
  narration_generation INTEGER,
  state_version INTEGER NOT NULL,
  at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_steps_run_id ON review_steps(run_id);

CREATE TRIGGER IF NOT EXISTS review_steps_no_update BEFORE UPDATE ON review_steps BEGIN SELECT RAISE(ABORT, 'review_steps is append-only'); END;

CREATE TRIGGER IF NOT EXISTS review_steps_no_delete BEFORE DELETE ON review_steps BEGIN SELECT RAISE(ABORT, 'review_steps is append-only'); END;

ALTER TABLE review_notes ADD COLUMN raised_role TEXT;

ALTER TABLE review_notes ADD COLUMN responded_by TEXT;

ALTER TABLE review_notes ADD COLUMN responded_at TEXT;

ALTER TABLE review_notes ADD COLUMN cleared_role TEXT;

ALTER TABLE narrative_edits ADD COLUMN review_stage TEXT;

ALTER TABLE narrative_edits ADD COLUMN note_id TEXT;

ALTER TABLE runs ADD COLUMN prepared_at TEXT;

ALTER TABLE runs ADD COLUMN reviewed_at TEXT;
