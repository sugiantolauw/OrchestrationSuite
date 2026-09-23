-- 001_p1a_ledger: run ledger tables (runs, run_state, run_fingerprints, node_attempts, trace_events, schema_migrations)

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT NOT NULL PRIMARY KEY,
  run_kind TEXT NOT NULL CHECK (run_kind IN ('fieldwork', 'sensing', 'assessment', 'planning')),
  engagement_id TEXT,
  skill_id TEXT,
  skill_version TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('playbook', 'explorer')),
  phase TEXT NOT NULL CHECK (phase IN ('plan', 'execute', 'export')),
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'awaiting_confirmation', 'awaiting_signoff', 'completed', 'failed', 'interrupted')),
  status_reason TEXT,
  audit_period_start TEXT NOT NULL,
  audit_period_end TEXT NOT NULL,
  objective TEXT NOT NULL,
  run_owner TEXT NOT NULL,
  fingerprint_id TEXT NOT NULL,
  state_version INTEGER NOT NULL CHECK (state_version >= 1),
  superseded_by TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  last_state_change_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_state (
  run_id TEXT NOT NULL PRIMARY KEY,
  state_version INTEGER NOT NULL CHECK (state_version >= 1),
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_fingerprints (
  fingerprint_id TEXT NOT NULL PRIMARY KEY,
  source_table_versions TEXT NOT NULL,
  uploaded_file_hashes TEXT NOT NULL,
  skill_content_hash TEXT,
  code_revision TEXT NOT NULL,
  dependency_lock_hash TEXT NOT NULL,
  runtime_config_hash TEXT NOT NULL,
  endpoint_config TEXT NOT NULL,
  prompt_template_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_attempts (
  attempt_id TEXT NOT NULL PRIMARY KEY,
  execution_key TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  node_index INTEGER NOT NULL,
  node_name TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  outcome TEXT CHECK (outcome IS NULL OR outcome IN ('succeeded', 'failed', 'interrupted')),
  error_detail TEXT,
  state_version_before INTEGER NOT NULL,
  state_version_after INTEGER,
  result_state_json TEXT
);

CREATE TABLE IF NOT EXISTS trace_events (
  event_id TEXT NOT NULL PRIMARY KEY,
  run_id TEXT NOT NULL,
  engagement_id TEXT,
  event_type TEXT NOT NULL,
  event_time TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL,
  duration_s REAL,
  node_name TEXT,
  execution_key TEXT,
  actor TEXT NOT NULL,
  state_version INTEGER
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT NOT NULL PRIMARY KEY,
  description TEXT NOT NULL,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS run_fingerprints_no_update BEFORE UPDATE ON run_fingerprints BEGIN SELECT RAISE(ABORT, 'run_fingerprints is append-only'); END;

CREATE TRIGGER IF NOT EXISTS run_fingerprints_no_delete BEFORE DELETE ON run_fingerprints BEGIN SELECT RAISE(ABORT, 'run_fingerprints is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trace_events_no_update BEFORE UPDATE ON trace_events BEGIN SELECT RAISE(ABORT, 'trace_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trace_events_no_delete BEFORE DELETE ON trace_events BEGIN SELECT RAISE(ABORT, 'trace_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'runs are audit evidence and cannot be deleted'); END;
