-- 011_p6_narration: model-written prose for findings and related artifacts
-- (P6, docs/specs/P6_narration_design.md §6.1). Narration is cached keyed on
-- (prompt_sha256, endpoint, served_model_version, params), giving deterministic
-- replay on the same data (NN8). Every sentence is validated before storage
-- (§3.3-3.4) to prohibit model-invented numbers and preserve G11 faithfulness.
-- narrative_id and related prose fields use typed placeholders {class:name}
-- which Python renders to formatted values (format_metric_value) at display
-- time. Models write prose with placeholders; Python decides every number.
-- Regenerate bumps generation and re-narrates; each generation texts are
-- versioned and tracked in narrative_edits.

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.narratives (
  narrative_id STRING NOT NULL,
  run_id STRING NOT NULL,
  engagement_id STRING,
  target_kind STRING NOT NULL,
  target_id STRING NOT NULL,
  field STRING NOT NULL,
  version INT NOT NULL,
  generation INT NOT NULL,
  origin STRING NOT NULL,
  template_text STRING,
  sources_json STRING NOT NULL,
  call_ids_json STRING NOT NULL,
  served_model_version STRING,
  violations_json STRING,
  updated_by STRING NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CONSTRAINT narratives_pk PRIMARY KEY (narrative_id)
) USING DELTA TBLPROPERTIES ('delta.enableDeletionVectors' = 'true', 'delta.enableRowTracking' = 'true');
ALTER TABLE ${catalog}.${schema}.narratives ADD CONSTRAINT narratives_target_kind_chk
  CHECK (target_kind IN ('finding', 'candidate', 'theme', 'run', 'chart', 'profile'));
ALTER TABLE ${catalog}.${schema}.narratives ADD CONSTRAINT narratives_origin_chk
  CHECK (origin IN ('model', 'model_repaired', 'fallback_invalid', 'fallback_unavailable', 'human_edit'));

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.narrative_edits (
  edit_id STRING NOT NULL,
  narrative_id STRING NOT NULL,
  run_id STRING NOT NULL,
  version INT NOT NULL,
  origin STRING NOT NULL,
  action STRING NOT NULL,
  actor STRING NOT NULL,
  at TIMESTAMP NOT NULL,
  before_text STRING,
  after_text STRING,
  diff STRING,
  reason STRING,
  call_id STRING,
  CONSTRAINT narrative_edits_pk PRIMARY KEY (edit_id)
) USING DELTA TBLPROPERTIES ('delta.enableDeletionVectors' = 'true', 'delta.enableRowTracking' = 'true');
ALTER TABLE ${catalog}.${schema}.narrative_edits ADD CONSTRAINT narrative_edits_origin_chk
  CHECK (origin IN ('generated', 'repaired', 'fallback', 'regenerated', 'human_edit'));
ALTER TABLE ${catalog}.${schema}.narrative_edits ADD CONSTRAINT narrative_edits_action_chk
  CHECK (action IN ('generated', 'repaired', 'fallback', 'regenerated', 'human_edit'));

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.finding_candidates (
  candidate_id STRING NOT NULL,
  run_id STRING NOT NULL,
  engagement_id STRING,
  skill_id STRING,
  generation INT NOT NULL,
  rule_id STRING NOT NULL,
  title STRING NOT NULL,
  metrics_cited_json STRING NOT NULL,
  producing_test_ids_json STRING NOT NULL,
  proposed_severity STRING NOT NULL,
  severity_reason STRING,
  rationale STRING,
  monetary_basis STRING NOT NULL,
  monetary_basis_note STRING,
  exposure_amount DOUBLE,
  headline_eligible BOOLEAN NOT NULL,
  headline_ineligible_reason STRING,
  candidate_status STRING NOT NULL,
  decided_by STRING,
  decided_at TIMESTAMP,
  decision_reason STRING,
  decided_severity STRING,
  call_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CONSTRAINT finding_candidates_pk PRIMARY KEY (candidate_id)
) USING DELTA TBLPROPERTIES ('delta.enableDeletionVectors' = 'true', 'delta.enableRowTracking' = 'true');
ALTER TABLE ${catalog}.${schema}.finding_candidates ADD CONSTRAINT finding_candidates_status_chk
  CHECK (candidate_status IN ('candidate', 'accepted', 'rejected', 'superseded'));
ALTER TABLE ${catalog}.${schema}.finding_candidates ADD CONSTRAINT finding_candidates_proposed_severity_chk
  CHECK (proposed_severity IN ('High', 'Medium', 'Low'));
ALTER TABLE ${catalog}.${schema}.finding_candidates ADD CONSTRAINT finding_candidates_decided_severity_chk
  CHECK (decided_severity IS NULL OR decided_severity IN ('High', 'Medium', 'Low'));
ALTER TABLE ${catalog}.${schema}.finding_candidates ADD CONSTRAINT finding_candidates_monetary_basis_chk
  CHECK (monetary_basis IN ('spend', 'excess', 'approved_not_spent', 'none'));

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.finding_themes (
  theme_id STRING NOT NULL,
  run_id STRING NOT NULL,
  generation INT NOT NULL,
  ordinal INT NOT NULL,
  finding_ids_json STRING NOT NULL,
  superseded BOOLEAN NOT NULL,
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT finding_themes_pk PRIMARY KEY (theme_id)
) USING DELTA TBLPROPERTIES ('delta.enableDeletionVectors' = 'true', 'delta.enableRowTracking' = 'true');

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.test_line_values (
  run_id STRING NOT NULL,
  test_id STRING NOT NULL,
  source STRING NOT NULL,
  row_key STRING NOT NULL,
  line_key STRING NOT NULL,
  spend_amount DOUBLE NOT NULL,
  excess_amount DOUBLE,
  CONSTRAINT test_line_values_pk PRIMARY KEY (run_id, test_id, source, row_key)
) USING DELTA TBLPROPERTIES ('delta.enableDeletionVectors' = 'true', 'delta.enableRowTracking' = 'true');

ALTER TABLE ${catalog}.${schema}.findings ADD COLUMN origin STRING;
ALTER TABLE ${catalog}.${schema}.findings ADD COLUMN candidate_id STRING;
ALTER TABLE ${catalog}.${schema}.findings ADD COLUMN accepted_by STRING;
ALTER TABLE ${catalog}.${schema}.findings ADD COLUMN accepted_at TIMESTAMP;
ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_origin_chk
  CHECK (origin IS NULL OR origin IN ('rule', 'ai_proposed'));
ALTER TABLE ${catalog}.${schema}.findings DROP CONSTRAINT findings_severity_basis_chk;
ALTER TABLE ${catalog}.${schema}.findings ADD CONSTRAINT findings_severity_basis_chk
  CHECK (severity_basis IS NULL OR severity_basis IN ('fixed', 'threshold', 'indeterminate', 'ai_proposed'));
ALTER TABLE ${catalog}.${schema}.management_actions ADD COLUMN description_origin STRING;
ALTER TABLE ${catalog}.${schema}.management_actions ADD CONSTRAINT management_actions_description_origin_chk
  CHECK (description_origin IS NULL OR description_origin IN ('template', 'model', 'human'));
