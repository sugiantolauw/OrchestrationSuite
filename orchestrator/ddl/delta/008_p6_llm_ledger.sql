-- 008_p6_llm_ledger: the LLM call ledger (independent review 2026-09-24
-- item 3). See orchestrator/ddl/sqlite/008_p6_llm_ledger.sql for the full
-- rationale -- same columns, Delta dialect.

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.llm_calls (
  call_id STRING NOT NULL,
  run_id STRING,
  engagement_id STRING,
  node_name STRING,
  execution_key STRING,
  task STRING NOT NULL,
  seq INT NOT NULL,
  transport_attempt INT NOT NULL,
  endpoint_role STRING NOT NULL,
  endpoint STRING,
  served_model_version STRING,
  source STRING,
  cache_hit BOOLEAN NOT NULL,
  cache_key STRING,
  cached_from_call_id STRING,
  version_changed BOOLEAN NOT NULL,
  prompt_template_id STRING NOT NULL,
  prompt_template_version STRING NOT NULL,
  prompt_sha256 STRING NOT NULL,
  messages_json STRING NOT NULL,
  params_sent_json STRING NOT NULL,
  params_withheld_json STRING NOT NULL,
  response_text STRING,
  reasoning_parts_stripped INT NOT NULL,
  finish_reason STRING,
  prompt_tokens BIGINT,
  completion_tokens BIGINT,
  total_tokens BIGINT,
  latency_ms BIGINT,
  request_id STRING,
  outcome STRING NOT NULL,
  error_type STRING,
  error_status_code INT,
  error_message STRING,
  pii_columns_masked_json STRING NOT NULL,
  pii_whitelist_json STRING NOT NULL,
  actor STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT llm_calls_pk PRIMARY KEY (call_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);
ALTER TABLE ${catalog}.${schema}.llm_calls ADD CONSTRAINT llm_calls_outcome_chk CHECK (outcome IN
  ('succeeded', 'invalid_output', 'unavailable', 'failed_transport', 'bad_request', 'replay_miss'));
ALTER TABLE ${catalog}.${schema}.llm_calls ADD CONSTRAINT llm_calls_source_chk
  CHECK (source IS NULL OR source IN ('live', 'cache'));

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.llm_cache (
  cache_key STRING NOT NULL,
  prompt_sha256 STRING NOT NULL,
  endpoint STRING NOT NULL,
  served_model_version STRING NOT NULL,
  params_json STRING NOT NULL,
  response_text STRING NOT NULL,
  finish_reason STRING NOT NULL,
  usage_json STRING NOT NULL,
  source_call_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT llm_cache_pk PRIMARY KEY (cache_key)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);
