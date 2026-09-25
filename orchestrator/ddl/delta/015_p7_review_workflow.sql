-- 015_p7_review_workflow: preparer -> reviewer -> approver segregation of
-- duties inside the existing awaiting_signoff status (docs/specs/
-- P7_mapping_authoring_design.md §3.4). See
-- orchestrator/ddl/sqlite/015_p7_review_workflow.sql for the full rationale
-- -- same columns, Delta dialect.

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.review_steps (
  step_id STRING NOT NULL,
  run_id STRING NOT NULL,
  engagement_id STRING,
  action STRING NOT NULL,
  actor STRING NOT NULL,
  role STRING NOT NULL,
  matched_group STRING,
  role_source STRING NOT NULL,
  sod_mode STRING NOT NULL,
  reason STRING,
  theme_generation INT,
  narration_generation INT,
  state_version INT NOT NULL,
  at TIMESTAMP NOT NULL,
  CONSTRAINT review_steps_pk PRIMARY KEY (step_id)
) USING DELTA TBLPROPERTIES (
  'delta.appendOnly' = 'true'
);

ALTER TABLE ${catalog}.${schema}.review_steps ADD CONSTRAINT review_steps_action CHECK (action IN ('prepared', 'reviewed', 'returned', 'approved'));

ALTER TABLE ${catalog}.${schema}.review_steps ADD CONSTRAINT review_steps_role CHECK (role IN ('preparer', 'reviewer', 'approver'));

ALTER TABLE ${catalog}.${schema}.review_steps ADD CONSTRAINT review_steps_role_source CHECK (role_source IN ('workspace_groups', 'config'));

ALTER TABLE ${catalog}.${schema}.review_steps ADD CONSTRAINT review_steps_sod_mode CHECK (sod_mode IN ('enforced', 'labelled'));

ALTER TABLE ${catalog}.${schema}.review_notes ADD COLUMNS (raised_role STRING, responded_by STRING, responded_at TIMESTAMP, cleared_role STRING);

ALTER TABLE ${catalog}.${schema}.narrative_edits ADD COLUMN review_stage STRING;

ALTER TABLE ${catalog}.${schema}.narrative_edits ADD COLUMN note_id STRING;

ALTER TABLE ${catalog}.${schema}.runs ADD COLUMNS (prepared_at TIMESTAMP, reviewed_at TIMESTAMP);
