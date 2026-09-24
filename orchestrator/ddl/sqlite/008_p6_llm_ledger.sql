-- 008_p6_llm_ledger: the LLM call ledger (independent review 2026-09-24
-- item 3; CLAUDE.md §3 non-negotiable 7/8, §6, docs/specs/P6_P8_explorer_
-- llm_design.md §3.7). Every LLM call -- live or served from cache -- is
-- logged synchronously to llm_calls BEFORE the call returns (NN7): prompt,
-- response text (reasoning parts stripped, never stored, NN11), endpoint,
-- served model version, params actually sent vs withheld, token counts,
-- latency, outcome. llm_cache is the Delta response cache keyed on
-- (prompt_sha256, endpoint, served_model_version, params_json) that gives
-- exact replay of a previously captured response (CLAUDE.md §3
-- non-negotiable 8) -- it does not by itself prove reproducibility of the
-- audit result; the run fingerprint does that.

CREATE TABLE IF NOT EXISTS llm_calls (
  call_id TEXT NOT NULL PRIMARY KEY,
  run_id TEXT,
  engagement_id TEXT,
  node_name TEXT,
  execution_key TEXT,
  task TEXT NOT NULL,
  seq INTEGER NOT NULL,
  transport_attempt INTEGER NOT NULL,
  endpoint_role TEXT NOT NULL,
  endpoint TEXT,
  served_model_version TEXT,
  source TEXT CHECK (source IS NULL OR source IN ('live', 'cache')),
  cache_hit INTEGER NOT NULL,
  cache_key TEXT,
  cached_from_call_id TEXT,
  version_changed INTEGER NOT NULL,
  prompt_template_id TEXT NOT NULL,
  prompt_template_version TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  messages_json TEXT NOT NULL,
  params_sent_json TEXT NOT NULL,
  params_withheld_json TEXT NOT NULL,
  response_text TEXT,
  reasoning_parts_stripped INTEGER NOT NULL,
  finish_reason TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  total_tokens INTEGER,
  latency_ms INTEGER,
  request_id TEXT,
  outcome TEXT NOT NULL CHECK (outcome IN
    ('succeeded', 'invalid_output', 'unavailable', 'failed_transport', 'bad_request', 'replay_miss')),
  error_type TEXT,
  error_status_code INTEGER,
  error_message TEXT,
  pii_columns_masked_json TEXT NOT NULL,
  pii_whitelist_json TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS llm_calls_run_id_idx ON llm_calls (run_id);
CREATE INDEX IF NOT EXISTS llm_calls_endpoint_idx ON llm_calls (endpoint, created_at);

CREATE TABLE IF NOT EXISTS llm_cache (
  cache_key TEXT NOT NULL PRIMARY KEY,
  prompt_sha256 TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  served_model_version TEXT NOT NULL,
  params_json TEXT NOT NULL,
  response_text TEXT NOT NULL,
  finish_reason TEXT NOT NULL,
  usage_json TEXT NOT NULL,
  source_call_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS llm_cache_lookup_idx ON llm_cache (prompt_sha256, endpoint, params_json);
