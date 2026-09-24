-- 013_p7_export_code_revision: independent review 2026-09-24 gap #11 /
-- CLAUDE.md section 11 Paused runs across a code deploy. See
-- orchestrator/ddl/sqlite/013_p7_export_code_revision.sql for the full
-- rationale -- same columns, Delta dialect. run_fingerprints stays
-- immutable, so the code revision actually used for export is recorded on
-- runs.export_code_revision (cheap direct read) and in the append-only
-- run_fingerprint_overrides history.

ALTER TABLE ${catalog}.${schema}.runs ADD COLUMN export_code_revision STRING;

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.run_fingerprint_overrides (
  override_id STRING NOT NULL,
  run_id STRING NOT NULL,
  fingerprint_id STRING NOT NULL,
  phase STRING NOT NULL,
  field STRING NOT NULL,
  stored_value STRING,
  override_value STRING NOT NULL,
  recorded_at TIMESTAMP NOT NULL,
  CONSTRAINT run_fingerprint_overrides_pk PRIMARY KEY (override_id)
) USING DELTA TBLPROPERTIES (
  'delta.appendOnly' = 'true'
);
