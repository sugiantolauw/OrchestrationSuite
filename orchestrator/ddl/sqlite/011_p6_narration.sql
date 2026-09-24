-- 011_p6_narration: model-written prose for findings and related artifacts
-- (P6, docs/specs/P6_narration_design.md §6.1). See
-- orchestrator/ddl/delta/011_p6_narration.sql for full rationale -- same
-- tables and columns, SQLite dialect.
--
-- Narration is cached keyed on (prompt_sha256, endpoint, served_model_version,
-- params) for deterministic replay on the same data (NN8). Every sentence is
-- validated before storage to prohibit model-invented numbers and preserve
-- G11 faithfulness. Models write prose with typed placeholders {class:name}
-- which Python renders to formatted values at display time. Rules decide
-- numbers; models decide words.
--
-- The findings table gains four columns (origin, candidate_id, accepted_by,
-- accepted_at) and updated severity_basis CHECK to include 'ai_proposed' (a
-- finding accepted from an AI proposal has severity_basis='ai_proposed'). SQLite
-- has no ALTER TABLE ... DROP/ADD CONSTRAINT, so widening the CHECK requires
-- the standard rebuild-and-copy pattern: create the table with the new
-- constraints under a temporary name, copy every row across unchanged, drop the
-- old table, rename the new one into place. Column list and every other
-- constraint carried over from 002_p1b_suite.sql + subsequent migrations
-- (004–010).

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
  monetary_basis TEXT
    CHECK (monetary_basis IS NULL OR monetary_basis IN ('spend', 'excess', 'approved_not_spent', 'none')),
  theme_id TEXT,
  review_state TEXT NOT NULL DEFAULT 'draft' CHECK (review_state IN ('draft', 'prepared', 'reviewed', 'approved')),
  prior_finding_id TEXT,
  recurrence_count INTEGER NOT NULL DEFAULT 0 CHECK (recurrence_count >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  analyst_set_severity INTEGER,
  severity_basis TEXT
    CHECK (severity_basis IS NULL OR severity_basis IN ('fixed', 'threshold', 'indeterminate', 'ai_proposed')),
  origin TEXT CHECK (origin IS NULL OR origin IN ('rule', 'ai_proposed')),
  candidate_id TEXT,
  accepted_by TEXT,
  accepted_at TEXT
);

INSERT INTO findings_new (
  finding_id, run_id, engagement_id, rule_id, skill_id, skill_version, test_id, control_id,
  risk_id, assertion, title, severity, severity_rule, threshold_refs_json, proposed_severity,
  proposed_severity_reason, metrics_cited_json, evidence_refs_json, observation, recommendation,
  management_questions_json, exposure_amount, exposure_basis, monetary_basis, theme_id, review_state,
  prior_finding_id, recurrence_count, created_at, updated_at, analyst_set_severity, severity_basis,
  origin, candidate_id, accepted_by, accepted_at
)
SELECT
  finding_id, run_id, engagement_id, rule_id, skill_id, skill_version, test_id, control_id,
  risk_id, assertion, title, severity, severity_rule, threshold_refs_json, proposed_severity,
  proposed_severity_reason, metrics_cited_json, evidence_refs_json, observation, recommendation,
  management_questions_json, exposure_amount, exposure_basis, monetary_basis, theme_id, review_state,
  prior_finding_id, recurrence_count, created_at, updated_at, analyst_set_severity, severity_basis,
  'rule', NULL, NULL, NULL
FROM findings;

DROP TABLE findings;
ALTER TABLE findings_new RENAME TO findings;

CREATE TABLE IF NOT EXISTS narratives (
  narrative_id TEXT NOT NULL PRIMARY KEY,
  run_id TEXT NOT NULL,
  engagement_id TEXT,
  target_kind TEXT NOT NULL
    CHECK (target_kind IN ('finding', 'candidate', 'theme', 'run', 'chart', 'profile')),
  target_id TEXT NOT NULL,
  field TEXT NOT NULL,
  version INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  origin TEXT NOT NULL
    CHECK (origin IN ('model', 'model_repaired', 'fallback_invalid', 'fallback_unavailable', 'human_edit')),
  template_text TEXT,
  sources_json TEXT NOT NULL,
  call_ids_json TEXT NOT NULL,
  served_model_version TEXT,
  violations_json TEXT,
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_edits (
  edit_id TEXT NOT NULL PRIMARY KEY,
  narrative_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  origin TEXT NOT NULL
    CHECK (origin IN ('generated', 'repaired', 'fallback', 'regenerated', 'human_edit')),
  action TEXT NOT NULL
    CHECK (action IN ('generated', 'repaired', 'fallback', 'regenerated', 'human_edit')),
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  before_text TEXT,
  after_text TEXT,
  diff TEXT,
  reason TEXT,
  call_id TEXT
);

CREATE TABLE IF NOT EXISTS finding_candidates (
  candidate_id TEXT NOT NULL PRIMARY KEY,
  run_id TEXT NOT NULL,
  engagement_id TEXT,
  skill_id TEXT,
  generation INTEGER NOT NULL,
  rule_id TEXT NOT NULL,
  title TEXT NOT NULL,
  metrics_cited_json TEXT NOT NULL,
  producing_test_ids_json TEXT NOT NULL,
  proposed_severity TEXT NOT NULL
    CHECK (proposed_severity IN ('High', 'Medium', 'Low')),
  severity_reason TEXT,
  rationale TEXT,
  monetary_basis TEXT NOT NULL
    CHECK (monetary_basis IN ('spend', 'excess', 'approved_not_spent', 'none')),
  monetary_basis_note TEXT,
  exposure_amount REAL,
  headline_eligible INTEGER NOT NULL,
  headline_ineligible_reason TEXT,
  candidate_status TEXT NOT NULL
    CHECK (candidate_status IN ('candidate', 'accepted', 'rejected', 'superseded')),
  decided_by TEXT,
  decided_at TEXT,
  decision_reason TEXT,
  decided_severity TEXT CHECK (decided_severity IS NULL OR decided_severity IN ('High', 'Medium', 'Low')),
  call_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finding_themes (
  theme_id TEXT NOT NULL PRIMARY KEY,
  run_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  finding_ids_json TEXT NOT NULL,
  superseded INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_line_values (
  run_id TEXT NOT NULL,
  test_id TEXT NOT NULL,
  source TEXT NOT NULL,
  row_key TEXT NOT NULL,
  line_key TEXT NOT NULL,
  spend_amount REAL NOT NULL,
  excess_amount REAL,
  PRIMARY KEY (run_id, test_id, source, row_key)
);

ALTER TABLE management_actions ADD COLUMN description_origin TEXT
  CHECK (description_origin IS NULL OR description_origin IN ('template', 'model', 'human'));
