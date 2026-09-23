-- 003_p3_run_outputs: P3 run-scoped outputs (run_leases, flagged_rows, run_metrics, exports)

CREATE TABLE IF NOT EXISTS run_leases (
  run_id TEXT NOT NULL PRIMARY KEY,
  claimed_by TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flagged_rows (
  run_id TEXT NOT NULL,
  source TEXT NOT NULL,
  row_key TEXT NOT NULL,
  flag TEXT NOT NULL,
  group_id TEXT,
  PRIMARY KEY (run_id, source, row_key, flag)
);

CREATE TABLE IF NOT EXISTS run_metrics (
  run_id TEXT NOT NULL,
  metric_name TEXT NOT NULL,
  value REAL,
  value_text TEXT,
  unit TEXT,
  source_ref_json TEXT,
  test_id TEXT,
  PRIMARY KEY (run_id, metric_name)
);

CREATE TABLE IF NOT EXISTS exports (
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  PRIMARY KEY (run_id, kind)
);
