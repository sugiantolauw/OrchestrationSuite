-- 009_p6_row_level_classification: per-row results for the T4.3 row-level
-- LLM classification capability (independent review 2026-09-24 item 4). See
-- orchestrator/ddl/sqlite/009_p6_row_level_classification.sql for the full
-- rationale -- same columns, Delta dialect.

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.t43_classifications (
  run_id STRING NOT NULL,
  row_key STRING NOT NULL,
  personal_expense BOOLEAN NOT NULL,
  confidence DOUBLE NOT NULL,
  rationale STRING,
  call_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT t43_classifications_pk PRIMARY KEY (run_id, row_key)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);
