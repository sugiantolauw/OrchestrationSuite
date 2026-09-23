-- 003_p3_run_outputs: P3 run-scoped outputs (run_leases, flagged_rows, run_metrics, exports)

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.run_leases (
  run_id STRING NOT NULL,
  claimed_by STRING NOT NULL,
  claimed_at TIMESTAMP NOT NULL,
  heartbeat_at TIMESTAMP NOT NULL,
  lease_expires_at TIMESTAMP NOT NULL,
  CONSTRAINT run_leases_pk PRIMARY KEY (run_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.flagged_rows (
  run_id STRING NOT NULL,
  source STRING NOT NULL,
  row_key STRING NOT NULL,
  flag STRING NOT NULL,
  group_id STRING,
  CONSTRAINT flagged_rows_pk PRIMARY KEY (run_id, source, row_key, flag)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.run_metrics (
  run_id STRING NOT NULL,
  metric_name STRING NOT NULL,
  value DOUBLE,
  value_text STRING,
  unit STRING,
  source_ref_json STRING,
  test_id STRING,
  CONSTRAINT run_metrics_pk PRIMARY KEY (run_id, metric_name)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.exports (
  run_id STRING NOT NULL,
  kind STRING NOT NULL,
  path STRING NOT NULL,
  sha256 STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  CONSTRAINT exports_pk PRIMARY KEY (run_id, kind)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);
