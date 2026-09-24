-- 012_management_action_edits: (sqlite dialect) see
-- orchestrator/ddl/delta/012_management_action_edits.sql for the full
-- rationale -- same change. SQLite has no ALTER TABLE ... DROP/ADD
-- CONSTRAINT (and no ALTER COLUMN), so widening the status CHECK requires
-- the same rebuild-and-copy pattern as 005_p3_indeterminate_severity.sql.
-- Column list and every other constraint are carried over byte-for-byte
-- from 002_p1b_suite.sql + 011_p6_narration.sql's description_origin;
-- updated_by is new.

CREATE TABLE management_actions_new (
  action_id TEXT NOT NULL PRIMARY KEY,
  issue_id TEXT,
  finding_id TEXT,
  run_id TEXT,
  engagement_id TEXT,
  skill_id TEXT,
  title TEXT NOT NULL,
  description TEXT,
  owner TEXT,
  risk TEXT CHECK (risk IS NULL OR risk IN ('High', 'Medium', 'Low')),
  status TEXT NOT NULL CHECK (status IN ('draft', 'open', 'under_review', 'in_progress', 'agreed', 'remediated', 'closed')),
  target_date TEXT,
  potential_exposure REAL,
  evidence_link TEXT,
  created_at TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  description_origin TEXT CHECK (description_origin IS NULL OR description_origin IN ('template', 'model', 'human')),
  updated_by TEXT
);

INSERT INTO management_actions_new (
  action_id, issue_id, finding_id, run_id, engagement_id, skill_id, title, description, owner,
  risk, status, target_date, potential_exposure, evidence_link, created_at, last_updated,
  description_origin
)
SELECT
  action_id, issue_id, finding_id, run_id, engagement_id, skill_id, title, description, owner,
  risk, status, target_date, potential_exposure, evidence_link, created_at, last_updated,
  description_origin
FROM management_actions;

DROP TABLE management_actions;
ALTER TABLE management_actions_new RENAME TO management_actions;
