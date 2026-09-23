-- 002_p1b_suite: engagement scoping and audit-suite tables (engagements, findings, management_actions, skill_versions, risks, controls, risk_assessments, review_notes, issues)

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.engagements (
  engagement_id STRING NOT NULL,
  name STRING NOT NULL,
  entity STRING,
  period_start DATE,
  period_end DATE,
  owner STRING NOT NULL,
  status STRING NOT NULL,
  stage STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT engagements_pk PRIMARY KEY (engagement_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.findings (
  finding_id STRING NOT NULL,
  run_id STRING NOT NULL,
  engagement_id STRING,
  rule_id STRING NOT NULL,
  skill_id STRING,
  skill_version STRING,
  test_id STRING,
  control_id STRING,
  risk_id STRING,
  assertion STRING,
  title STRING NOT NULL,
  severity STRING NOT NULL,
  severity_rule STRING,
  threshold_refs_json STRING,
  proposed_severity STRING,
  proposed_severity_reason STRING,
  metrics_cited_json STRING NOT NULL,
  evidence_refs_json STRING,
  observation STRING,
  recommendation STRING,
  management_questions_json STRING,
  exposure_amount DOUBLE,
  exposure_basis STRING,
  theme_id STRING,
  review_state STRING NOT NULL,
  prior_finding_id STRING,
  recurrence_count INT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CONSTRAINT findings_pk PRIMARY KEY (finding_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.issues (
  issue_id STRING NOT NULL,
  engagement_id STRING,
  rule_id STRING,
  title STRING NOT NULL,
  description STRING,
  rating STRING,
  status STRING NOT NULL,
  raised_by STRING,
  raised_at TIMESTAMP,
  owner STRING,
  due_date DATE,
  remediation_plan STRING,
  management_response STRING,
  prior_issue_id STRING,
  finding_ids_json STRING NOT NULL,
  run_ids_json STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CONSTRAINT issues_pk PRIMARY KEY (issue_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.management_actions (
  action_id STRING NOT NULL,
  issue_id STRING,
  finding_id STRING,
  run_id STRING,
  engagement_id STRING,
  skill_id STRING,
  title STRING NOT NULL,
  description STRING,
  owner STRING,
  risk STRING,
  status STRING NOT NULL,
  target_date DATE,
  potential_exposure DOUBLE,
  evidence_link STRING,
  created_at TIMESTAMP NOT NULL,
  last_updated TIMESTAMP NOT NULL,
  CONSTRAINT management_actions_pk PRIMARY KEY (action_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.skill_versions (
  skill_id STRING NOT NULL,
  version STRING NOT NULL,
  content_hash STRING NOT NULL,
  content_json STRING NOT NULL,
  status STRING NOT NULL,
  created_by STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  reviewed_by STRING,
  published_by STRING,
  published_at TIMESTAMP,
  superseded_by STRING,
  surface2_results_json STRING,
  CONSTRAINT skill_versions_pk PRIMARY KEY (skill_id, version)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.risks (
  risk_id STRING NOT NULL,
  engagement_id STRING,
  title STRING NOT NULL,
  description STRING,
  category STRING,
  owner STRING,
  status STRING NOT NULL,
  source STRING NOT NULL,
  source_ref STRING,
  as_of_date DATE,
  confidence DOUBLE,
  prior_risk_id STRING,
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT risks_pk PRIMARY KEY (risk_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.controls (
  control_id STRING NOT NULL,
  risk_id STRING,
  engagement_id STRING,
  title STRING NOT NULL,
  description STRING,
  type STRING,
  frequency STRING,
  owner STRING,
  design_conclusion STRING,
  operating_conclusion STRING,
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT controls_pk PRIMARY KEY (control_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.risk_assessments (
  assessment_id STRING NOT NULL,
  risk_id STRING NOT NULL,
  dimension STRING NOT NULL,
  score DOUBLE NOT NULL,
  rationale STRING,
  method STRING NOT NULL,
  assessed_by STRING NOT NULL,
  assessed_at TIMESTAMP NOT NULL,
  source_ref STRING,
  CONSTRAINT risk_assessments_pk PRIMARY KEY (assessment_id)
) USING DELTA TBLPROPERTIES (
  'delta.appendOnly' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.review_notes (
  note_id STRING NOT NULL,
  engagement_id STRING,
  run_id STRING,
  finding_id STRING,
  raised_by STRING NOT NULL,
  raised_at TIMESTAMP NOT NULL,
  body STRING NOT NULL,
  state STRING NOT NULL,
  cleared_by STRING,
  cleared_at TIMESTAMP,
  response STRING,
  CONSTRAINT review_notes_pk PRIMARY KEY (note_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

ALTER TABLE ${catalog}.${schema}.engagements ADD CONSTRAINT engagements_status CHECK (status IN ('open', 'closed'));

ALTER TABLE ${catalog}.${schema}.engagements ADD CONSTRAINT engagements_stage CHECK (stage IN ('planning', 'fieldwork', 'reporting', 'closed'));

ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_assertion CHECK (assertion IS NULL OR assertion IN ('design', 'operating'));

ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_severity CHECK (severity IN ('High', 'Medium', 'Low'));

ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_proposed_severity CHECK (proposed_severity IS NULL OR proposed_severity IN ('High', 'Medium', 'Low'));

ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_review_state CHECK (review_state IN ('draft', 'prepared', 'reviewed', 'approved'));

ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_recurrence_count CHECK (recurrence_count >= 0);

ALTER TABLE ${catalog}.${schema}.issues ADD CONSTRAINT issues_rating CHECK (rating IS NULL OR rating IN ('High', 'Medium', 'Low'));

ALTER TABLE ${catalog}.${schema}.issues ADD CONSTRAINT issues_status CHECK (status IN ('draft', 'open', 'agreed', 'remediated', 'closed', 'superseded'));

ALTER TABLE ${catalog}.${schema}.management_actions ADD CONSTRAINT management_actions_risk CHECK (risk IS NULL OR risk IN ('High', 'Medium', 'Low'));

ALTER TABLE ${catalog}.${schema}.management_actions ADD CONSTRAINT management_actions_status CHECK (status IN ('draft', 'open', 'under_review', 'in_progress', 'closed'));

ALTER TABLE ${catalog}.${schema}.skill_versions ADD CONSTRAINT skill_versions_status CHECK (status IN ('draft', 'published', 'superseded'));

ALTER TABLE ${catalog}.${schema}.risks ADD CONSTRAINT risks_status CHECK (status IN ('proposed', 'accepted', 'rejected', 'superseded'));

ALTER TABLE ${catalog}.${schema}.risks ADD CONSTRAINT risks_source CHECK (source IN ('manual', 'glean', 'regulation', 'erm_import'));

ALTER TABLE ${catalog}.${schema}.controls ADD CONSTRAINT controls_type CHECK (type IS NULL OR type IN ('preventive', 'detective'));

ALTER TABLE ${catalog}.${schema}.risk_assessments ADD CONSTRAINT risk_assessments_dimension CHECK (dimension IN ('financial', 'reputational', 'regulatory', 'operational'));

ALTER TABLE ${catalog}.${schema}.risk_assessments ADD CONSTRAINT risk_assessments_method CHECK (method IN ('model', 'human'));

ALTER TABLE ${catalog}.${schema}.review_notes ADD CONSTRAINT review_notes_state CHECK (state IN ('open', 'cleared'));

ALTER TABLE ${catalog}.${schema}.runs ADD COLUMNS (prepared_by STRING, reviewed_by STRING, approved_by STRING);

MERGE INTO ${catalog}.${schema}.engagements t USING (SELECT 'ENG-DEFAULT' AS engagement_id) s ON t.engagement_id = s.engagement_id WHEN NOT MATCHED THEN INSERT (engagement_id, name, entity, period_start, period_end, owner, status, stage, created_at) VALUES ('ENG-DEFAULT', 'Default engagement', NULL, NULL, NULL, 'system', 'open', 'fieldwork', current_timestamp());
