-- 007_p5_uploaded_files: business-provided files uploaded from the landing
-- page Upload audit files panel (CLAUDE.md build brief P5). A row is
-- created at upload time (status='Uploaded'), moved to 'Profiling' while the
-- file is parsed for row_count/columns_json, then 'Ready' or 'Failed' with
-- `error` set (never a silent partial result, CLAUDE.md NN14). Not run-scoped:
-- an uploaded file can be bound to a Skill contract source at run start
-- (like a Unity Catalog table binding) and reused across runs, so it is
-- scoped to an engagement, not a single run.

CREATE TABLE IF NOT EXISTS uploaded_files (
  upload_id TEXT NOT NULL PRIMARY KEY,
  engagement_id TEXT,
  filename TEXT NOT NULL,
  volume_path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('Uploaded', 'Profiling', 'Ready', 'Warning', 'Failed')),
  row_count INTEGER,
  columns_json TEXT,
  error TEXT
);
