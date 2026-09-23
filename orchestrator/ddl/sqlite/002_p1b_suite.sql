-- 002_p1b_suite: engagement scoping and audit-suite tables (engagements, findings, management_actions, skill_versions, risks, controls, risk_assessments, review_notes, issues)

CREATE TABLE IF NOT EXISTS engagements (
  engagement_id TEXT NOT NULL PRIMARY KEY,
  name TEXT NOT NULL,
  entity TEXT,
  period_start TEXT,
  period_end TEXT,
  owner TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
  stage TEXT NOT NULL CHECK (stage IN ('planning', 'fieldwork', 'reporting', 'closed')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
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
  severity TEXT NOT NULL CHECK (severity IN ('High', 'Medium', 'Low')),
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
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
  issue_id TEXT NOT NULL PRIMARY KEY,
  engagement_id TEXT,
  rule_id TEXT,
  title TEXT NOT NULL,
  description TEXT,
  rating TEXT CHECK (rating IS NULL OR rating IN ('High', 'Medium', 'Low')),
  status TEXT NOT NULL CHECK (status IN ('draft', 'open', 'agreed', 'remediated', 'closed', 'superseded')),
  raised_by TEXT,
  raised_at TEXT,
  owner TEXT,
  due_date TEXT,
  remediation_plan TEXT,
  management_response TEXT,
  prior_issue_id TEXT,
  finding_ids_json TEXT NOT NULL,
  run_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS management_actions (
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
  status TEXT NOT NULL CHECK (status IN ('draft', 'open', 'under_review', 'in_progress', 'closed')),
  target_date TEXT,
  potential_exposure REAL,
  evidence_link TEXT,
  created_at TEXT NOT NULL,
  last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
  skill_id TEXT NOT NULL,
  version TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  content_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft', 'published', 'superseded')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  reviewed_by TEXT,
  published_by TEXT,
  published_at TEXT,
  superseded_by TEXT,
  surface2_results_json TEXT,
  PRIMARY KEY (skill_id, version)
);

CREATE TABLE IF NOT EXISTS risks (
  risk_id TEXT NOT NULL PRIMARY KEY,
  engagement_id TEXT,
  title TEXT NOT NULL,
  description TEXT,
  category TEXT,
  owner TEXT,
  status TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'rejected', 'superseded')),
  source TEXT NOT NULL CHECK (source IN ('manual', 'glean', 'regulation', 'erm_import')),
  source_ref TEXT,
  as_of_date TEXT,
  confidence REAL,
  prior_risk_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS controls (
  control_id TEXT NOT NULL PRIMARY KEY,
  risk_id TEXT,
  engagement_id TEXT,
  title TEXT NOT NULL,
  description TEXT,
  type TEXT CHECK (type IS NULL OR type IN ('preventive', 'detective')),
  frequency TEXT,
  owner TEXT,
  design_conclusion TEXT,
  operating_conclusion TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_assessments (
  assessment_id TEXT NOT NULL PRIMARY KEY,
  risk_id TEXT NOT NULL,
  dimension TEXT NOT NULL CHECK (dimension IN ('financial', 'reputational', 'regulatory', 'operational')),
  score REAL NOT NULL,
  rationale TEXT,
  method TEXT NOT NULL CHECK (method IN ('model', 'human')),
  assessed_by TEXT NOT NULL,
  assessed_at TEXT NOT NULL,
  source_ref TEXT
);

CREATE TABLE IF NOT EXISTS review_notes (
  note_id TEXT NOT NULL PRIMARY KEY,
  engagement_id TEXT,
  run_id TEXT,
  finding_id TEXT,
  raised_by TEXT NOT NULL,
  raised_at TEXT NOT NULL,
  body TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'cleared')),
  cleared_by TEXT,
  cleared_at TEXT,
  response TEXT
);

CREATE TRIGGER IF NOT EXISTS risk_assessments_no_update BEFORE UPDATE ON risk_assessments BEGIN SELECT RAISE(ABORT, 'risk_assessments is append-only'); END;

CREATE TRIGGER IF NOT EXISTS risk_assessments_no_delete BEFORE DELETE ON risk_assessments BEGIN SELECT RAISE(ABORT, 'risk_assessments is append-only'); END;

ALTER TABLE runs ADD COLUMN prepared_by TEXT;

ALTER TABLE runs ADD COLUMN reviewed_by TEXT;

ALTER TABLE runs ADD COLUMN approved_by TEXT;

INSERT OR IGNORE INTO engagements (engagement_id, name, entity, period_start, period_end, owner, status, stage, created_at) VALUES ('ENG-DEFAULT', 'Default engagement', NULL, NULL, NULL, 'system', 'open', 'fieldwork', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
