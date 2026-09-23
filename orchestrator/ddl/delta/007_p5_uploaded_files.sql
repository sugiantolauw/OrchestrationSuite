-- 007_p5_uploaded_files: business-provided files uploaded from the landing
-- page's "Upload audit files" panel (CLAUDE.md build brief P5). See
-- orchestrator/ddl/sqlite/007_p5_uploaded_files.sql for the full rationale --
-- same columns, Delta dialect.

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.uploaded_files (
  upload_id STRING NOT NULL,
  engagement_id STRING,
  filename STRING NOT NULL,
  volume_path STRING NOT NULL,
  size_bytes BIGINT NOT NULL,
  sha256 STRING NOT NULL,
  uploaded_by STRING NOT NULL,
  uploaded_at TIMESTAMP NOT NULL,
  status STRING NOT NULL,
  row_count BIGINT,
  columns_json STRING,
  error STRING,
  CONSTRAINT uploaded_files_pk PRIMARY KEY (upload_id)
) USING DELTA TBLPROPERTIES (
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true'
);
ALTER TABLE ${catalog}.${schema}.uploaded_files ADD CONSTRAINT uploaded_files_status_chk
  CHECK (status IN ('Uploaded', 'Profiling', 'Ready', 'Warning', 'Failed'));
