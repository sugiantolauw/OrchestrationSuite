-- 005_p3_indeterminate_severity: allow a finding's severity ladder to record
-- "could not be evaluated" as its own explicit outcome (CLAUDE.md NN14,
-- P2/P3 gate review item 4). See
-- orchestrator/ddl/delta/005_p3_indeterminate_severity.sql for the full
-- rationale -- same change, sqlite dialect.
--
-- SQLite has no ALTER TABLE ... DROP/ADD CONSTRAINT (and no ALTER COLUMN),
-- so widening the severity/severity_basis CHECK constraints requires the
-- standard rebuild-and-copy pattern: create the table with the new
-- constraints under a temporary name, copy every row across unchanged, drop
-- the old table, rename the new one into place. Column list and every other
-- constraint are carried over byte-for-byte from 002_p1b_suite.sql.

CREATE TABLE findings_new (
  finding_id TEXT NOT NULL PRIMARY KEY,
  run_id TEXT NOT NULL,
  engagement_id TEXT,
  rule_id TEXT NOT NULL,
  skill_id TEXT,
  skill_version TEXT,
  test_id TEXT,
  control_id TEXT,
  risk_id TEXT,
  assertion TEXT CHECK (assertion IS NULL OR assertion IN ('design', 'operating')),
  title TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('High', 'Medium', 'Low', 'Indeterminate')),
  severity_rule TEXT,
  threshold_refs_json TEXT,
  proposed_severity TEXT CHECK (proposed_severity IS NULL OR proposed_severity IN ('High', 'Medium', 'Low')),
  proposed_severity_reason TEXT,
  metrics_cited_json TEXT NOT NULL,
  evidence_refs_json TEXT,
  observation TEXT,
  recommendation TEXT,
  management_questions_json TEXT,
  exposure_amount REAL,
  exposure_basis TEXT,
  theme_id TEXT,
  review_state TEXT NOT NULL DEFAULT 'draft' CHECK (review_state IN ('draft', 'prepared', 'reviewed', 'approved')),
  prior_finding_id TEXT,
  recurrence_count INTEGER NOT NULL DEFAULT 0 CHECK (recurrence_count >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  analyst_set_severity INTEGER,
  severity_basis TEXT CHECK (severity_basis IS NULL OR severity_basis IN ('fixed', 'threshold', 'indeterminate'))
);

INSERT INTO findings_new (
  finding_id, run_id, engagement_id, rule_id, skill_id, skill_version, test_id, control_id,
  risk_id, assertion, title, severity, severity_rule, threshold_refs_json, proposed_severity,
  proposed_severity_reason, metrics_cited_json, evidence_refs_json, observation, recommendation,
  management_questions_json, exposure_amount, exposure_basis, theme_id, review_state,
  prior_finding_id, recurrence_count, created_at, updated_at, analyst_set_severity, severity_basis
)
SELECT
  finding_id, run_id, engagement_id, rule_id, skill_id, skill_version, test_id, control_id,
  risk_id, assertion, title, severity, severity_rule, threshold_refs_json, proposed_severity,
  proposed_severity_reason, metrics_cited_json, evidence_refs_json, observation, recommendation,
  management_questions_json, exposure_amount, exposure_basis, theme_id, review_state,
  prior_finding_id, recurrence_count, created_at, updated_at, analyst_set_severity, severity_basis
FROM findings;

DROP TABLE findings;
ALTER TABLE findings_new RENAME TO findings;
