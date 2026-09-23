-- 001_p1a_ledger: run ledger tables (runs, run_state, run_fingerprints, node_attempts, trace_events, schema_migrations)

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.runs (
  run_id STRING NOT NULL,
  run_kind STRING NOT NULL,
  engagement_id STRING,
  skill_id STRING,
  skill_version STRING,
  mode STRING NOT NULL,
  phase STRING NOT NULL,
  status STRING NOT NULL,
  status_reason STRING,
  audit_period_start DATE NOT NULL,
  audit_period_end DATE NOT NULL,
  objective STRING NOT NULL,
  run_owner STRING NOT NULL,
  fingerprint_id STRING NOT NULL,
  state_version BIGINT NOT NULL,
  superseded_by STRING,
  created_at TIMESTAMP NOT NULL,
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  last_state_change_at TIMESTAMP NOT NULL,
  CONSTRAINT runs_pk PRIMARY KEY (run_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.run_state (
  run_id STRING NOT NULL,
  state_version BIGINT NOT NULL,
  state_json STRING NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CONSTRAINT run_state_pk PRIMARY KEY (run_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.run_fingerprints (
  fingerprint_id STRING NOT NULL,
  source_table_versions STRING NOT NULL,
  uploaded_file_hashes STRING NOT NULL,
  skill_content_hash STRING,
  code_revision STRING NOT NULL,
  dependency_lock_hash STRING NOT NULL,
  runtime_config_hash STRING NOT NULL,
  endpoint_config STRING NOT NULL,
  prompt_template_version STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT run_fingerprints_pk PRIMARY KEY (fingerprint_id)
) USING DELTA TBLPROPERTIES (
  'delta.appendOnly' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.node_attempts (
  attempt_id STRING NOT NULL,
  execution_key STRING NOT NULL,
  run_id STRING NOT NULL,
  phase STRING NOT NULL,
  node_index INT NOT NULL,
  node_name STRING NOT NULL,
  attempt_number INT NOT NULL,
  started_at TIMESTAMP NOT NULL,
  completed_at TIMESTAMP,
  outcome STRING,
  error_detail STRING,
  state_version_before BIGINT NOT NULL,
  state_version_after BIGINT,
  result_state_json STRING,
  CONSTRAINT node_attempts_pk PRIMARY KEY (attempt_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.trace_events (
  event_id STRING NOT NULL,
  run_id STRING NOT NULL,
  engagement_id STRING,
  event_type STRING NOT NULL,
  event_time TIMESTAMP NOT NULL,
  stage STRING NOT NULL,
  status STRING NOT NULL,
  message STRING NOT NULL,
  duration_s DOUBLE,
  node_name STRING,
  execution_key STRING,
  actor STRING NOT NULL,
  state_version BIGINT,
  CONSTRAINT trace_events_pk PRIMARY KEY (event_id)
) USING DELTA TBLPROPERTIES (
  'delta.appendOnly' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.schema_migrations (
  version STRING NOT NULL,
  description STRING NOT NULL,
  checksum STRING NOT NULL,
  applied_at TIMESTAMP NOT NULL,
  CONSTRAINT schema_migrations_pk PRIMARY KEY (version)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

ALTER TABLE ${catalog}.${schema}.runs ADD CONSTRAINT runs_run_kind CHECK (run_kind IN ('fieldwork', 'sensing', 'assessment', 'planning'));

ALTER TABLE ${catalog}.${schema}.runs ADD CONSTRAINT runs_mode CHECK (mode IN ('playbook', 'explorer'));

ALTER TABLE ${catalog}.${schema}.runs ADD CONSTRAINT runs_phase CHECK (phase IN ('plan', 'execute', 'export'));

ALTER TABLE ${catalog}.${schema}.runs ADD CONSTRAINT runs_status CHECK (status IN ('queued', 'running', 'awaiting_confirmation', 'awaiting_signoff', 'completed', 'failed', 'interrupted'));

ALTER TABLE ${catalog}.${schema}.runs ADD CONSTRAINT runs_state_version CHECK (state_version >= 1);

ALTER TABLE ${catalog}.${schema}.run_state ADD CONSTRAINT run_state_state_version CHECK (state_version >= 1);

ALTER TABLE ${catalog}.${schema}.node_attempts ADD CONSTRAINT node_attempts_attempt_number CHECK (attempt_number >= 1);

ALTER TABLE ${catalog}.${schema}.node_attempts ADD CONSTRAINT node_attempts_outcome CHECK (outcome IS NULL OR outcome IN ('succeeded', 'failed', 'interrupted'));
